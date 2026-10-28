# 船厂数据库备份恢复演练

本手册用于独立测试库或预发布库的备份、恢复、迁移重复执行和 API 启动演练。
它不是生产部署授权，也不能替代迁移窗口、双人复核、回滚审批或现场 UAT。

## 安全边界

- 默认源库只能是 `sentry_test`；禁止直接使用业务库或无标签的数据库；
- 每次创建两个唯一目标库：`sentry_drill_primary_<UTC时间>` 和
  `sentry_drill_restore_<UTC时间>`；
- 备份、恢复和删除都必须显式使用变量校验后的精确库名；
- 演练结束删除临时库，但把备份文件、SHA-256 和脱敏结果保存在仓库外；
- 数据库 URL、密码和连接串不得写入报告或 Git。

## 1. 创建备份

以下命令在仓库根目录的 PowerShell 中执行。所有命令都通过 Docker 容器运行，
宿主机不需要安装 PostgreSQL 客户端。

```powershell
$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$backup = "shipyard_recovery_$stamp.dump"
$dbPrimary = "sentry_drill_primary_$stamp"
$dbRestore = "sentry_drill_restore_$stamp"

docker exec sentry-db pg_dump -U sentry -d sentry_test `
  --format=custom --file "/tmp/$backup"

$hashLine = docker exec sentry-db sha256sum "/tmp/$backup"
$sha256 = ($hashLine -split ' ')[0]
$bytes = docker exec sentry-db stat -c %s "/tmp/$backup"
```

把备份复制到仓库外并核对宿主机哈希：

```powershell
$artifactDir = Join-Path $env:USERPROFILE 'shipyard-recovery-artifacts'
New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null
$artifact = Join-Path $artifactDir $backup
docker cp "sentry-db:/tmp/$backup" $artifact
(Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
```

## 2. 恢复到两个独立库

```powershell
$dbs = @($dbPrimary, $dbRestore)
foreach ($db in $dbs) {
  if ($db -notmatch '^sentry_drill_(primary|restore)_\d{14}$') {
    throw "临时库名不符合约定: $db"
  }
  docker exec sentry-db createdb -U sentry -T template0 $db
  docker exec sentry-db pg_restore -U sentry -d $db `
    --no-owner --no-privileges --exit-on-error "/tmp/$backup"
}
```

## 3. 核对结构、数据和审计链

至少核对以下不变量：

```powershell
$checkSql = @"
SELECT 'shipyard_tables', count(*)
  FROM information_schema.tables
 WHERE table_schema = 'public'
   AND table_name LIKE 'shipyard_%';
SELECT 'qr_columns', count(*)
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND column_name IN ('qr_payload', 'qr_payload_sha256');
SELECT 'legacy_085_tables', count(*)
  FROM information_schema.tables
 WHERE table_schema = 'public'
   AND table_name IN (
       'shipyard_delivery_lines',
       'shipyard_delivery_notes',
       'shipyard_dispatch_lines',
       'shipyard_dispatch_orders',
       'shipyard_distribution_lines',
       'shipyard_distribution_files',
       'shipyard_distribution_imports'
   );
SELECT 'audit_chain', verify_audit_log_chain();
"@

docker exec sentry-db psql -U sentry -d $dbPrimary -Atc $checkSql
docker exec sentry-db psql -U sentry -d $dbRestore -Atc $checkSql
```

生产前必须逐表比较源库和恢复库行数，而不只看总行数；空审计表返回 `NULL`
是 PostgreSQL 对空结果集函数的正常表现，源库和恢复库应一致。

## 4. 重复执行清理迁移

在恢复库上连续执行两轮 088 和 089：

```powershell
docker exec -e DRILL_DB=$dbPrimary sentry-api sh -c `
  'DRILL_URL="${DATABASE_URL%/*}/$DRILL_DB";
   psql "$DRILL_URL" -v ON_ERROR_STOP=1 -f /db/migrations/088_shipyard_remove_legacy_qr.sql;
   psql "$DRILL_URL" -v ON_ERROR_STOP=1 -f /db/migrations/089_shipyard_remove_distribution_dispatch.sql;
   psql "$DRILL_URL" -v ON_ERROR_STOP=1 -f /db/migrations/088_shipyard_remove_legacy_qr.sql;
   psql "$DRILL_URL" -v ON_ERROR_STOP=1 -f /db/migrations/089_shipyard_remove_distribution_dispatch.sql'
```

## 5. 用恢复库启动 API

在一个独立 Python 进程中临时覆盖 `DATABASE_URL`，确认应用可以启动、健康接口可用、
受保护船厂路由仍要求认证：

```powershell
docker exec -e DRILL_DB=$dbPrimary sentry-api sh -c `
  'export DATABASE_URL="${DATABASE_URL%/*}/$DRILL_DB"
   export FLASK_ENV=test
   export SENTRY_ALLOW_HTTP_WEBHOOKS=false
   export SENTRY_ALLOW_INTERNAL_WEBHOOKS=false
   export DISPATCHER_ENABLED=false
   python -c "
from app import create_app
app = create_app()
client = app.test_client()
assert client.get(\"/api/health\").status_code == 200
assert client.get(\"/api/shipyard/inbound/manifests?warehouse_id=1\").status_code == 401
print(\"recovery API boot OK\")
"'
```

对 `$dbRestore` 重复同一启动检查，确认第二份从原始备份恢复的数据库同样可用。

## 6. 清理

清理前再次核对名称。只删除本手册创建的临时库和容器内临时备份：

```powershell
foreach ($db in $dbs) {
  if ($db -notmatch '^sentry_drill_(primary|restore)_\d{14}$') {
    throw "拒绝删除意外库名: $db"
  }
  docker exec sentry-db dropdb -U sentry --if-exists $db
}
docker exec sentry-db rm -f "/tmp/$backup"
docker exec sentry-db psql -U sentry -d postgres -Atc `
  "SELECT datname FROM pg_database WHERE datname LIKE 'sentry%';"
```

## 2026-10-29 本地演练记录

| 项目 | 结果 |
| --- | --- |
| 源库 | `sentry_test`，不是生产库 |
| 备份格式 | PostgreSQL custom dump |
| 备份大小 | `453,935` bytes |
| 备份 SHA-256 | `ea9c16f6e3fad2a09e26fb7be1c37b1490fb69072a43b25d35ab3970554401a4` |
| 恢复库 | 两个独立 `sentry_drill_*` 数据库 |
| 船厂表数量 | 源库和两个恢复库均为 21 |
| 船厂表总行数 | 源库和两个恢复库均为 16，逐表计数一致 |
| 二维码旧列/索引 | 均为 0 |
| 085 旧表 | 均为 0 |
| 审计链 | 三个库均为 `NULL`，因为测试库审计表为空且结果一致 |
| 088/089 | 在恢复库连续执行两轮成功 |
| API 启动 | 两个恢复库均为健康检查 `200`、受保护船厂路由 `401` |
| 清理 | 两个临时库和容器内临时 dump 已删除 |
| 备份保留 | 仓库外 `../recovery-artifacts/shipyard_recovery_20261029004512.dump`，宿主哈希一致 |

本记录证明本地隔离环境具备可重复的备份恢复和迁移重放路径；不证明生产网络、
真实业务窗口、双人复核、生产回滚审批或船厂 UAT 已完成。
