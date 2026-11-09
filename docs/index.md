# 船厂仓储管理系统

本仓库以 Sentry WMS v1.37.0 为技术基座，交付船厂到货清单、吊牌拍照识别、
整票多托收货、缺件补货、正式出库、库存扣减和装车确认闭环。

## 首期范围

- 船号、工程号、制作图绑定；
- 一级/二级仓库区域和船号定置；
- 固定模板到货清单与正式出库清单导入；
- 物料编码、品名、规格的强校验；
- 微信小程序逐托拍摄吊牌、本地优先 OCR、原图留证；
- 整票多托吊牌后缀（`1-1`、`1-2`）识别与齐套校验；
- 少件按实收入库、原批次补货和库存流水；
- 日配送/按需需求导入、前置仓可提过滤、急单/改单派工单和全额留存；
- 正式出库任务、整票库存扣减、装车确认；
- 仓库隔离、页面权限、业务角色和审计。

## 明确不做

- 现场不使用二维码、条码或扫码设备；
- 不解析船厂定制二维码，也不维护制码规则；
- 不保留 Expo / React Native 现场端，唯一现场端是微信小程序；
- 不沿用旧版“派工单反向匹配配送需求”的历史流程，当前以船厂需求表和
  急单派工单为业务入口；
- 首期不维护货架坐标、BOM 或财务结算。

吊牌拍照是同一次业务动作：原图用于留证，同时提交 OCR 识别吊牌字段。默认使用
API 主机内的 RapidOCR；只有明确配置百度兜底时才向百度发送图片。
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
- [真实吊牌 OCR 验收](shipyard/ocr-acceptance.md)
- [正式上线检查表](shipyard/go-live-checklist.md)
- [正式切换与回滚](shipyard/cutover-runbook.md)
- [备份恢复演练](shipyard/recovery-drill.md)
- [风险台账](shipyard/risk-register.md)
- [部署](deployment.md)
- [测试手册](test-lab.md)
- [贡献指南](contributing.md)

## 当前状态

代码与自动化验证完成不等于生产验收。真实现场 OCR 样本、微信真机、合法域名和
船厂 UAT 未完成前，生产状态保持 `NO-GO`。
