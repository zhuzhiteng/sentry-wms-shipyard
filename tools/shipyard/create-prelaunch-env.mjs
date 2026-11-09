#!/usr/bin/env node

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

function secret(prefix = '', bytes = 32) {
  return `${prefix}${crypto.randomBytes(bytes).toString('hex')}`;
}

function parseArgs(argv) {
  let output = '';
  let force = false;
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--output') output = argv[++index];
    else if (argv[index] === '--force') force = true;
    else throw new Error(`未知参数: ${argv[index]}`);
  }
  if (!output) throw new Error('必须提供 --output');
  return { output: path.resolve(output), force };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (
    args.output === path.parse(args.output).root
    || args.output === path.resolve(REPO_ROOT)
  ) {
    throw new Error('拒绝把预发布环境文件写到文件系统根目录或仓库根目录');
  }
  if (fs.existsSync(args.output) && !args.force) {
    throw new Error(`输出文件已存在: ${args.output}`);
  }
  const values = {
    POSTGRES_USER: 'sentry_app',
    POSTGRES_PASSWORD: secret('db-', 24),
    POSTGRES_DB: 'sentry',
    ADMIN_PASSWORD: secret('admin-', 24),
    JWT_SECRET: secret(),
    SENTRY_ENCRYPTION_KEY: crypto.randomBytes(32).toString('base64'),
    SENTRY_TOKEN_PEPPER: secret(),
    SENTRY_PUBSUB_HMAC_KEY: secret(),
    REDIS_PASSWORD: secret('redis-', 24),
    TRUST_PROXY: 'true',
    API_BIND_HOST: '127.0.0.1',
    ADMIN_BIND_HOST: '127.0.0.1',
    CORS_ORIGINS: 'https://prelaunch.corp.test',
    SHIPYARD_OCR_PROVIDER: 'rapidocr',
    SHIPYARD_OCR_FALLBACK_PROVIDER: '',
    SHIPYARD_OCR_REQUIRED_FIELDS: 'material_code,item_name,tag_suffix',
    SHIPYARD_OCR_TOKEN_TIMEOUT_S: '10',
    SHIPYARD_OCR_TIMEOUT_S: '20',
    SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS: '90',
    SENTRY_INBOUND_MAX_BODY_KB: '256',
    SENTRY_OPS_ENVIRONMENT: 'production',
    SENTRY_OPS_ALERT_WEBHOOK_URL: 'https://alerts.prelaunch.corp.test/sentry-wms',
    SENTRY_OPS_ALERT_BEARER_TOKEN: '',
    SENTRY_OPS_ALLOW_HTTP_ALERTS: 'false',
    SENTRY_OPS_INTERVAL_S: '10',
    SENTRY_OPS_TIMEOUT_S: '5',
    SENTRY_OPS_FAILURE_THRESHOLD: '2',
    SENTRY_OPS_REPEAT_S: '3600',
    SENTRY_OPS_BACKUP_MAX_AGE_HOURS: '30',
    SENTRY_BACKUP_SCHEDULE_UTC: '02:30',
    SENTRY_BACKUP_RETENTION_COUNT: '14',
    SENTRY_BACKUP_TIMEOUT_S: '1800',
    SENTRY_BACKUP_RETRY_S: '900',
    SENTRY_BACKUP_RUN_ON_START: 'true',
  };
  fs.mkdirSync(path.dirname(args.output), { recursive: true });
  const content = [
    '# Generated local-only production-shape rehearsal credentials.',
    '# Never use these values in a real deployment.',
    ...Object.entries(values).map(([key, value]) => `${key}=${value}`),
    '',
  ].join('\n');
  fs.writeFileSync(args.output, content, { encoding: 'utf8', mode: 0o600 });
  process.stdout.write(`Prelaunch env written to ${args.output}\n`);
}

try {
  main();
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 2;
}
