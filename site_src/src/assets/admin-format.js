// Pure helpers for the admin uploads page. No DOM here, so they can be
// unit-tested with `node --test` - see admin.js for the DOM wiring.

/** The Cognito group whose members may use the admin API. */
export const ADMIN_GROUP = 'gds-idea';

/**
 * Whether to show admin UI for this /.auth/user response. This is display
 * only - the admin API independently enforces the same group check.
 * (Deliberately the group, not `is_admin`: `is_admin` can also be true for
 * per-app admins, who the admin API would reject.)
 *
 * @param {{groups?: unknown}|null|undefined} user
 */
export function isAdminUser(user) {
  return Boolean(
    user && Array.isArray(user.groups) && user.groups.includes(ADMIN_GROUP),
  );
}

/**
 * @param {number|null|undefined} bytes
 * @returns {string}
 */
export function formatBytes(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) {
    return 'Unknown';
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = unit === 0 ? String(value) : value.toFixed(value < 10 ? 1 : 0);
  return `${text} ${units[unit]}`;
}

/**
 * Formats an ISO timestamp as e.g. "2 Jan 2026, 14:05 UTC". Fixed to UTC so
 * it's unambiguous and testable.
 *
 * @param {string|null|undefined} iso
 * @returns {string}
 */
export function formatDateTime(iso) {
  const date = new Date(iso ?? '');
  if (Number.isNaN(date.getTime())) {
    return 'Unknown';
  }
  const text = new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'UTC',
  }).format(date);
  return `${text} UTC`;
}

/**
 * @param {{uploadedByName?: string|null, uploadedByEmail?: string|null}} item
 * @returns {{name: string, email: string}}
 */
export function describeUploader(item) {
  const email = item.uploadedByEmail || '';
  const name = item.uploadedByName || '';
  if (!name && !email) {
    return { name: 'Unknown', email: '' };
  }
  return { name: name || email, email: name ? email : '' };
}

/**
 * @param {string} key
 * @returns {string}
 */
export function downloadApiUrl(key) {
  return `/api/admin/download?key=${encodeURIComponent(key)}`;
}

/**
 * @param {string|null|undefined} cursor
 * @param {number} [limit]
 * @returns {string}
 */
export function listApiUrl(cursor, limit = 50) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) {
    params.set('cursor', cursor);
  }
  return `/api/admin/uploads?${params.toString()}`;
}
