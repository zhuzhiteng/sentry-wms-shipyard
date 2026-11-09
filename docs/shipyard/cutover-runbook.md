# 船厂仓储正式切换与回滚手册

本手册只用于已获得生产部署授权后的切换。执行人不得根据本文自行扩大授权范围。

## 1. 切换前冻结

1. 记录发布 commit、镜像 digest、小程序 release manifest SHA-256；
2. 通知船厂和仓库停止导入新清单、收货、出库和装车写操作；
3. 查询并记录当前未完成到货、部分到货、顺延、出库和装车单数量；
4. 确认监控、告警、数据库、Redis 和备份 sidecar 正常；
5. 保存生产环境文件和证书的受控备份，不复制到普通终端目录。

## 2. 预检

```powershell
node tools/shipyard/release-preflight.mjs `
  --env-file D:\secure\shipyard-production.env `
  --app-id wx_REPLACE_WITH_REAL_APP_ID `
  --api-base https://wms-api.example.com/api `
  --output-dir D:\release\shipyard-miniprogram `
  --report-dir D:\release\preflight
```

只有报告为 `READY` 才继续。出现任何 `error` 时停止，不通过修改报告或降低门槛绕过。

## 3. 数据库保护

1. 使用生产凭据执行 custom-format 备份；
2. 验证 `pg_restore --list`；
3. 记录备份 SHA-256、字节数、开始和结束时间；
4. 将备份复制到独立故障域存储；
5. 在隔离库恢复并执行 `db/verify-shipyard-go-live.sql`；
6. 恢复验证失败时停止切换。

不得直接覆盖生产库做恢复演练。

## 4. 迁移

已有数据库按版本顺序执行 082–090。每条迁移使用：

```text
ON_ERROR_STOP=1
lock_timeout=5s
statement_timeout=120s
```

迁移完成后执行：

```powershell
docker compose --env-file D:\secure\shipyard-production.env `
  -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T db psql -U sentry_app -d sentry `
  -v ON_ERROR_STOP=1 -f /seed-data/verify-shipyard-go-live.sql
```

期望输出：

```text
shipyard_go_live_schema | ok
```

## 5. 应用切换

1. 部署预构建、固定 digest 的 API、Admin、backup 镜像；
2. 等待数据库、Redis、API 健康检查通过；
3. 确认 Celery 和 ops sidecar 无持续重启；
4. 检查 API 日志没有 `BUILD_VERSION` 不一致或启动校验失败；
5. 检查管理端容器只监听回环地址，公网通过 HTTPS 代理访问。

手工启动任何恢复库 API 时必须保留
`SENTRY_INBOUND_MAPPINGS_DIR=/app/db/mappings`，否则非 root API 用户会尝试写入
不可用的 `/db` 并以权限错误退出。

## 6. 只读冒烟

管理员账号和密码只通过环境变量注入：

```powershell
$env:SHIPYARD_SMOKE_USERNAME = 'production-smoke-admin'
$env:SHIPYARD_SMOKE_PASSWORD = (Get-Content D:\secure\smoke-password.txt -Raw).Trim()

docker compose --env-file D:\secure\shipyard-production.env `
  -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T `
  -e SHIPYARD_SMOKE_USERNAME `
  -e SHIPYARD_SMOKE_PASSWORD `
  api python -m scripts.shipyard_deployment_smoke `
  --api-base https://wms-api.example.com/api `
  --admin-url https://wms-admin.example.com `
  --expected-version 1.30.0 `
  --report /tmp/shipyard-smoke.json
```

冒烟是只读检查，不创建收货单、出库单或库存流水。任何一项失败均停止解冻。

## 7. 告警与备份验证

1. 对非关键组件制造一次可控失败，确认达到阈值后收到告警；
2. 恢复组件，确认收到恢复通知；
3. 手动执行一次 `ops-backup --once`；
4. 校验 `.sha256`、归档可读性和备份元数据；
5. 将首份生产备份复制到隔离存储并完成恢复演练。

## 8. 现场 UAT

解冻前至少完成：

1. 导入一份到货清单并生成票货；
2. 连续拍摄两张四联验收单；
3. 足额收货一票并自动进入下一票；
4. 少货一票，填写原因并保留补收状态；
5. 正式出库一票，逐托拍照并确认扣库；
6. 整张出库单确认装车；
7. 查询原图、OCR、操作人、时间、库存流水和状态；
8. 船厂业务负责人签字确认。

## 9. 解冻和观察

- 先解除管理端清单导入；
- 再解除仓库现场写操作；
- 观察 30 分钟：5xx、认证失败、数据库锁、Redis、Celery、备份和告警；
- 无异常后记录 `GO` 结论和证据路径。

## 10. 回滚

### 应用回滚

仅当数据库结构尚未变化且旧镜像兼容当前 schema 时，可切回上一组固定镜像 digest。

### 数据和迁移回滚

090 没有生产级反向 DROP 脚本，因为删除新表会丢失四联验收单、提货需求和无货留存
证据。迁移失败或新版本无法在 5–10 分钟内恢复时：

1. 立即停止 API、Celery 和新版本 sidecar；
2. 禁止继续写数据库；
3. 保留失败数据库实例和日志用于取证；
4. 新建回滚库并从切换前备份恢复；
5. 验证 schema、关键行数、审计链和 API 启动；
6. 将 API 指向恢复库；
7. 切回上一版固定镜像；
8. 重新执行只读冒烟和业务核对。

不得直接在生产原库执行手写反向 SQL。回滚完成前不得恢复现场写操作。

## 11. 证据归档

- 预检 JSON/Markdown 和源码 commit；
- 镜像 build 日志和 digest；
- 备份文件哈希、字节数、恢复耗时和逐表比较；
- 迁移命令、输出和验证 SQL 结果；
- 健康检查和只读冒烟报告；
- 告警与恢复通知回执；
- 微信真机、隐私、合法域名和 OCR 验收结果；
- UAT 签字、发布批准和回滚决策记录。
