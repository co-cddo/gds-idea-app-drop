import { test } from 'node:test';
import assert from 'node:assert/strict';

import { partByteRange, runWithConcurrency, sleep } from '../src/assets/upload-concurrency.js';

test('runWithConcurrency: runs the worker over every item', async () => {
  const seen = [];
  await runWithConcurrency([1, 2, 3, 4, 5], 2, async (item) => {
    seen.push(item);
  });

  assert.deepEqual(seen.sort(), [1, 2, 3, 4, 5]);
});

test('runWithConcurrency: never runs more than `limit` workers at once', async () => {
  let active = 0;
  let maxActive = 0;

  await runWithConcurrency(Array.from({ length: 10 }, (_, i) => i), 3, async () => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await sleep(1);
    active -= 1;
  });

  assert.ok(maxActive <= 3, `expected max 3 concurrent, saw ${maxActive}`);
});

test('runWithConcurrency: caps lane count at the number of items (limit > items)', async () => {
  let active = 0;
  let maxActive = 0;

  await runWithConcurrency([1, 2], 10, async () => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await sleep(1);
    active -= 1;
  });

  assert.ok(maxActive <= 2);
});

test('runWithConcurrency: propagates a worker rejection', async () => {
  await assert.rejects(
    () =>
      runWithConcurrency([1, 2, 3], 2, async (item) => {
        if (item === 2) {
          throw new Error('boom');
        }
      }),
    /boom/
  );
});

test('runWithConcurrency: handles an empty item list', async () => {
  let calls = 0;
  await runWithConcurrency([], 5, async () => {
    calls += 1;
  });
  assert.equal(calls, 0);
});

test('partByteRange: interior part spans a full partSize', () => {
  assert.deepEqual(partByteRange(2, 100, 250), { start: 100, end: 200 });
});

test('partByteRange: first part starts at zero', () => {
  assert.deepEqual(partByteRange(1, 100, 250), { start: 0, end: 100 });
});

test('partByteRange: last part is clipped to fileSize, not a full partSize', () => {
  assert.deepEqual(partByteRange(3, 100, 250), { start: 200, end: 250 });
});

test('partByteRange: exact multiple - last part is still a full partSize', () => {
  assert.deepEqual(partByteRange(2, 100, 200), { start: 100, end: 200 });
});
