import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isSameFile, mergeFiles, removeFile } from '../src/assets/merge-files.js';

function makeFile(name, { size = 10, lastModified = 1_700_000_000_000 } = {}) {
  // Node's built-in File doesn't accept an arbitrary size directly, so pad
  // the content to reach the requested size.
  return new File([new Uint8Array(size)], name, { lastModified });
}

test('isSameFile: true for identical name/size/lastModified', () => {
  const a = makeFile('report.pdf');
  const b = makeFile('report.pdf');
  assert.equal(isSameFile(a, b), true);
});

test('isSameFile: false when name differs', () => {
  const a = makeFile('report.pdf');
  const b = makeFile('other.pdf');
  assert.equal(isSameFile(a, b), false);
});

test('isSameFile: false when size differs', () => {
  const a = makeFile('report.pdf', { size: 10 });
  const b = makeFile('report.pdf', { size: 20 });
  assert.equal(isSameFile(a, b), false);
});

test('mergeFiles: accumulates files from separate calls (the drop-then-drop-again case)', () => {
  const afterFirstDrop = mergeFiles([], [makeFile('a.txt')]);
  assert.equal(afterFirstDrop.length, 1);

  const afterSecondDrop = mergeFiles(afterFirstDrop, [makeFile('b.txt')]);

  assert.equal(afterSecondDrop.length, 2);
  assert.deepEqual(
    afterSecondDrop.map((f) => f.name),
    ['a.txt', 'b.txt']
  );
});

test('mergeFiles: does not duplicate a file that is re-selected/re-dropped', () => {
  const first = mergeFiles([], [makeFile('a.txt')]);
  const second = mergeFiles(first, [makeFile('a.txt')]);

  assert.equal(second.length, 1);
});

test('mergeFiles: a single drop with multiple files still merges all of them', () => {
  const result = mergeFiles([], [makeFile('a.txt'), makeFile('b.txt'), makeFile('c.txt')]);
  assert.equal(result.length, 3);
});

test('mergeFiles: does not mutate the existing array passed in', () => {
  const existing = mergeFiles([], [makeFile('a.txt')]);
  const existingSnapshot = [...existing];

  mergeFiles(existing, [makeFile('b.txt')]);

  assert.deepEqual(existing, existingSnapshot);
});

test('mergeFiles: three separate drops each add their files (regression test for the reported bug)', () => {
  let pending = [];
  pending = mergeFiles(pending, [makeFile('one.txt')]);
  pending = mergeFiles(pending, [makeFile('two.txt')]);
  pending = mergeFiles(pending, [makeFile('three.txt')]);

  assert.deepEqual(
    pending.map((f) => f.name),
    ['one.txt', 'two.txt', 'three.txt']
  );
});

test('removeFile: removes only the matching file', () => {
  const pending = mergeFiles([], [makeFile('a.txt'), makeFile('b.txt'), makeFile('c.txt')]);

  const result = removeFile(pending, pending[1]);

  assert.deepEqual(
    result.map((f) => f.name),
    ['a.txt', 'c.txt']
  );
});

test('removeFile: leaves the list unchanged if the file is not present', () => {
  const pending = mergeFiles([], [makeFile('a.txt')]);

  const result = removeFile(pending, makeFile('not-in-list.txt'));

  assert.equal(result.length, 1);
});

test('removeFile: does not mutate the input array', () => {
  const pending = mergeFiles([], [makeFile('a.txt'), makeFile('b.txt')]);
  const snapshot = [...pending];

  removeFile(pending, pending[0]);

  assert.deepEqual(pending, snapshot);
});

test('removeFile: only removes a file matching on name/size/lastModified, not just name', () => {
  const a = makeFile('a.txt', { size: 10 });
  const pending = [a];

  // Same name, different size - should NOT be treated as the same file.
  const result = removeFile(pending, makeFile('a.txt', { size: 999 }));

  assert.equal(result.length, 1);
});
