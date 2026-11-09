import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  parseDotenv,
  prepareMiniprogramRelease,
  validateProductionBootstrap,
  validateProductionEnv,
  validateReleasePackage,
} from './release-preflight.mjs';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

function validEnv() {
  return {
    POSTGRES_USER: 'sentry_app',
    POSTGRES_PASSWORD: `db-${crypto.randomBytes(24).toString('hex')}`,
    POSTGRES_DB: 'sentry',
    ADMIN_PASSWORD: `admin-${crypto.randomBytes(24).toString('hex')}`,
    JWT_SECRET: crypto.randomBytes(32).toString('hex'),
    SENTRY_ENCRYPTION_KEY: crypto.randomBytes(32).toString('base64'),
    SENTRY_TOKEN_PEPPER: crypto.randomBytes(32).toString('hex'),
    SENTRY_PUBSUB_HMAC_KEY: crypto.randomBytes(32).toString('hex'),
    REDIS_PASSWORD: `redis-${crypto.randomBytes(24).toString('hex')}`,
    TRUST_PROXY: 'true',
    API_BIND_HOST: '127.0.0.1',
    ADMIN_BIND_HOST: '127.0.0.1',
    CORS_ORIGINS: 'https://wms.corp.test',
    SHIPYARD_OCR_PROVIDER: 'rapidocr',
    SHIPYARD_OCR_FALLBACK_PROVIDER: '',
    SENTRY_OPS_ENVIRONMENT: 'production',
    SENTRY_OPS_ALERT_WEBHOOK_URL: 'https://alerts.corp.test/sentry-wms',
    SENTRY_OPS_ALLOW_HTTP_ALERTS: 'false',
    SENTRY_BACKUP_SCHEDULE_UTC: '02:30',
    SENTRY_BACKUP_RETENTION_COUNT: '14',
    SENTRY_BACKUP_RUN_ON_START: 'true',
  };
}

function writeFile(root, relativePath, content) {
  const target = path.join(root, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, 'utf8');
}

function createFixtureRepo() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'shipyard-preflight-'));
  writeFile(root, 'api/version.py', '__version__ = "9.9.9"\n');
  writeFile(root, 'admin/package.json', '{"version":"9.9.8"}\n');
  writeFile(root, 'miniprogram/app.js', 'App({});\n');
  writeFile(root, 'miniprogram/app.json', '{"pages":["pages/login/index"]}\n');
  writeFile(root, 'miniprogram/app.wxss', 'page { color: #000; }\n');
  writeFile(
    root,
    'miniprogram/config.js',
    "module.exports = { defaultApiBase: 'http://127.0.0.1:5000/api' };\n",
  );
  writeFile(
    root,
    'miniprogram/project.config.json',
    JSON.stringify({
      appid: 'touristappid',
      compileType: 'miniprogram',
      miniprogramRoot: './',
      projectname: 'fixture',
      setting: { urlCheck: false },
    }),
  );
  writeFile(root, 'miniprogram/sitemap.json', '{}\n');
  writeFile(root, 'miniprogram/pages/login/index.js', 'Page({});\n');
  writeFile(root, 'miniprogram/utils/session.js', 'module.exports = {};\n');
  writeFile(root, 'miniprogram/tests/not-for-release.test.js', 'throw new Error("copy");\n');
  writeFile(root, 'miniprogram/README.md', '# dev only\n');

  for (const args of [
    ['init'],
    ['config', 'user.email', 'preflight@example.test'],
    ['config', 'user.name', 'Preflight Test'],
    ['add', '.'],
    ['commit', '-m', 'fixture'],
  ]) {
    const result = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
    assert.equal(result.status, 0, `${args.join(' ')}: ${result.stderr}`);
  }
  return root;
}

test('production environment validator accepts a complete independent secret set', () => {
  const findings = validateProductionEnv(validEnv());
  assert.deepEqual(
    findings.filter((item) => item.severity === 'error'),
    [],
  );
});

test('production environment validator requires Baidu credentials only when Baidu is enabled', () => {
  const localOnly = validEnv();
  assert.equal(
    validateProductionEnv(localOnly).some((item) => item.id.includes('BAIDU_OCR')),
    false,
  );

  const localWithFallback = {
    ...validEnv(),
    SHIPYARD_OCR_FALLBACK_PROVIDER: 'baidu',
  };
  const missing = validateProductionEnv(localWithFallback).map((item) => item.id);
  assert.ok(missing.includes('env.missing.BAIDU_OCR_API_KEY'));
  assert.ok(missing.includes('env.missing.BAIDU_OCR_SECRET_KEY'));

  const withFallbackCredentials = {
    ...localWithFallback,
    BAIDU_OCR_API_KEY: `ocr-key-${crypto.randomBytes(16).toString('hex')}`,
    BAIDU_OCR_SECRET_KEY: `ocr-secret-${crypto.randomBytes(16).toString('hex')}`,
  };
  assert.deepEqual(
    validateProductionEnv(withFallbackCredentials)
      .filter((item) => item.severity === 'error'),
    [],
  );
});

test('production environment validator rejects unsupported or duplicate OCR fallback', () => {
  const unsupported = {
    ...validEnv(),
    SHIPYARD_OCR_FALLBACK_PROVIDER: 'stub',
  };
  assert.ok(
    validateProductionEnv(unsupported).some(
      (item) => item.id === 'env.ocr-fallback-provider',
    ),
  );

  const duplicate = {
    ...validEnv(),
    SHIPYARD_OCR_FALLBACK_PROVIDER: 'rapidocr',
  };
  assert.ok(
    validateProductionEnv(duplicate).some(
      (item) => item.id === 'env.ocr-fallback-same',
    ),
  );
});

test('production environment validator blocks placeholders and insecure transport', () => {
  const env = {
    ...validEnv(),
    ADMIN_PASSWORD: 'REPLACE_WITH_ADMIN_PASSWORD',
    TRUST_PROXY: 'false',
    CORS_ORIGINS: 'http://localhost:8080',
    SENTRY_OPS_ALERT_WEBHOOK_URL: 'http://alerts.corp.test/hook',
    SENTRY_BACKUP_RETENTION_COUNT: '2',
  };
  const ids = validateProductionEnv(env).map((item) => item.id);
  assert.ok(ids.includes('env.weak.ADMIN_PASSWORD'));
  assert.ok(ids.includes('env.proxy'));
  assert.ok(ids.includes('env.cors'));
  assert.ok(ids.includes('env.alert-webhook'));
  assert.ok(ids.includes('env.backup-retention'));
});

test('dotenv parser reports duplicate keys without exposing values', () => {
  const parsed = parseDotenv('A=1\nB=2\nA=3\n');
  assert.equal(parsed.values.A, '3');
  assert.deepEqual(parsed.duplicateKeys, ['A']);
});

test('production bootstrap mounts schema and never logs the admin password', () => {
  assert.deepEqual(validateProductionBootstrap(REPO_ROOT), []);
});

test('release packaging injects production WeChat settings and excludes development tests', () => {
  const root = createFixtureRepo();
  const output = path.join(root, 'release-output');
  try {
    const release = prepareMiniprogramRelease({
      repoRoot: root,
      outputDir: output,
      appId: 'wx1234567890abcdef',
      apiBase: 'https://wms-api.corp.test/api',
    });
    const project = JSON.parse(
      fs.readFileSync(path.join(output, 'project.config.json'), 'utf8'),
    );
    const config = fs.readFileSync(path.join(output, 'config.js'), 'utf8');
    assert.equal(project.appid, 'wx1234567890abcdef');
    assert.equal(project.setting.urlCheck, true);
    assert.match(config, /https:\/\/wms-api\.corp\.test\/api/);
    assert.equal(fs.existsSync(path.join(output, 'tests')), false);
    assert.equal(fs.existsSync(path.join(output, 'README.md')), false);
    assert.equal(release.manifest.source_clean, true);
    assert.equal(release.manifest.versions.api, '9.9.9');
    assert.deepEqual(validateReleasePackage(output), []);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('release packaging rejects tourist AppID and HTTP API', () => {
  const root = createFixtureRepo();
  try {
    assert.throws(
      () => prepareMiniprogramRelease({
        repoRoot: root,
        outputDir: path.join(root, 'release-output'),
        appId: 'touristappid',
        apiBase: 'https://wms-api.corp.test/api',
      }),
      /AppID/,
    );
    assert.throws(
      () => prepareMiniprogramRelease({
        repoRoot: root,
        outputDir: path.join(root, 'release-output'),
        appId: 'wx1234567890abcdef',
        apiBase: 'http://127.0.0.1:5000/api',
      }),
      /HTTPS/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
