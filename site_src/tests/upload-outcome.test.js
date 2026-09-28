import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  consumeUploadOutcome,
  outcomeDestinationUrl,
  storeUploadOutcome,
} from '../src/assets/upload-outcome.js';

// sessionStorage isn't available in Node's test runner by default - provide
// a minimal in-memory stand-in, reset before each test.
function installFakeSessionStorage() {
  const store = new Map();
  globalThis.sessionStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  };
}

test('storeUploadOutcome + consumeUploadOutcome: round-trips an outcome', () => {
  installFakeSessionStorage();

  const outcome = {
    succeeded: ['a.txt', 'b.txt'],
    failed: [{ filename: 'c.txt', error: 'Upload failed (status 500)' }],
  };
  storeUploadOutcome(outcome);

  assert.deepEqual(consumeUploadOutcome(), outcome);
});

test('consumeUploadOutcome: is one-shot - a second read returns null', () => {
  installFakeSessionStorage();

  storeUploadOutcome({ succeeded: ['a.txt'], failed: [] });
  consumeUploadOutcome();

  assert.equal(consumeUploadOutcome(), null);
});

test('consumeUploadOutcome: returns null when nothing was stored', () => {
  installFakeSessionStorage();
  assert.equal(consumeUploadOutcome(), null);
});

test('consumeUploadOutcome: returns null for malformed stored data', () => {
  installFakeSessionStorage();
  sessionStorage.setItem('dropUploadOutcome', 'not json');
  assert.equal(consumeUploadOutcome(), null);
});

test('consumeUploadOutcome: returns null when shape is unexpected', () => {
  installFakeSessionStorage();
  sessionStorage.setItem('dropUploadOutcome', JSON.stringify({ foo: 'bar' }));
  assert.equal(consumeUploadOutcome(), null);
});

test('storeUploadOutcome: does not throw if sessionStorage is unavailable', () => {
  globalThis.sessionStorage = {
    setItem: () => {
      throw new Error('blocked (e.g. private browsing)');
    },
  };
  assert.doesNotThrow(() => storeUploadOutcome({ succeeded: [], failed: [] }));
});

test('outcomeDestinationUrl: /success/ when nothing failed', () => {
  assert.equal(outcomeDestinationUrl({ succeeded: ['a.txt'], failed: [] }), '/success/');
});

test('outcomeDestinationUrl: /error/ when anything failed, even with some successes', () => {
  assert.equal(
    outcomeDestinationUrl({
      succeeded: ['a.txt'],
      failed: [{ filename: 'b.txt', error: 'boom' }],
    }),
    '/error/'
  );
});
