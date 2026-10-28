# 船厂仓储管理系统

面向船厂托盘配件仓的收货、在库和正式出库系统，由 Sentry WMS 的认证、权限、
审计和 PostgreSQL 事务底座演进而来。

## 业务范围

- 全局船号、工程号、制作图和厂家绑定
- 仓库二级区域与船号主入库区域
- 到货清单 Excel 识别和人工确认
- 微信小程序逐托拍摄吊牌、公网 OCR、字段强校验和原图留证
- 整票多托收货、少件补货、库存流水和幂等确认
- 正式出库清单识别、库存匹配和整票任务
- 整票多托拍照核销、库存扣减和装车确认
- 用户、角色、页面权限、仓库隔离和审计日志

现场不使用二维码、条码或扫码设备。吊牌原图只上传一次，同时用于 OCR 识别和
业务留证。

## 技术结构

| 层 | 技术 |
| --- | --- |
| API | Python 3.11 / Flask |
| 数据库 | PostgreSQL 16 |
| 管理端 | React / Vite |
| 现场端 | 原生微信小程序 |
| 部署 | Docker Compose |

## 快速启动

```bash
cp .env.example .env
```

编辑 `.env`，至少配置：

- `JWT_SECRET`
- `SENTRY_ENCRYPTION_KEY`
- `REDIS_PASSWORD`
- `SENTRY_PUBSUB_HMAC_KEY`
- `SENTRY_TOKEN_PEPPER`

启动服务：

```bash
docker compose up -d
```

- API：`http://localhost:5000`
- 管理端：`http://localhost:8080`
- 健康检查：`http://localhost:5000/api/health`

公网 OCR 还需要配置 `BAIDU_OCR_API_KEY` 和 `BAIDU_OCR_SECRET_KEY`。

## 微信小程序

用微信开发者工具打开 `miniprogram/`，配置正式 AppID、API HTTPS 域名和
`miniprogram/config.js`。部署前还必须把 API 域名加入 `request` 与
`uploadFile` 合法域名。

## 验证

```bash
cd api
python -m pytest tests/ -q
```

```bash
cd admin
npm ci --ignore-scripts
npm test
npm run build
```

```bash
node --test miniprogram/tests/*.test.js
```

业务规则、接口和风险状态见：

- [船厂基线](docs/shipyard/baseline.md)
- [收货与 OCR](docs/shipyard/receipt-api.md)
- [正式出库与装车](docs/shipyard/formal-outbound-api.md)
- [真实吊牌 OCR 验收](docs/shipyard/ocr-acceptance.md)
- [备份恢复演练](docs/shipyard/recovery-drill.md)
- [风险闭环台账](docs/shipyard/risk-register.md)

## 状态边界

自动测试和 HTTP E2E 已覆盖核心业务事务。真实百度 OCR 样本、微信真机与合法域名、
现场 UAT 和生产部署仍属于外部验收项；完成前生产结论保持 `NO-GO`。

## 许可

本项目保留并遵循上游 Sentry WMS 的 Apache License 2.0。原版权和第三方资产说明
见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
