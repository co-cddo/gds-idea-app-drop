import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  describeUploader,
  downloadApiUrl,
  formatBytes,
  formatDateTime,
  isAdminUser,
  listApiUrl,
} from '../src/assets/admin-format.js';

test('isAdminUser requires membership of the gds-idea group', () => {
  assert.equal(isAdminUser({ groups: ['gds-idea'] }), true);
  assert.equal(isAdminUser({ groups: ['other', 'gds-idea'] }), true);
  assert.equal(isAdminUser({ groups: ['gds-idea-ish'] }), false);
  assert.equal(isAdminUser({ groups: [] }), false);
  assert.equal(isAdminUser({}), false);
  assert.equal(isAdminUser(null), false);
});

test('isAdminUser ignores is_admin (per-app admins are rejected by the API)', () => {
  assert.equal(isAdminUser({ is_admin: true, groups: [] }), false);
});

test('isAdminUser does not treat a string groups value as membership', () => {
  assert.equal(isAdminUser({ groups: 'gds-idea' }), false);
});

test('formatBytes', () => {
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(512), '512 B');
  assert.equal(formatBytes(1024), '1.0 KB');
  assert.equal(formatBytes(1536), '1.5 KB');
  assert.equal(formatBytes(10 * 1024 * 1024), '10 MB');
  assert.equal(formatBytes(5 * 1024 ** 3), '5.0 GB');
  assert.equal(formatBytes(null), 'Unknown');
  assert.equal(formatBytes(-1), 'Unknown');
  assert.equal(formatBytes(NaN), 'Unknown');
});

test('formatDateTime is UTC and tolerant of bad input', () => {
  assert.equal(
    formatDateTime('2026-01-02T14:05:00+00:00'),
    '2 Jan 2026, 14:05 UTC',
  );
  assert.equal(formatDateTime('2026-07-02T14:05:00+01:00'), '2 Jul 2026, 13:05 UTC');
  assert.equal(formatDateTime('nonsense'), 'Unknown');
  assert.equal(formatDateTime(null), 'Unknown');
});

test('describeUploader', () => {
  assert.deepEqual(
    describeUploader({ uploadedByName: 'A B', uploadedByEmail: 'a@x.gov.uk' }),
    { name: 'A B', email: 'a@x.gov.uk' },
  );
  assert.deepEqual(describeUploader({ uploadedByEmail: 'a@x.gov.uk' }), {
    name: 'a@x.gov.uk',
    email: '',
  });
  assert.deepEqual(describeUploader({}), { name: 'Unknown', email: '' });
});

test('API url builders encode their parameters', () => {
  assert.equal(
    downloadApiUrl('uploads/2026/01/02/id/a b&c.txt'),
    '/api/admin/download?key=uploads%2F2026%2F01%2F02%2Fid%2Fa%20b%26c.txt',
  );
  assert.equal(listApiUrl(null), '/api/admin/uploads?limit=50');
  assert.equal(listApiUrl('a=b', 10), '/api/admin/uploads?limit=10&cursor=a%3Db');
});
