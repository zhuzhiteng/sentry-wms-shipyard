#!/usr/bin/env node

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

const REQUIRED_ENV = [
  'POSTGRES_USER',
  'POSTGRES_PASSWORD',
  'POSTGRES_DB',
  'ADMIN_PASSWORD',
  'JWT_SECRET',
  'SENTRY_ENCRYPTION_KEY',
  'SENTRY_TOKEN_PEPPER',
  'SENTRY_PUBSUB_HMAC_KEY',
  'REDIS_PASSWORD',
  'CORS_ORIGINS',
  'SHIPYARD_OCR_PROVIDER',
  'SENTRY_OPS_ALERT_WEBHOOK_URL',
];

const SUPPORTED_OCR_PROVIDERS = new Set(['rapidocr', 'baidu']);

const REQUIRED_FILES = [
  'docker-compose.prod.yml',
  'docker-compose.ops.yml',
  'docker-compose.prelaunch.yml',
  'db/schema.sql',
  'db/migrations/090_shipyard_pickup_requests.sql',
  'api/scripts/shipyard_deployment_smoke.py',
  'miniprogram/app.json',
  'miniprogram/project.config.json',
];

const RELEASE_ENTRIES = [
  'app.js',
  'app.json',
  'app.wxss',
  'config.js',
  'pages',
  'project.config.json',
  'sitemap.json',
  'utils',
];

function finding(id, severity, message, remediation = '') {
  return { id, severity, message, remediation };
}

function boolValue(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value || '').trim().toLowerCase());
}

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

export function parseDotenv(text) {
  const values = {};
  const duplicateKeys = [];
  for (const rawLine of String(text || '').replace(/^\uFEFF/, '').split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.startsWith('export ')) line = line.slice(7).trimStart();
    const separator = line.indexOf('=');
    if (separator <= 0) continue;
    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if (
      value.length >= 2
      && ((value.startsWith('"') && value.endsWith('"'))
        || (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    }
    if (Object.hasOwn(values, key)) duplicateKeys.push(key);
    values[key] = value;
  }
  return { values, duplicateKeys };
}

function validFernetKey(value) {
  try {
    return Buffer.from(value, 'base64').length === 32;
  } catch {
    return false;
  }
}

function looksPlaceholder(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return (
    !normalized
    || normalized.includes('replace')
    || normalized.includes('change-me')
    || normalized.includes('your-')
    || normalized.includes('example.com')
    || normalized.includes('example.invalid')
  );
}

function validStrongSecret(value, minimumLength = 32) {
  const text = String(value || '').trim();
  return text.length >= minimumLength && !/\s/.test(text) && !looksPlaceholder(text);
}

function validApiBase(value) {
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === 'https:'
      && !['localhost', '127.0.0.1', '::1'].includes(parsed.hostname)
      && !looksPlaceholder(parsed.hostname)
      && !parsed.hostname.endsWith('.invalid')
      && parsed.pathname.replace(/\/+$/, '') === '/api'
      && !parsed.search
      && !parsed.hash
    );
  } catch {
    return false;
  }
}

function validWechatAppId(value) {
  return /^wx[a-zA-Z0-9]{16}$/.test(String(value || '').trim());
}

export function validateProductionEnv(values) {
  const findings = [];
  const env = values || {};

  for (const key of REQUIRED_ENV) {
    if (!String(env[key] || '').trim()) {
      findings.push(finding(
        `env.missing.${key}`,
        'error',
        `缺少生产配置 ${key}`,
        '复制 deploy/shipyard-production.env.example 到仓库外并填写正式值。',
      ));
    }
  }

  for (const key of ['POSTGRES_PASSWORD', 'ADMIN_PASSWORD', 'REDIS_PASSWORD']) {
    if (env[key] && !validStrongSecret(env[key], 16)) {
      findings.push(finding(
        `env.weak.${key}`,
        'error',
        `${key} 仍是占位值、过短或包含空白`,
        '使用独立生成的强密码，且不要与其它密钥复用。',
      ));
    }
  }

  for (const key of [
    'JWT_SECRET',
    'SENTRY_TOKEN_PEPPER',
    'SENTRY_PUBSUB_HMAC_KEY',
  ]) {
    if (env[key] && !validStrongSecret(env[key], 32)) {
      findings.push(finding(
        `env.weak.${key}`,
        'error',
        `${key} 不足 32 个非空白字符或仍是占位值`,
        '使用 openssl rand -hex 32 生成独立随机值。',
      ));
    }
  }

  if (env.SENTRY_ENCRYPTION_KEY && !validFernetKey(env.SENTRY_ENCRYPTION_KEY)) {
    findings.push(finding(
      'env.invalid.SENTRY_ENCRYPTION_KEY',
      'error',
      'SENTRY_ENCRYPTION_KEY 不是合法的 32 字节 Fernet 密钥',
      '用 cryptography.fernet.Fernet.generate_key() 生成，不能轮换后丢失旧值。',
    ));
  }

  const secrets = [
    env.POSTGRES_PASSWORD,
    env.ADMIN_PASSWORD,
    env.JWT_SECRET,
    env.SENTRY_TOKEN_PEPPER,
    env.SENTRY_PUBSUB_HMAC_KEY,
    env.REDIS_PASSWORD,
    env.BAIDU_OCR_API_KEY,
    env.BAIDU_OCR_SECRET_KEY,
  ].filter(Boolean);
  if (new Set(secrets).size !== secrets.length) {
    findings.push(finding(
      'env.reused-secret',
      'error',
      '发现多个生产密钥使用了相同值',
      '数据库、管理员、JWT、token pepper、Redis 和 OCR 凭据必须分别生成。',
    ));
  }

  if (String(env.POSTGRES_USER || '').trim().toLowerCase() === 'postgres') {
    findings.push(finding(
      'env.database-superuser',
      'error',
      'POSTGRES_USER 不能使用 postgres 超级用户',
      '使用专用数据库账号名，例如 sentry_app。',
    ));
  }
  if (['postgres', 'template0', 'template1'].includes(String(env.POSTGRES_DB || '').toLowerCase())) {
    findings.push(finding(
      'env.database-name',
      'error',
      'POSTGRES_DB 使用了系统数据库名',
      '使用独立业务库名，例如 sentry。',
    ));
  }

  if (String(env.TRUST_PROXY || '').toLowerCase() !== 'true') {
    findings.push(finding(
      'env.proxy',
      'error',
      'TRUST_PROXY 必须为 true',
      '正式部署必须位于受控 HTTPS 反向代理后，并只信任该代理转发头。',
    ));
  }
  for (const key of ['API_BIND_HOST', 'ADMIN_BIND_HOST']) {
    if (String(env[key] || '') !== '127.0.0.1') {
      findings.push(finding(
        `env.bind.${key}`,
        'error',
        `${key} 必须绑定 127.0.0.1`,
        '公网入口只能通过受控 HTTPS 反向代理开放。',
      ));
    }
  }

  const origins = String(env.CORS_ORIGINS || '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  if (!origins.length || origins.some((origin) => {
    try {
      const parsed = new URL(origin);
      return (
        parsed.protocol !== 'https:'
        || parsed.username
        || parsed.password
        || looksPlaceholder(parsed.hostname)
        || parsed.search
        || parsed.hash
        || (parsed.pathname !== '/' && parsed.pathname !== '')
      );
    } catch {
      return true;
    }
  })) {
    findings.push(finding(
      'env.cors',
      'error',
      'CORS_ORIGINS 必须只包含正式 HTTPS 站点来源',
      '例如 https://wms-admin.corp.example；不能使用 *、localhost 或 HTTP。',
    ));
  }

  const ocrProvider = String(env.SHIPYARD_OCR_PROVIDER || '').trim().toLowerCase();
  const ocrFallback = String(env.SHIPYARD_OCR_FALLBACK_PROVIDER || '')
    .trim()
    .toLowerCase();
  if (ocrProvider && !SUPPORTED_OCR_PROVIDERS.has(ocrProvider)) {
    findings.push(finding(
      'env.ocr-provider',
      'error',
      `不支持的 SHIPYARD_OCR_PROVIDER: ${ocrProvider}`,
      '使用 rapidocr（默认本地识别）或 baidu；确定性 stub 不能用于正式环境。',
    ));
  }
  if (ocrFallback && !SUPPORTED_OCR_PROVIDERS.has(ocrFallback)) {
    findings.push(finding(
      'env.ocr-fallback-provider',
      'error',
      `不支持的 SHIPYARD_OCR_FALLBACK_PROVIDER: ${ocrFallback}`,
      '留空表示不启用兜底，或配置为 rapidocr / baidu。',
    ));
  }
  if (ocrFallback && ocrFallback === ocrProvider) {
    findings.push(finding(
      'env.ocr-fallback-same',
      'error',
      '主 OCR 与兜底 OCR 不能使用同一个服务',
      '留空关闭兜底，或改用另一个 provider。',
    ));
  }
  const baiduRequired = ocrProvider === 'baidu' || ocrFallback === 'baidu';
  const baiduCredentialKeys = baiduRequired
    ? ['BAIDU_OCR_API_KEY', 'BAIDU_OCR_SECRET_KEY']
    : [];
  for (const key of baiduCredentialKeys) {
    if (!String(env[key] || '').trim()) {
      findings.push(finding(
        `env.missing.${key}`,
        'error',
        `启用百度 OCR 时缺少 ${key}`,
        '注入百度智能云 OCR 应用的正式凭据，或关闭百度主/兜底配置。',
      ));
    } else if (looksPlaceholder(env[key])) {
      findings.push(finding(
        `env.ocr-placeholder.${key}`,
        'error',
        `${key} 尚未替换为正式凭据`,
        '在百度智能云创建 OCR 应用并在部署主机注入正式凭据。',
      ));
    }
  }

  try {
    const webhook = new URL(String(env.SENTRY_OPS_ALERT_WEBHOOK_URL || ''));
    if (
      webhook.protocol !== 'https:'
      || looksPlaceholder(webhook.hostname)
      || webhook.username
      || webhook.password
    ) {
      throw new Error('invalid webhook');
    }
  } catch {
    findings.push(finding(
      'env.alert-webhook',
      'error',
      'SENTRY_OPS_ALERT_WEBHOOK_URL 必须是真实 HTTPS 告警地址',
      '配置 Teams、企业告警网关或其它可接收 JSON text 字段的 HTTPS webhook。',
    ));
  }

  if (String(env.SENTRY_OPS_ENVIRONMENT || 'production') !== 'production') {
    findings.push(finding(
      'env.ops-environment',
      'error',
      'SENTRY_OPS_ENVIRONMENT 必须为 production',
      '避免测试告警混入或正式告警被误标为开发环境。',
    ));
  }
  if (boolValue(env.SENTRY_OPS_ALLOW_HTTP_ALERTS)) {
    findings.push(finding(
      'env.http-alert',
      'error',
      '生产环境禁止 SENTRY_OPS_ALLOW_HTTP_ALERTS=true',
      '告警端点必须使用 HTTPS。',
    ));
  }
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(String(env.SENTRY_BACKUP_SCHEDULE_UTC || ''))) {
    findings.push(finding(
      'env.backup-schedule',
      'error',
      'SENTRY_BACKUP_SCHEDULE_UTC 必须是有效的 UTC HH:MM',
      '例如 02:30。',
    ));
  }
  if (Number(env.SENTRY_BACKUP_RETENTION_COUNT || 0) < 7) {
    findings.push(finding(
      'env.backup-retention',
      'error',
      'SENTRY_BACKUP_RETENTION_COUNT 至少为 7',
      '生产至少保留一周每日备份，建议 14 份。',
    ));
  }
  if (!boolValue(env.SENTRY_BACKUP_RUN_ON_START)) {
    findings.push(finding(
      'env.backup-start',
      'error',
      'SENTRY_BACKUP_RUN_ON_START 必须为 true',
      '首次上线必须立即生成首份备份，不能等待次日计划。',
    ));
  }

  return findings;
}

export function validateArtifacts(repoRoot = REPO_ROOT) {
  const findings = [];
  for (const relativePath of REQUIRED_FILES) {
    if (!fs.existsSync(path.join(repoRoot, relativePath))) {
      findings.push(finding(
        `artifact.missing.${relativePath}`,
        'error',
        `发布所需文件不存在: ${relativePath}`,
        '恢复该文件后再构建发布包。',
      ));
    }
  }
  return findings;
}

export function validateProductionBootstrap(repoRoot = REPO_ROOT) {
  const findings = [];
  const composePath = path.join(repoRoot, 'docker-compose.prod.yml');
  const seedPath = path.join(repoRoot, 'db/seed.sh');
  if (!fs.existsSync(composePath) || !fs.existsSync(seedPath)) return findings;
  const compose = fs.readFileSync(composePath, 'utf8');
  const seed = fs.readFileSync(seedPath, 'utf8');
  for (const required of [
    'docker-entrypoint-initdb.d/01-schema.sql',
    'docker-entrypoint-initdb.d/02-minimal-seed.sh',
    './db/migrations:/migrations:ro',
    './db/verify-shipyard-go-live.sql:/seed-data/verify-shipyard-go-live.sql:ro',
    'ADMIN_PASSWORD: ${ADMIN_PASSWORD:?Set ADMIN_PASSWORD in .env}',
    'POSTGRES_INITDB_ARGS: "--data-checksums"',
  ]) {
    if (!compose.includes(required)) {
      findings.push(finding(
        'production.bootstrap',
        'error',
        `生产 Compose 缺少首次建库保障: ${required}`,
        '确保新数据卷会加载 schema、最小管理员种子和数据校验和。',
      ));
    }
  }
  if (
    seed.includes('echo "  Admin password: $ADMIN_PW"')
    || !seed.includes('Admin password: supplied through ADMIN_PASSWORD')
  ) {
    findings.push(finding(
      'production.seed-log-secret',
      'error',
      '数据库初始化仍可能把管理员密码写入容器日志',
      '只输出密码来源，不输出实际密码。',
    ));
  }
  return findings;
}

export function checkGitState(repoRoot = REPO_ROOT) {
  const findings = [];
  const commit = spawnSync('git', ['rev-parse', '--verify', 'HEAD'], {
    cwd: repoRoot,
    encoding: 'utf8',
  });
  const status = spawnSync('git', ['status', '--porcelain'], {
    cwd: repoRoot,
    encoding: 'utf8',
  });
  if (commit.status !== 0 || !commit.stdout.trim()) {
    findings.push(finding(
      'git.no-commit',
      'error',
      '仓库没有可追溯的 Git 提交',
      '正式发布必须从已提交、已通过 CI 的固定 commit 构建。',
    ));
    return { findings, commit: '', clean: false };
  }
  const clean = status.status === 0 && !status.stdout.trim();
  if (!clean) {
    findings.push(finding(
      'git.dirty',
      'error',
      '工作区存在未提交改动，不能作为正式发布源',
      '完成评审和测试后按交付流程提交，从干净 commit 重新构建。',
    ));
  }
  return { findings, commit: commit.stdout.trim(), clean };
}

function copyEntry(source, destination) {
  const stat = fs.statSync(source);
  if (stat.isDirectory()) {
    fs.mkdirSync(destination, { recursive: true });
    for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
      copyEntry(path.join(source, entry.name), path.join(destination, entry.name));
    }
    return;
  }
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.copyFileSync(source, destination);
}

function listFiles(root) {
  const files = [];
  function walk(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(absolute);
      else files.push(absolute);
    }
  }
  walk(root);
  return files.sort();
}

function readVersion(repoRoot) {
  const apiVersion = fs.readFileSync(path.join(repoRoot, 'api/version.py'), 'utf8');
  const adminPackage = JSON.parse(
    fs.readFileSync(path.join(repoRoot, 'admin/package.json'), 'utf8'),
  );
  const match = apiVersion.match(/__version__\s*=\s*["']([^"']+)["']/);
  return {
    api: match?.[1] || 'unknown',
    admin: adminPackage.version || 'unknown',
  };
}

export function prepareMiniprogramRelease({
  repoRoot = REPO_ROOT,
  outputDir,
  appId,
  apiBase,
  force = false,
}) {
  if (!validWechatAppId(appId)) {
    throw new Error('小程序 AppID 格式无效；正式使用 wx 开头的 18 位 AppID');
  }
  if (!validApiBase(apiBase)) {
    throw new Error('小程序 API 地址必须是 HTTPS 且路径为 /api');
  }
  const sourceDir = path.join(repoRoot, 'miniprogram');
  const targetDir = path.resolve(outputDir);
  const effectiveTarget = fs.existsSync(targetDir)
    ? fs.realpathSync(targetDir)
    : targetDir;
  const git = checkGitState(repoRoot);
  if (
    effectiveTarget === path.parse(effectiveTarget).root
    || effectiveTarget === path.resolve(repoRoot)
    || effectiveTarget === path.resolve(sourceDir)
  ) {
    throw new Error('拒绝使用仓库根目录、小程序源码目录或文件系统根目录作为发布目录');
  }
  if (fs.existsSync(targetDir)) {
    if (!force) throw new Error(`发布目录已存在: ${targetDir}`);
    fs.rmSync(targetDir, { recursive: true, force: true });
  }
  fs.mkdirSync(targetDir, { recursive: true });
  for (const entry of RELEASE_ENTRIES) {
    const source = path.join(sourceDir, entry);
    if (!fs.existsSync(source)) throw new Error(`小程序发布文件不存在: ${entry}`);
    copyEntry(source, path.join(targetDir, entry));
  }

  fs.writeFileSync(
    path.join(targetDir, 'config.js'),
    `module.exports = {\n`
      + `  defaultApiBase: ${JSON.stringify(apiBase.replace(/\/+$/, ''))},\n`
      + `  sessionTimeoutMs: 8 * 60 * 60 * 1000,\n`
      + `};\n`,
    'utf8',
  );

  const projectPath = path.join(targetDir, 'project.config.json');
  const project = JSON.parse(fs.readFileSync(projectPath, 'utf8'));
  project.appid = appId;
  project.compileType = 'miniprogram';
  project.miniprogramRoot = './';
  project.projectname = 'sentry-shipyard-wms';
  project.setting = {
    ...(project.setting || {}),
    urlCheck: true,
    minified: true,
    es6: true,
    enhance: true,
    postcss: true,
  };
  fs.writeFileSync(projectPath, `${JSON.stringify(project, null, 2)}\n`, 'utf8');

  const files = listFiles(targetDir);
  const hashes = Object.fromEntries(files.map((absolute) => {
    const relative = path.relative(targetDir, absolute).replaceAll(path.sep, '/');
    return [relative, sha256(fs.readFileSync(absolute))];
  }));
  const versions = readVersion(repoRoot);
  const manifest = {
    schema_version: 1,
    release_mode: 'production',
    generated_at: new Date().toISOString(),
    app_id: appId,
    api_origin: new URL(apiBase).origin,
    api_base: apiBase.replace(/\/+$/, ''),
    versions,
    source_commit: git.commit,
    source_clean: git.clean,
    files: hashes,
  };
  fs.writeFileSync(
    path.join(targetDir, 'release-manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8',
  );
  return {
    outputDir: targetDir,
    manifest,
    manifestSha256: sha256(fs.readFileSync(path.join(targetDir, 'release-manifest.json'))),
  };
}

export function validateReleasePackage(releaseDir) {
  const findings = [];
  const projectPath = path.join(releaseDir, 'project.config.json');
  const configPath = path.join(releaseDir, 'config.js');
  const manifestPath = path.join(releaseDir, 'release-manifest.json');
  if (!fs.existsSync(projectPath) || !fs.existsSync(configPath) || !fs.existsSync(manifestPath)) {
    findings.push(finding(
      'release.missing',
      'error',
      '小程序发布包不完整',
      '重新运行 release-preflight.mjs 生成发布包。',
    ));
    return findings;
  }
  const project = JSON.parse(fs.readFileSync(projectPath, 'utf8'));
  const config = fs.readFileSync(configPath, 'utf8');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  if (!validWechatAppId(project.appid)) {
    findings.push(finding(
      'release.appid',
      'error',
      '发布包仍使用测试 AppID',
      '注入正式微信小程序 AppID。',
    ));
  }
  if (project.setting?.urlCheck !== true) {
    findings.push(finding(
      'release.url-check',
      'error',
      '发布包未开启微信合法域名校验',
      '正式发布必须设置 setting.urlCheck=true。',
    ));
  }
  const apiMatch = config.match(/defaultApiBase:\s*["']([^"']+)["']/);
  if (!apiMatch || !validApiBase(apiMatch[1])) {
    findings.push(finding(
      'release.api-base',
      'error',
      '发布包 API 地址不是正式 HTTPS /api 地址',
      '重新生成发布包并注入正式 API 根地址。',
    ));
  }
  if (manifest.schema_version !== 1 || !manifest.files || !manifest.source_commit) {
    findings.push(finding(
      'release.manifest',
      'error',
      '发布清单缺少版本、来源提交或文件哈希',
      '重新生成发布包，禁止手工修改生产包。',
    ));
  }
  if (manifest.source_clean !== true) {
    findings.push(finding(
      'release.dirty-source',
      'error',
      '发布包来自脏工作区',
      '从已提交且通过 CI 的固定 commit 重新生成。',
    ));
  }
  return findings;
}

function runComposeConfig(envFile) {
  const result = spawnSync('docker', [
    'compose',
    '--env-file',
    envFile,
    '-f',
    'docker-compose.prod.yml',
    '-f',
    'docker-compose.ops.yml',
    'config',
    '--quiet',
  ], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  });
  if (result.status !== 0) {
    return [finding(
      'compose.invalid',
      'error',
      `生产 Compose 配置校验失败: ${(result.stderr || result.stdout || '').trim().slice(0, 500)}`,
      '修复环境变量或 Compose 配置后重试。',
    )];
  }
  return [];
}

function writeReport(reportDir, report) {
  fs.mkdirSync(reportDir, { recursive: true });
  const jsonPath = path.join(reportDir, 'shipyard-release-preflight.json');
  const markdownPath = path.join(reportDir, 'shipyard-release-preflight.md');
  fs.writeFileSync(jsonPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  const lines = [
    '# 船厂仓储发布预检',
    '',
    `- 生成时间：${report.generated_at}`,
    `- 结论：${report.ready ? 'READY' : 'BLOCKED'}`,
    `- Git 提交：${report.git.commit || '无'}`,
    `- 工作区干净：${report.git.clean ? '是' : '否'}`,
    '',
    '## 检查项',
    '',
    '| ID | 级别 | 结果 | 处理建议 |',
    '| --- | --- | --- | --- |',
    ...report.findings.map((item) => (
      `| ${item.id} | ${item.severity} | ${item.message} | ${item.remediation || ''} |`
    )),
  ];
  if (!report.findings.length) {
    lines.push('| all | info | 全部通过 | 可进入部署审批 |');
  }
  fs.writeFileSync(markdownPath, `${lines.join('\n')}\n`, 'utf8');
  return { jsonPath, markdownPath };
}

function parseArgs(argv) {
  const args = {
    envFile: '',
    appId: '',
    apiBase: '',
    outputDir: '',
    reportDir: '',
    skipCompose: false,
    force: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === '--env-file') args.envFile = argv[++index];
    else if (value === '--app-id') args.appId = argv[++index];
    else if (value === '--api-base') args.apiBase = argv[++index];
    else if (value === '--output-dir') args.outputDir = argv[++index];
    else if (value === '--report-dir') args.reportDir = argv[++index];
    else if (value === '--skip-compose') args.skipCompose = true;
    else if (value === '--force') args.force = true;
    else throw new Error(`未知参数: ${value}`);
  }
  if (!args.envFile || !args.appId || !args.apiBase || !args.outputDir) {
    throw new Error(
      '必须提供 --env-file、--app-id、--api-base 和 --output-dir',
    );
  }
  return args;
}

export function runPreflight({
  repoRoot = REPO_ROOT,
  envFile,
  appId,
  apiBase,
  outputDir,
  reportDir = '',
  skipCompose = false,
  force = false,
}) {
  const envPath = path.resolve(envFile);
  if (!fs.existsSync(envPath)) throw new Error(`环境文件不存在: ${envPath}`);
  const parsed = parseDotenv(fs.readFileSync(envPath, 'utf8'));
  const git = checkGitState(repoRoot);
  const findings = [
    ...validateProductionEnv(parsed.values),
    ...validateArtifacts(repoRoot),
    ...validateProductionBootstrap(repoRoot),
    ...git.findings,
  ];
  if (parsed.duplicateKeys.length) {
    findings.push(finding(
      'env.duplicates',
      'error',
      `环境文件存在重复键: ${[...new Set(parsed.duplicateKeys)].join(', ')}`,
      '删除重复配置，避免 Compose 与预检读取到不同值。',
    ));
  }
  if (!skipCompose) findings.push(...runComposeConfig(envPath));

  let release = null;
  if (!findings.some((item) => item.severity === 'error')) {
    release = prepareMiniprogramRelease({
      repoRoot,
      outputDir,
      appId,
      apiBase,
      force,
    });
    findings.push(...validateReleasePackage(release.outputDir));
  }

  const report = {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    ready: !findings.some((item) => item.severity === 'error'),
    environment: parsed.values.SENTRY_OPS_ENVIRONMENT || 'unknown',
    git,
    release: release
      ? {
        output_dir: release.outputDir,
        manifest_sha256: release.manifestSha256,
        api_origin: release.manifest.api_origin,
        versions: release.manifest.versions,
      }
      : null,
    findings,
  };
  if (reportDir) {
    report.report_files = writeReport(path.resolve(reportDir), report);
  }
  return report;
}

async function main() {
  try {
    const args = parseArgs(process.argv.slice(2));
    const report = runPreflight(args);
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    process.exitCode = report.ready ? 0 : 1;
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
