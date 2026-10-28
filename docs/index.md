# 船厂仓储管理系统

本仓库以 Sentry WMS v1.37.0 为技术基座，交付船厂到货清单、吊牌拍照识别、
整票多托收货、缺件补货、正式出库、库存扣减和装车确认闭环。

## 首期范围

- 船号、工程号、制作图绑定；
- 一级/二级仓库区域和船号定置；
- 固定模板到货清单与正式出库清单导入；
- 物料编码、品名、规格的强校验；
- 微信小程序逐托拍摄吊牌、公网 OCR、原图留证；
- 整票多托吊牌后缀（`1-1`、`1-2`）识别与齐套校验；
- 少件按实收入库、原批次补货和库存流水；
- 正式出库任务、整票库存扣减、装车确认；
- 仓库隔离、页面权限、业务角色和审计。

## 明确不做

- 现场不使用二维码、条码或扫码设备；
- 不解析船厂定制二维码，也不维护制码规则；
- 不保留 Expo / React Native 现场端，唯一现场端是微信小程序；
- 不实现配送需求、派工单、前置仓发货单三段链路；
- 首期不维护货架坐标、BOM 或财务结算。

吊牌拍照是同一次业务动作：原图用于留证，同时提交公网 OCR 识别吊牌字段。
识别结果必须经过服务端字段校验和人工确认，不能直接改库存。

## 技术结构

| 层 | 技术 |
|---|---|
| 管理端 | React 19 / Vite |
| API | Python 3.11 / Flask |
| 数据库 | PostgreSQL 16 |
| 现场端 | 原生微信小程序 |
| 部署 | Docker Compose |

## 本地启动

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

- 管理端：`http://localhost:8080`
- API 健康检查：`http://localhost:5000/api/health`
- 测试库：`sentry_test`

首次启动按 `.env` 中配置创建管理员。正式环境必须更换全部示例密钥并启用 HTTPS。

## 文档

- [基线与验证](shipyard/baseline.md)
- [到货清单](shipyard/inbound-api.md)
- [拍照识别收货](shipyard/receipt-api.md)
- [正式出库与装车](shipyard/formal-outbound-api.md)
- [风险台账](shipyard/risk-register.md)
- [部署](deployment.md)
- [测试手册](test-lab.md)
- [贡献指南](contributing.md)

## 当前状态

代码与自动化验证完成不等于生产验收。真实公网 OCR、微信真机、现场吊牌样本和
船厂 UAT 未完成前，生产状态保持 `NO-GO`。
