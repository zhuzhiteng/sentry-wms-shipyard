# 生产监控与备份自动化

## 目标与边界

`docker-compose.ops.yml` 在现有 API 镜像上增加两个独立 sidecar：

- `ops-monitor`：主动检查 API、PostgreSQL、Redis、Celery 和最近一次备份，
  在故障持续达到阈值后发送告警，恢复时发送恢复通知；
- `ops-backup`：按 UTC 时间执行 PostgreSQL custom-format 备份，校验归档、
  生成 SHA-256、执行保留策略，并把状态提供给监控 sidecar。

备份镜像固定基于 `postgres:16`，确保 `pg_dump` / `pg_restore` 与当前数据库
主版本一致。不能使用系统默认的最新版 PostgreSQL 客户端，否则新版归档头可能
无法被 PostgreSQL 16 的恢复工具读取。

该实现只证明代码、容器编排和自动测试就绪。启用实际 webhook、部署 sidecar、
生成第一份生产备份并做恢复演练，仍属于生产发布步骤，不能由本页替代。

## 启用

生产环境使用生产 Compose 加运维 overlay：

```powershell
docker compose `
  -f docker-compose.prod.yml `
  -f docker-compose.ops.yml `
  up -d
```

本地预发布验证可基于开发 Compose：

```powershell
docker compose `
  -f docker-compose.yml `
  -f docker-compose.ops.yml `
  up -d
```

启动顺序由 `ops-init` 保证：它创建并授权共享状态卷和备份卷，然后监控与备份
容器才启动。两个 sidecar 都以非 root 用户运行，根文件系统只读。

## 告警

至少配置：

```dotenv
SENTRY_OPS_ALERT_WEBHOOK_URL=https://your-alert-endpoint.example/hook
SENTRY_OPS_ALERT_BEARER_TOKEN=
SENTRY_OPS_ENVIRONMENT=production
```

请求体为：

```json
{"text": "[Sentry WMS][production] database is unhealthy: ..."}
```

这兼容 Teams incoming webhook，也适用于可接收通用 `text` 字段的告警网关。
Bearer token 可选且只能从环境变量注入。默认只允许 HTTPS；HTTP 仅限本地测试，
需要显式设置 `SENTRY_OPS_ALLOW_HTTP_ALERTS=true`。

告警策略：

- 连续失败达到 `SENTRY_OPS_FAILURE_THRESHOLD` 才首次告警，避免单次抖动；
- 持续故障每 `SENTRY_OPS_REPEAT_S` 提醒一次；
- 组件恢复后发送一次恢复通知；
- webhook 未配置时状态文件仍更新，日志明确提示不会发送外部通知。

监控结果位于共享状态卷：

```powershell
docker compose -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T ops-monitor cat /state/monitor-status.json
docker compose -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T ops-monitor cat /state/monitor.prom
```

`monitor.prom` 提供 `sentry_wms_component_up` 和
`sentry_wms_monitor_overall_up` 等指标快照，可在后续接入 Prometheus 时直接复用。

## 定时备份

默认配置：

```dotenv
SENTRY_BACKUP_SCHEDULE_UTC=02:30
SENTRY_BACKUP_RETENTION_COUNT=14
SENTRY_BACKUP_TIMEOUT_S=1800
SENTRY_BACKUP_RETRY_S=900
SENTRY_BACKUP_RUN_ON_START=true
SENTRY_OPS_BACKUP_MAX_AGE_HOURS=30
```

每次备份执行：

1. `pg_dump --format=custom --compress=9 --no-owner --no-privileges`；
2. `pg_restore --list` 验证归档可读；
3. 原子移动到最终文件；
4. 把备份权限收紧为 `0600`，生成同名 `.sha256` 和 `.json` 元数据；
5. 只保留最近 `SENTRY_BACKUP_RETENTION_COUNT` 份受管备份；
6. 写入 `/state/backup-status.json`，失败也会记录脱敏错误。

`SENTRY_BACKUP_RUN_ON_START=true` 会在 sidecar 每次启动时先生成一份备份，
然后按 UTC 计划执行；设置为 `false` 时只按计划运行。监控在最近备份超过
`SENTRY_OPS_BACKUP_MAX_AGE_HOURS` 或最近一次尝试失败时告警。

备份包含完整业务数据。宿主机备份卷必须使用磁盘加密和最小权限；复制到仓库外
存储前必须启用目标存储的静态加密和传输加密。当前实现不假定宿主机已经配置这两项。

手动触发并检查：

```powershell
docker compose -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T ops-backup python -m ops.backup --once
docker compose -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T ops-backup sh -c 'ls -lh /backups'
docker compose -f docker-compose.prod.yml -f docker-compose.ops.yml `
  exec -T ops-backup cat /state/backup-status.json
```

## 恢复验证

生产恢复必须在隔离的预发布库执行，不得直接覆盖生产库。至少完成：

1. 复制最新 `.dump`、`.sha256` 和 `.json` 到预发布主机；
2. 用 `Get-FileHash` 或 `sha256sum` 复核哈希；
3. 建立唯一 `sentry_restore_check_*` 临时库；
4. 用 `pg_restore --no-owner --no-privileges --exit-on-error` 恢复；
5. 比较船厂表数量、关键表行数、外键、审计链和 API 健康检查；
6. 记录耗时、命令、哈希和结果后删除临时库。

完整恢复演练步骤见 [`recovery-drill.md`](recovery-drill.md)。

## 本地验证记录

2026-10-29 在开发容器完成：

- `ops-monitor --once` 实际检查 API、PostgreSQL、Redis、Celery 和备份状态，
  结果为 `healthy=true`，heartbeat 健康检查退出 `0`；
- 固定 PostgreSQL 16 的 `ops-backup --once` 在只读根文件系统、非 root 用户下
  生成 `414,774` bytes、权限 `0600` 的备份，最终 SHA-256 为
  `19aa66274dd42ffdae8694def3b6739214011fc3ea755df99794b6a12c54df8e`；
- 同构归档在独立临时库完成 `pg_restore --exit-on-error`，恢复后
  `shipyard` 表 `28` 张、`audit_log` `10` 行、`shipyard_cargo_tickets` `1` 行，
  与源库一致；
- 临时恢复库、备份和状态目录已清理；
- 本地 HTTP sink 实际收到告警 POST 并返回 `2xx`，但生产 HTTPS webhook
  仍未配置或验收。

这些结果证明备份兼容性、恢复和告警发送代码可用，不证明生产环境已经部署。

## 上线前仍须完成

- 在预发布或生产环境实际配置 HTTPS 告警 webhook；
- 制造一次可控故障并确认收到故障与恢复通知；
- 完成首次按计划备份和一次隔离恢复；
- 把备份复制到与主机磁盘故障域隔离的仓库外存储；
- 记录监控告警、备份哈希、恢复耗时和失败处置演练。
