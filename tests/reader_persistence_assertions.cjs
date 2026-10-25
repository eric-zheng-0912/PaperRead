const assert = require('node:assert/strict');

// Only reader navigation state may change; every other (including unknown) field stays exact.
function assertReaderPersistence(before, after, blockIds) {
  const stable = value => Object.fromEntries(Object.entries(value).filter(([key]) => !['progress', 'rev'].includes(key)));
  assert.deepEqual(stable(after), stable(before), 'reader human/unknown fields changed');
  assert.ok(Number.isInteger(after.rev) && after.rev >= before.rev, 'reader revision regressed');
  const p = after.progress;
  assert.ok(p && typeof p === 'object' && !Array.isArray(p));
  assert.ok(Object.keys(p).every(k => ['block', 'at', 'off', 'ratio'].includes(k)), 'unexpected progress field');
  if (Object.keys(p).length) {
    assert.ok(blockIds.includes(p.block), 'progress points outside the paper');
    assert.ok(typeof p.at === 'string' && Number.isFinite(Date.parse(p.at)), 'invalid progress timestamp');
    if (before.progress?.at) assert.ok(Date.parse(p.at) >= Date.parse(before.progress.at), 'progress timestamp regressed');
    for (const key of ['off', 'ratio']) if (key in p) assert.ok(Number.isFinite(p[key]) && p[key] >= 0 && p[key] <= 1, 'invalid '+key);
  }
}
module.exports = { assertReaderPersistence };
