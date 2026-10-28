# 贡献指南

## 开发环境

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

管理端热更新：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## 验证

所有测试必须使用独立测试库：

```powershell
docker exec sentry-api sh -c 'export TEST_DATABASE_URL="${DATABASE_URL%/*}/sentry_test"; python -m pytest tests/ -q'
```

提交前至少执行：

- 船厂定向 pytest；
- 后端全量 pytest；
- 管理端测试和生产构建；
- 小程序测试；
- Ruff、Bandit、`pip-audit`、`npm audit`；
- `git diff --check` 和 gitleaks。

## 规则

- 现场入口只保留微信小程序；
- 吊牌只拍照，不新增二维码、条码或扫码枪链路；
- OCR 结果不得直接写库存，必须经过服务端校验和人工确认；
- 库存写入必须幂等并带审计记录；
- 不提交密钥、令牌、客户敏感信息、照片、数据库或构建产物；
- 提交信息使用 Conventional Commits。

## 交付

业务分支使用 `codex/` 前缀。同一交付项保持一个规范分支和一个 PR；只有测试、
迁移、文档和风险状态均核验后才推进 `main`。真实 OCR、微信真机、UAT 或部署
未完成时，交付说明必须明确标记生产 `NO-GO`。
