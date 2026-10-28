const { createIdempotencyKey } = require('./idempotency');

const PENDING_PREFIX = 'shipyard:submission:';
const DRAFT_PREFIX = 'shipyard:draft:';
const DRAFT_TTL_MS = 12 * 60 * 60 * 1000;

function _key(prefix, scope) {
  if (!scope) throw new Error('workflow scope is required');
  return `${prefix}${scope}`;
}

function _storage() {
  if (typeof wx === 'undefined') return null;
  return wx;
}

function getOrCreateSubmissionKey(scope, prefix) {
  const key = _key(PENDING_PREFIX, scope);
  const storage = _storage();
  if (!storage || typeof storage.getStorageSync !== 'function') {
    return createIdempotencyKey(prefix);
  }
  try {
    const existing = storage.getStorageSync(key);
    if (typeof existing === 'string' && existing) return existing;
  } catch {
    return createIdempotencyKey(prefix);
  }
  const created = createIdempotencyKey(prefix);
  try {
    storage.setStorageSync(key, created);
  } catch {
    // The operation can still proceed; it just cannot survive a process restart.
  }
  return created;
}

function clearSubmissionKey(scope) {
  const storage = _storage();
  if (!scope || !storage || typeof storage.removeStorageSync !== 'function') return;
  try {
    storage.removeStorageSync(_key(PENDING_PREFIX, scope));
  } catch {
    // Storage cleanup must never mask the business outcome.
  }
}

function isAmbiguousSubmissionError(error) {
  if (!error) return false;
  if (error.networkError || error.statusCode === 0) return true;
  if (error.statusCode === 408) return true;
  return Number(error.statusCode) >= 500;
}

function readDraft(scope) {
  const storage = _storage();
  if (!scope || !storage || typeof storage.getStorageSync !== 'function') {
    return null;
  }
  try {
    const draft = storage.getStorageSync(_key(DRAFT_PREFIX, scope));
    if (!draft || typeof draft !== 'object') return null;
    if (
      !Number.isFinite(draft.savedAt)
      || Date.now() - draft.savedAt > DRAFT_TTL_MS
    ) {
      clearDraft(scope);
      return null;
    }
    return draft;
  } catch {
    return null;
  }
}

function writeDraft(scope, draft) {
  const storage = _storage();
  if (!scope || !storage || typeof storage.setStorageSync !== 'function') return;
  try {
    storage.setStorageSync(_key(DRAFT_PREFIX, scope), {
      ...draft,
      savedAt: Date.now(),
    });
  } catch {
    // Draft recovery is best-effort; the submitted server state remains authoritative.
  }
}

function clearDraft(scope) {
  const storage = _storage();
  if (!scope || !storage || typeof storage.removeStorageSync !== 'function') return;
  try {
    storage.removeStorageSync(_key(DRAFT_PREFIX, scope));
  } catch {
    // Storage cleanup must never mask the business outcome.
  }
}

module.exports = {
  clearDraft,
  clearSubmissionKey,
  getOrCreateSubmissionKey,
  isAmbiguousSubmissionError,
  readDraft,
  writeDraft,
};
