const test = require('node:test');
const assert = require('node:assert/strict');

const {
  chooseNextSuffix,
  normalizeSuffix,
} = require('../utils/pallet');

test('validates suffixes and chooses the next whole-ticket pallet', () => {
  assert.equal(normalizeSuffix('1-0'), '');
  assert.equal(normalizeSuffix('1-2'), '1-2');
  assert.equal(chooseNextSuffix(['1-1', '1-2'], ['1-1']), '1-2');
  assert.equal(chooseNextSuffix(['1-1'], ['1-1']), '');
});
