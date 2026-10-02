// Admin uploads page: lists uploaded files and lets a gds-idea admin
// download them. The page itself is static and visible to anyone signed in;
// all access control is enforced by /api/admin/* - the isAdminUser check here
// only decides what to display.
//
// Everything that comes from the API (filenames, names, emails) is
// user-controlled, so it is only ever written via textContent.

import {
  describeUploader,
  downloadApiUrl,
  formatBytes,
  formatDateTime,
  isAdminUser,
  listApiUrl,
} from './admin-format.js';

const $ = (id) => document.getElementById(id);

let nextCursor = null;
let loading = false;

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function getJson(url) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ApiError(response.status, `Request failed (${response.status})`);
  }
  return response.json();
}

function explain(error) {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return 'Your session has expired. Refresh the page to sign in again.';
    }
    if (error.status === 403) {
      return 'You do not have access to this page.';
    }
    if (error.status === 404) {
      return 'That file no longer exists.';
    }
  }
  return 'Something went wrong. Try again.';
}

function showError(error) {
  $('admin-error-message').textContent = explain(error);
  $('admin-error').hidden = false;
}

function clearError() {
  $('admin-error').hidden = true;
}

function cell(className, text) {
  const td = document.createElement('td');
  td.className = `govuk-table__cell ${className}`.trim();
  td.textContent = text;
  return td;
}

function renderRow(item) {
  const tr = document.createElement('tr');
  tr.className = 'govuk-table__row';

  tr.appendChild(cell('', item.filename || 'Unknown'));

  const uploader = describeUploader(item);
  const uploaderCell = cell('', uploader.name);
  if (uploader.email) {
    uploaderCell.appendChild(document.createElement('br'));
    const email = document.createElement('span');
    email.className = 'govuk-hint govuk-!-margin-bottom-0';
    email.textContent = uploader.email;
    uploaderCell.appendChild(email);
  }
  tr.appendChild(uploaderCell);

  tr.appendChild(cell('', formatDateTime(item.uploadedAt)));
  tr.appendChild(cell('govuk-table__cell--numeric', formatBytes(item.size)));

  const actions = document.createElement('td');
  actions.className = 'govuk-table__cell';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'govuk-button govuk-button--secondary govuk-!-margin-bottom-0';
  button.textContent = 'Download';
  const hidden = document.createElement('span');
  hidden.className = 'govuk-visually-hidden';
  hidden.textContent = ` ${item.filename || 'file'}`;
  button.appendChild(hidden);
  button.addEventListener('click', () => download(item, button));
  actions.appendChild(button);
  tr.appendChild(actions);

  return tr;
}

async function download(item, button) {
  clearError();
  button.disabled = true;
  try {
    const { url } = await getJson(downloadApiUrl(item.key));
    // The link is short-lived and served as an attachment, so this starts a
    // download without navigating away from the page.
    window.location.assign(url);
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
}

async function loadPage() {
  if (loading) {
    return;
  }
  loading = true;
  clearError();
  const moreButton = $('admin-load-more');
  moreButton.disabled = true;
  $('admin-status').textContent = 'Loading files…';

  try {
    const { items, nextCursor: cursor } = await getJson(listApiUrl(nextCursor));
    const rows = $('admin-rows');
    for (const item of items) {
      rows.appendChild(renderRow(item));
    }
    nextCursor = cursor || null;

    const empty = rows.children.length === 0;
    $('admin-table').hidden = empty;
    $('admin-empty').hidden = !empty;
    moreButton.hidden = !nextCursor;
    $('admin-status').textContent = empty ? '' : `${rows.children.length} files shown.`;
  } catch (error) {
    $('admin-status').textContent = '';
    showError(error);
  } finally {
    moreButton.disabled = false;
    loading = false;
  }
}

async function init() {
  let user = null;
  try {
    const response = await fetch('/.auth/user', { credentials: 'same-origin' });
    user = response.ok ? await response.json() : null;
  } catch {
    user = null;
  }

  $('admin-loading').hidden = true;

  if (!isAdminUser(user)) {
    $('admin-denied').hidden = false;
    return;
  }

  $('admin-content').hidden = false;
  $('admin-load-more').addEventListener('click', loadPage);
  await loadPage();
}

init();
