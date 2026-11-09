# 真实吊牌 OCR 验收

本流程用真实、已脱敏的船厂吊牌照片，分别验证 RapidOCR 和已启用的云 OCR
字段命中率、耗时和失败情况。它只验证“字段是否读对”，不能用来自动通过库存、
收货、出库或现场 UAT。

## 数据边界

- 样图必须已获准用于 OCR 验收并完成脱敏；
- 样图和验收报告放在仓库外目录；
- 提交前不得把真实吊牌、验收报告或 OCR 密钥加入 Git；
- RapidOCR 默认在 API 主机内处理图片，图片不会因 OCR 离开该主机；
- 只有验收目标是百度，或生产明确配置百度兜底时，图片才会发送给百度；现场和
  数据负责人必须事先知悉该数据流向；
- 脚本不会打印 API Key 或 Secret Key。

## 期望清单

样图目录中默认放置 `expectations.json`。每个 `expected` 字段都会参与准确率统计，
建议至少人工核对以下业务字段：

```json
{
  "dataset": "2026-11 船厂脱敏吊牌验收集",
  "samples": [
    {
      "file": "pallet-01.jpg",
      "expected": {
        "ship_no": "H1613",
        "engineering_no": "H1613",
        "production_drawing_no": "D-001",
        "material_code": "MAT-001",
        "item_name": "船用支架",
        "item_spec": "S-100",
        "batch_no": "H1613-B01",
        "tag_suffix": "1-1",
        "label_quantity": "4"
      }
    }
  ]
}
```

`file` 必须是样图目录内的相对路径，支持 JPEG、PNG 和 WebP，单张不超过 10 MiB。
不要登记重复样图，也不要引用目录外文件。

## 先校验清单

以下命令只检查路径、格式、大小、字段名和重复项，不调用 OCR，也不需要凭据：

```powershell
docker compose run --rm `
  -v "${sampleDir}:/ocr-samples" `
  api python -m scripts.shipyard_ocr_acceptance `
  --input-dir /ocr-samples `
  --dry-run
```

## 正式验收

RapidOCR 是默认 provider，不需要百度凭据。首次识别会加载本地模型，耗时会明显
高于同一 worker 的后续图片。API 镜像配置 2 个 Gunicorn worker，因此两个 worker
各自首次识别都会加载模型；容量规划应按单个预热 worker 约 `0.8 GiB` 峰值内存预留。
执行：

```powershell
docker compose run --rm `
  -v "${sampleDir}:/ocr-samples" `
  api python -m scripts.shipyard_ocr_acceptance `
  --input-dir /ocr-samples `
  --provider rapidocr `
  --interval-seconds 0.5
```

如已批准使用百度，先在仓库根目录 `.env` 注入真实
`BAIDU_OCR_API_KEY` 和 `BAIDU_OCR_SECRET_KEY`，再单独执行：

```powershell
docker compose run --rm `
  -v "${sampleDir}:/ocr-samples" `
  api python -m scripts.shipyard_ocr_acceptance `
  --input-dir /ocr-samples `
  --provider baidu `
  --interval-seconds 0.5
```

验收脚本只为命令行指定的一个 provider 调用 OCR，并显式关闭兜底，避免把本地
结果和百度结果混在同一份准确率报告中。报告同时记录指定服务和逐图实际返回服务；
如发现多个实际服务，不得把该报告当作单一 provider 验收证据。

如果直接在已安装 API 依赖的主机 Python 中运行，可以使用：

```powershell
Set-Location api
python -m scripts.shipyard_ocr_acceptance `
  --input-dir D:\shipyard-ocr-samples `
  --provider rapidocr `
  --env-file ..\.env
```

脚本在样图目录生成两个不覆盖旧报告的文件：

- `shipyard-ocr-report-<UTC时间>.json`：机器可复核的逐字段结果；
- `shipyard-ocr-report-<UTC时间>.md`：业务验收人员可阅读的摘要。

默认要求整图通过率和字段准确率均为 100%。诊断阶段可显式降低门槛，但降低门槛的
报告不能作为上线通过证据：

```powershell
python -m scripts.shipyard_ocr_acceptance `
  --input-dir D:\shipyard-ocr-samples `
  --provider rapidocr `
  --env-file ..\.env `
  --minimum-image-pass-rate 0.95 `
  --minimum-field-accuracy 0.99
```

退出码：

- `0`：达到本次指定门槛；
- `1`：报告已生成，但未达到门槛；
- `2`：清单、文件、凭据或参数不合格，未开始正式验收。

## 通过条件

风险 `SHIP-R07` 只有在以下证据同时存在时才能关闭：

1. 使用第 1 版计划字段的人工金标清单；
2. 使用对应的正式 provider 配置和至少一轮不同光照、角度、污损程度的脱敏样图
   完成验收；启用百度时保留凭据和隐私审批记录；
3. 报告达到项目负责人确认的门槛；
4. 凭据未配置错误、超时和 provider 错误有明确处置记录；
5. 报告保存在受限位置，仓库只保留脱敏结论和报告 SHA-256。

OCR 报告通过后，仍须完成微信真机、合法域名、现场业务 UAT 和部署回滚演练。
