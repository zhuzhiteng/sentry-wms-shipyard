# 船厂仓储正式上线检查表

更新日期：2026-11-09

本检查表是正式部署的唯一汇总入口。代码、自动测试、镜像构建、部署、微信审核、
真实 OCR 验收和船厂 UAT 是不同层级，任何一层通过都不能替代其它层。
所有项目完成并留存证据前，发布结论保持 `NO-GO`。

## 一、代码与构建

- [ ] 所有改动完成评审，工作区干净，发布源是有 CI 记录的固定 commit；
- [ ] 后端船厂域、迁移、管理端、小程序和 HTTP E2E 全部通过；
- [ ] `frontend/admin` 生产构建通过；
- [ ] API、Admin、ops-backup 镜像构建成功并记录镜像 digest；
- [ ] 当前发布版本和 API `version.py`、管理端 `package.json` 一致；
- [ ] 发布清单包含源码 commit、镜像 digest、小程序包 SHA-256 和构建时间。

当前工程状态：测试和构建已通过，但工作区仍有未提交改动。正式发布必须从已评审、
已提交且通过 CI 的干净 commit 重新构建。

## 二、生产配置和密钥

模板：`deploy/shipyard-production.env.example`

- [ ] 生产环境文件放在仓库外，由服务器密钥管理或受控环境变量注入；
- [ ] `POSTGRES_PASSWORD`、`ADMIN_PASSWORD`、`JWT_SECRET`、
      `SENTRY_ENCRYPTION_KEY`、`SENTRY_TOKEN_PEPPER`、
      `SENTRY_PUBSUB_HMAC_KEY`、`REDIS_PASSWORD` 分别独立生成；
- [ ] `SENTRY_ENCRYPTION_KEY` 已进入密钥备份；丢失后现有连接器凭据不可解密；
- [ ] `ADMIN_PASSWORD` 只在首次初始化空数据卷时生效，已存入密码管理系统；
- [ ] `TRUST_PROXY=true`，API 和管理端只绑定 `127.0.0.1`；
- [ ] `CORS_ORIGINS` 只包含正式 HTTPS 管理端来源；
- [ ] OCR provider 与兜底策略已确认，百度凭据仅在启用百度时注入，告警 Webhook
      和所有占位值均已替换；
- [ ] 执行统一预检并达到 `READY`：

```powershell
node tools/shipyard/release-preflight.mjs `
  --env-file D:\secure\shipyard-production.env `
  --app-id wx_REPLACE_WITH_REAL_APP_ID `
  --api-base https://wms-api.example.com/api `
  --output-dir ..\shipyard-release\miniprogram `
  --report-dir ..\shipyard-release\preflight
```

预检会验证密钥强度、Fernet 格式、HTTPS、代理模式、Compose、迁移文件、Git 状态和
小程序发布包。报告不会输出密钥值。

## 三、数据库首次初始化和升级

全新生产数据卷使用 `docker-compose.prod.yml` 时：

- 自动加载当前 `db/schema.sql`；
- 使用 `SKIP_SEED=true` 只创建最小仓库、区域、库位、管理员和基础设置；
- 使用 `ADMIN_PASSWORD` 写入 bcrypt 哈希，不把明文密码打印到日志；
- 启用 PostgreSQL 数据页校验和。

已有数据库升级时：

- [ ] 先做 pre-cutover custom dump、哈希和隔离恢复；
- [ ] 在维护窗口按版本顺序执行 082–090 船厂迁移；
- [ ] 执行 `db/verify-shipyard-go-live.sql`；
- [ ] 记录迁移耗时、行数、备份 SHA-256 和失败回滚点。

```powershell
docker compose --env-file D:\secure\shipyard-production.env `
  -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T db psql -U sentry_app -d sentry `
  -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/01-schema.sql
```

上述命令只适用于确认需要重新建库的受控场景。已有业务库不得直接重放全量
`schema.sql`；必须使用对应增量迁移和既有备份恢复流程。

### 本地生产形态预演

预演环境使用独立 Compose 项目、唯一容器名、临时卷和回环端口，不接触现有环境：

```powershell
node tools/shipyard/create-prelaunch-env.mjs `
  --output dist/prelaunch/prelaunch.env --force

docker compose -p shipyard-prelaunch `
  --env-file dist/prelaunch/prelaunch.env `
  -f docker-compose.prod.yml `
  -f docker-compose.ops.yml `
  -f docker-compose.prelaunch.yml `
  up -d --build
```

预演完成后执行只读 schema 门禁：

```powershell
docker exec sentry-prelaunch-db psql `
  -U sentry_app -d sentry -v ON_ERROR_STOP=1 `
  -f /seed-data/verify-shipyard-go-live.sql
```

2026-11-02 本地预演结果：

- 全新空卷自动创建 `25` 张船厂表、最小管理员和基础仓库；
- 容器日志确认管理员密码只显示来源，不显示明文；
- API、管理端、Redis、Celery、监控和备份容器正常启动；
- API 健康 `200`，管理员登录、代理识别、到货/出库读取和管理端安全头全部通过；
- 首份备份 `388,067` bytes，SHA-256
  `57a9658ca5ba5c8fe74c3db185d06abe56b6f8350dd40dd15013980d25fefa9f`，
  `pg_restore --list` 通过；
- 恢复到独立库后 schema 门禁 `ok`，`90` 张公共表逐表行数一致；
- 恢复库 API 健康、管理员登录和船厂只读接口通过。

该预演仍使用本机 Docker、临时生成密钥和本机 HTTP 入口，不替代真实服务器、
HTTPS、生产告警和船厂现场验收。

## 四、域名、证书和反向代理

- [ ] API 与管理端域名完成备案、DNS 和证书签发；
- [ ] TLS 仅允许 1.2/1.3，证书自动续期和到期告警已验证；
- [ ] 反向代理覆盖并清洗 `Host`、`X-Forwarded-Proto`、`X-Forwarded-For`；
- [ ] 公网只开放 443，API 5000、Admin 8080、PostgreSQL 5432 和 Redis 不暴露；
- [ ] `TRUST_PROXY` 实际生效，`/api/admin/system-info` 返回
      `proxy_fix_active=true`；
- [ ] 安全响应头、上传大小限制、请求超时和限流符合生产配置。

## 五、OCR 与微信小程序

- [ ] 默认 `SHIPYARD_OCR_PROVIDER=rapidocr`，确定性 stub 未用于正式环境；
- [ ] API 镜像已包含 RapidOCR、onnxruntime 和 `libgomp1`，容器内首次识别已预热；
- [ ] 如启用百度兜底，`SHIPYARD_OCR_FALLBACK_PROVIDER=baidu`、两项凭据、
      百度计费额度和图片外发审批均已确认；未启用时不应要求空白百度凭据；
- [ ] 至少一组脱敏现场吊牌完成字段金标、准确率、超时和错误恢复验收；
- [ ] 首张模型加载、连续识别 P50/P95、容器内存和 4 worker 下的稳定性已记录；
- [ ] 验收报告存放在仓库外受限位置，仓库只保留脱敏结论和报告 SHA-256；
- [ ] 微信企业主体小程序 AppID 已取得；
- [ ] API 域名已加入 request 和 uploadFile 合法域名；
- [ ] 隐私保护指引已声明相机、照片和 OCR 用途，并经微信平台审核；
- [ ] 使用唯一发布脚本生成生产包，`urlCheck=true`、无 `touristappid`；
- [ ] iOS 和 Android 真机完成拍照、强光、戴手套、弱网、断网、上传中断、
      进程中断和重复点击验收；
- [ ] 体验版完成项目负责人和船厂业务人员 UAT 后才提交审核。

不得直接把当前 `touristappid`、`127.0.0.1`、`urlCheck=false` 的源码目录上传为
正式版本。必须使用发布预检生成的生产包。

## 六、监控、告警和备份

- [ ] `docker-compose.ops.yml` 已部署；
- [ ] HTTPS 告警 Webhook 已配置，真实故障与恢复通知均已收到；
- [ ] 首次备份在启动后立即完成，`pg_restore --list`、SHA-256 和权限校验通过；
- [ ] 备份已复制到与主机故障域隔离、启用静态加密和传输加密的仓库外存储；
- [ ] 在隔离预发布库完成恢复，比较表数量、关键行数、审计链和 API 启动；
- [ ] 备份保留 14 份，监控可在备份超龄或失败时告警。

## 七、上线执行

- [ ] 发布负责人、数据库负责人、仓库负责人和船厂联系人到场；
- [ ] 冻结清单导入和现场写操作，记录当前请求量和在途任务；
- [ ] 备份、迁移、部署、健康检查、只读冒烟按
      [`cutover-runbook.md`](cutover-runbook.md) 顺序执行；
- [ ] 部署后 API 健康检查、管理员登录、到货/出库读取和管理端页面通过；
- [ ] 监控 30 分钟内无 5xx、认证失败、数据库锁等待和备份告警；
- [ ] 解冻前完成一笔到货、一笔少货补收、一笔出库和一次装车的现场 UAT；
- [ ] 记录实际版本、镜像 digest、迁移结果、冒烟报告和 UAT 签字。

## 八、发布门禁

| 门禁 | 当前状态 | 关闭证据 |
| --- | --- | --- |
| 代码自动测试与构建 | 本地完成 | 后端/迁移 85、小程序 43、管理端 204、生产构建 |
| 生产数据库初始化 | 代码已修复，待实测 | 全新卷建库、管理员登录和 schema 校验 |
| 发布预检工具 | 已建立 | Node 单测和真实生产环境报告 |
| 小程序生产包 | 工具已建立 | 正式 AppID、HTTPS 和 release manifest |
| 真实 OCR | 部分完成，仍外部阻塞 | 已用脱敏四联单验证 RapidOCR 字段与耗时；仍需扩充不同光照/角度/污损金标，启用百度时再加凭据和隐私审批 |
| 微信合法域名/隐私/真机 | 外部阻塞 | 平台截图、设备和系统版本记录 |
| HTTPS 告警与备份恢复 | 外部阻塞 | Webhook 回执、首份备份、隔离恢复报告 |
| 船厂 UAT 与生产授权 | 外部阻塞 | 业务签字、迁移窗口和回滚审批 |

以上任一门禁未关闭，不得把发布状态改为 `GO`。

## 九、需要项目侧提供的外部输入

| 输入 | 提供方 | 注入方式 |
| --- | --- | --- |
| 企业主体微信小程序 AppID | 微信管理员 | 发布预检参数，不写入仓库 |
| API/Admin 正式域名与证书 | 网络/域名负责人 | 反向代理和 DNS |
| request/uploadFile 合法域名 | 微信管理员 | 微信公众平台 |
| 隐私保护指引和审核结果 | 项目负责人、法务/合规 | 微信公众平台 |
| 百度 OCR API Key / Secret（可选兜底） | 百度智能云管理员 | 服务器密钥管理或环境变量 |
| 脱敏吊牌样图和人工金标 | 船厂业务负责人 | 仓库外受限目录 |
| HTTPS 告警 Webhook | 运维/告警平台 | 生产环境变量 |
| 生产主机、备份卷和离站存储 | 运维 | 生产基础设施 |
| iOS/Android 测试设备与现场网络 | 船厂项目负责人 | 真机 UAT |
| 生产迁移窗口、双人复核和回滚批准 | 发布负责人 | 变更审批记录 |

密钥、密码、Webhook Bearer token、真实吊牌和客户明细不得通过聊天正文、Git commit、
普通文档或截图传递。项目侧提供后，只填写到受控环境/密钥管理系统并重新运行发布预检。
