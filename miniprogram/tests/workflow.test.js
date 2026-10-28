const test = require('node:test');
const assert = require('node:assert/strict');

const {
  clearDraft,
  clearSubmissionKey,
  getOrCreateSubmissionKey,
  isAmbiguousSubmissionError,
  readDraft,
  writeDraft,
} = require('../utils/workflow');

function installStorage() {
  const values = new Map();
  global.wx = {
    getStorageSync: (key) => values.get(key),
    setStorageSync: (key, value) => values.set(key, value),
    removeStorageSync: (key) => values.delete(key),
  };
  return values;
}

test('reuses a pending submission key until the business action is resolved', () => {
  const values = installStorage();

  const first = getOrCreateSubmissionKey('receipt:receiver:ticket-1', 'receipt');
  const repeated = getOrCreateSubmissionKey(
    'receipt:receiver:ticket-1',
    'receipt',
  );

  assert.equal(first, repeated);
  assert.equal(values.size, 1);

  clearSubmissionKey('receipt:receiver:ticket-1');
  const next = getOrCreateSubmissionKey('receipt:receiver:ticket-1', 'receipt');
  assert.notEqual(first, next);
});

test('detects network and gateway failures as ambiguous write outcomes', () => {
  assert.equal(isAmbiguousSubmissionError({ networkError: true }), true);
  assert.equal(isAmbiguousSubmissionError({ statusCode: 0 }), true);
  assert.equal(isAmbiguousSubmissionError({ statusCode: 408 }), true);
  assert.equal(isAmbiguousSubmissionError({ statusCode: 502 }), true);
  assert.equal(isAmbiguousSubmissionError({ statusCode: 504 }), true);
  assert.equal(isAmbiguousSubmissionError({ statusCode: 409 }), false);
  assert.equal(isAmbiguousSubmissionError({ statusCode: 422 }), false);
});

test('persists and clears a page draft without changing its business shape', () => {
  const values = installStorage();
  const draft = {
    manifest_id: 'manifest-1',
    ticket_id: 'ticket-1',
    scans: [{ suffix: '1-1', photo: { photo_id: 'photo-1' } }],
    quantity: '2',
    difference_reason: '少 2 件',
    submit_uncertain: true,
  };

  writeDraft('receipt:receiver:1', draft);
  const restored = readDraft('receipt:receiver:1');

  assert.equal(restored.ticket_id, 'ticket-1');
  assert.deepEqual(restored.scans, draft.scans);
  assert.equal(restored.submit_uncertain, true);
  assert.ok(Number.isFinite(restored.savedAt));

  clearDraft('receipt:receiver:1');
  assert.equal(readDraft('receipt:receiver:1'), null);
  assert.equal(values.size, 0);
});
