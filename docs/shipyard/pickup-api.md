# 日配送、按需与急单提货 API

本文记录 `090_shipyard_pickup_requests.sql` 新增的提货需求链路。它处理船厂
日配送需求、按需工作表以及临时急单/改单派工单。

船厂配送单是前置仓库存事实的主要依据。正常路径只处理配送单标注“前置仓”
有货的行，不要求仓库逐行核库存、确认匹配或选择库存票。系统在后台自动定位
库存票货和托位，目的只是生成可执行的出库任务。找不到唯一对应关系属于少见
的数据异常，不是日常缺货流程。

## 来源与单据

- `DAILY_DEMAND`：日配送工作表；
- `ON_DEMAND`：同一工作簿中的按需工作表；
- `SPECIAL_DISPATCH`：急单、改单或前置仓提货派工单。

日配送/按需文件中的普通行没有独立需求数量时，系统按一整张仓内票货匹配。
急单/改单有数量时，只有整票可用数量与提货数量一致才自动匹配。系统不拆票。

日配送表的`到货情况=前置仓`表示船厂配送单声明前置仓有货。只有该声明为有货的行
才进入本批可提货候选；`未到`、`前置仓无货`或`系统未查到该托盘`的行直接标记
`SOURCE_UNAVAILABLE`并留存。系统随后自动定位对应库存票货，不把这一步呈现为
人工库存确认。按需表和急单/改单派工单没有日配送的“前置仓状态”列，系统按
各自需求行自动定位，不推断不存在的空白状态。

## 导入与查询

```text
POST /api/shipyard/pickup-requests/daily/preview
POST /api/shipyard/pickup-requests/special
GET  /api/shipyard/pickup-requests?warehouse_id=1
GET  /api/shipyard/pickup-requests/<request_id>
GET  /api/shipyard/pickup-requests/<request_id>/file
GET  /api/shipyard/pickup-requests/<request_id>/print
```

打印接口默认只打印已经匹配为`AVAILABLE`或已生成出库任务的`OUTBOUND_PENDING`
行。`?include_unavailable=1`生成完整需求留存版，包含配送单未到和少见的
后台数据异常行。两种打印都不改变需求状态。

日配送导入使用 `multipart/form-data`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `warehouse_id` | 是 | 前置仓所属仓库 |
| `file` | 是 | `.xlsx`，最多 10 MiB |
| `request_no` | 否 | 不填时自动生成 |

解析器读取所有带有效表头的工作表，因此同一文件中的`日需求1`和`按需`
都会导入。辅助说明页忽略。原始行、原始字段、Excel 行号和来源工作表全部保存。
导入完成后直接进入已确认状态，不需要再点一次“确认需求与匹配”。

## 匹配状态

| 状态 | 含义 |
| --- | --- |
| `PENDING` | 系统处理中 |
| `AVAILABLE` | 已自动定位唯一可提整票 |
| `AMBIGUOUS` | 数据同时对应多张票，进入后台核对 |
| `SOURCE_UNAVAILABLE` | 配送单声明前置仓无货或未到，本批不安排 |
| `WAREHOUSE_MISSING` | 系统未找到对应票货，作为数据异常留存 |
| `QUANTITY_MISMATCH` | 找到同托盘票货，但整票数量与提货数量不一致 |
| `OUTBOUND_PENDING` | 已生成出库任务 |
| `OUTBOUND` | 对应票货已经出库 |
| `DUPLICATE_DEMAND` | 同一请求重复占用同一票货 |
| `VOID` | 已作废 |

## 后台异常处理

以下接口保留给后台兼容和少见的数据冲突处理，不是管理端的正常操作步骤：

```text
POST /api/shipyard/pickup-requests/<request_id>/confirm
POST /api/shipyard/pickup-requests/<request_id>/rematch
PUT  /api/shipyard/pickup-requests/<request_id>/lines/<line_id>/ticket
```

人工选票请求体：

```json
{"ticket_id": "00000000-0000-0000-0000-000000000000"}
```

`rematch` 只重新计算尚未生成出库任务的行。库存或基础数据修正后，原异常行
可以重新自动定位，不需要重新导入整张需求。

## 生成整票出库任务

`POST /api/shipyard/pickup-requests/<request_id>/outbound`

```json
{
  "line_ids": [
    "00000000-0000-0000-0000-000000000001"
  ]
}
```

不传`line_ids`时处理所有当前可提行。每个需求行只能占用一张库存票货；重复
票货行改为`DUPLICATE_DEMAND`。生成动作创建中间出库清单、正式出库单和
整票出库任务，然后交给现有小程序吊牌拍照与整票核销链路。

少见的未定位、多票候选和数量不一致行不会进入出库任务，继续保留在提货需求中。

## 到货顺延和四联验收单

```text
POST   /api/shipyard/inbound/tickets/<ticket_id>/deferral
DELETE /api/shipyard/inbound/tickets/<ticket_id>/deferral
POST   /api/shipyard/inbound/manifests/<manifest_id>/arrival-slips
GET    /api/shipyard/inbound/arrival-slips/<arrival_slip_id>/file
```

顺延请求：

```json
{
  "next_batch_date": "2026-10-30",
  "reason": "车辆装不下，随下一批送达"
}
```

票货`delivery_status`为：

- `PENDING`：本批未到；
- `DEFERRED`：已顺延下批；
- `PARTIAL`：已到一部分；
- `RECEIVED`：已到齐。

顺延不会删除票货或改变计划数量。下一次实收时自动清除顺延标记。

四联到货验收单上传图片 `file`，并提交：

- `slip_no`；
- `delivery_note_no`；
- `supplier_name`；
- `engineering_no`；
- `purchase_order_no`；
- `is_urgent`；
- `factory_copy_count`，默认 `2`；
- `shipyard_copy_count`，默认 `2`；
- `warehouse_copy_retained`，默认 `true`。

系统把四联单原图、联次数量和接收人保存在入库清单下。

## 权限与审计

- 查询页面复用`shipyard-outbound`页面权限；
- 写入需要`SHIPYARD_MANAGER`或`OUTBOUND`业务角色；
- 每次导入、后台异常处理、生成出库任务、作废、顺延和四联单登记都写入
  审计日志；
- 导入与生成均受仓库访问权限约束。
