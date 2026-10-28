# 船厂仓储测试手册

## 环境

- 管理端：现代浏览器；
- 现场端：微信开发者工具和至少一台真机；
- 服务端：Docker Compose；
- 测试库：独立 `sentry_test`，禁止指向生产库；
- OCR：测试默认使用固定识别结果，现场 UAT 使用已获授权的公网 OCR。

现场不测试二维码、条码或扫码枪。每个托盘拍一张吊牌原图，系统用同一张图完成
OCR 识别和留证。

## 自动化验证

```powershell
docker exec sentry-api sh -c 'export FLASK_ENV=test SENTRY_ALLOW_HTTP_WEBHOOKS=false SENTRY_ALLOW_INTERNAL_WEBHOOKS=false TEST_DATABASE_URL="${DATABASE_URL%/*}/sentry_test"; python -m pytest tests/shipyard tests/test_087_shipyard_label_ocr_migration.py tests/test_088_shipyard_remove_legacy_qr_migration.py tests/test_089_shipyard_remove_distribution_dispatch_migration.py -q'
```

```powershell
Set-Location miniprogram
npm test
```

管理端应在干净 Node 环境执行：

```powershell
Set-Location admin
npm ci
npm test -- --run
npm run build
npm audit --audit-level=high
```

HTTP E2E：

```powershell
docker exec sentry-api sh -c 'export FLASK_ENV=test SENTRY_ALLOW_HTTP_WEBHOOKS=false SENTRY_ALLOW_INTERNAL_WEBHOOKS=false TEST_DATABASE_URL="${DATABASE_URL%/*}/sentry_test"; python tests/e2e_shipyard_http.py'
```

## 人工业务走查

1. 管理端建立船号、工程号、制作图绑定和二级区域定置。
2. 导入到货清单，检查字段映射、重复行、缺失字段和区域冲突。
3. 确认清单后，在小程序逐托拍吊牌，核对物料编码、品名、规格、批次、
   托组后缀和整票总件数。
4. 验证少收只按实收入库，缺件不冻结已有库存；补货后累计补足。
5. 导入正式出库清单，检查库存匹配和阻断项。
6. 在小程序逐托拍吊牌，必须拍齐整票托盘后才能扣减库存。
7. 全部票货完成后确认装车，核对只生成一次库存流水和一次装车记录。
8. 复核原图、OCR 记录、操作人、时间、区域路径和库存流水。

## 上线前专项

- 按 [`shipyard/ocr-acceptance.md`](shipyard/ocr-acceptance.md) 用真实脱敏吊牌
  样本测公网 OCR 的字段准确率、耗时和失败恢复，并保留仓库外报告；
- 在微信真机测拍照、失败重试、弱网和原图上传；
- 验证小程序合法域名、HTTPS、AppID 和隐私声明；
- 连续提交同一幂等键，确认不重复收货、扣库或装车；
- 验证角色收回、跨仓库访问和只读账号均被拒绝；
- 按 [`shipyard/recovery-drill.md`](shipyard/recovery-drill.md) 做独立库备份、
  恢复、迁移重放和 API 启动演练；
- 由仓库和船厂完成至少一轮真实 UAT。

未完成上述专项时，只能声明“代码与模拟验证通过”，不能声明“生产可用”。
