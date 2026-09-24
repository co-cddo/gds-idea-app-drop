// Handles the upload form on the homepage: for each selected file, asks the
// backend for a presigned S3 POST (via /api/presign - a Lambda behind the
// same ALB/Cognito auth as this site), then POSTs the file straight to S3
// using the returned url/fields.
//
// In local dev (`npm run start` / the eleventy dev server), /api/presign and
// the upload URL are mocked in eleventy.config.js - no AWS calls happen and
// no real upload occurs, but the full UI flow (progress, success, error
// states) can be exercised end-to-end without any cloud resources.

import { mergeFiles, removeFile } from './merge-files.js';

// Must match backend_stack.py's MAX_UPLOAD_BYTES (5 GiB). Checked client-side
// purely to avoid a wasted round trip for obviously-too-large files - the
// backend's presigned POST condition is the real enforcement point.
const MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024;

const form = document.getElementById('upload-form');
const fileInput = document.getElementById('file-upload');
const statusList = document.getElementById('upload-status');
const pendingList = document.getElementById('pending-files');

// GOV.UK Frontend's drag-and-drop handler does `input.files = event.dataTransfer.files`
// on every drop - a straight replace, not an append (see file-upload.mjs). There is no
// native way to accumulate files across separate drop/pick interactions, so we track our
// own list here (via the pure mergeFiles/removeFile helpers) and keep the native input in
// sync with it (so GOV.UK's own "X files chosen" status text stays correct).
let pendingFiles = [];
let isSyncingInput = false;

if (form && fileInput && statusList) {
  fileInput.addEventListener('change', handleFileInputChange);
  form.addEventListener('submit', handleSubmit);
}

function handleFileInputChange() {
  // Ignore the change event we trigger ourselves when re-syncing the input
  // below - only react to genuine user-driven picks/drops.
  if (isSyncingInput) {
    return;
  }

  pendingFiles = mergeFiles(pendingFiles, Array.from(fileInput.files || []));
  syncInputWithPendingFiles();
  renderPendingFiles();
}

function syncInputWithPendingFiles() {
  const dataTransfer = new DataTransfer();
  pendingFiles.forEach((file) => dataTransfer.items.add(file));

  isSyncingInput = true;
  fileInput.files = dataTransfer.files;
  // Setting .files programmatically doesn't fire a native change event, but
  // GOV.UK Frontend's own status text is only updated by its change handler
  // - re-dispatch so it re-renders against the merged count.
  fileInput.dispatchEvent(new Event('change'));
  isSyncingInput = false;
}

// Renders the pending-files preview using the same markup/classes as GOV.UK
// Frontend's real summary-list component (govuk-summary-list__row/key/
// value/actions) - built by hand rather than via the Nunjucks macro, since
// this only exists once the user has actually selected files in the
// browser. Lets users review and remove individual files before uploading.
function renderPendingFiles() {
  if (!pendingList) {
    return;
  }

  pendingList.textContent = '';
  pendingList.hidden = pendingFiles.length === 0;

  pendingFiles.forEach((file) => {
    pendingList.appendChild(buildPendingFileRow(file));
  });
}

function buildPendingFileRow(file) {
  const row = document.createElement('div');
  row.className = 'govuk-summary-list__row';

  const key = document.createElement('dt');
  key.className = 'govuk-summary-list__key';
  key.textContent = file.name;

  const value = document.createElement('dd');
  value.className = 'govuk-summary-list__value';
  value.textContent = formatFileSize(file.size);

  const actions = document.createElement('dd');
  actions.className = 'govuk-summary-list__actions';

  const removeLink = document.createElement('a');
  removeLink.href = '#';
  removeLink.className = 'govuk-link';
  removeLink.append(
    'Remove',
    Object.assign(document.createElement('span'), {
      className: 'govuk-visually-hidden',
      textContent: ` ${file.name}`,
    })
  );
  removeLink.addEventListener('click', (event) => {
    event.preventDefault();
    pendingFiles = removeFile(pendingFiles, file);
    syncInputWithPendingFiles();
    renderPendingFiles();
  });

  actions.appendChild(removeLink);
  row.append(key, value, actions);

  return row;
}

function formatFileSize(bytes) {
  if (bytes < 1024) {
    return `${bytes} bytes`;
  }

  const units = ['KB', 'MB', 'GB'];
  let value = bytes;
  let unitIndex = -1;

  do {
    value /= 1024;
    unitIndex += 1;
  } while (value >= 1024 && unitIndex < units.length - 1);

  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

async function handleSubmit(event) {
  event.preventDefault();

  if (pendingFiles.length === 0) {
    return;
  }

  statusList.textContent = '';

  const filesToUpload = pendingFiles;
  pendingFiles = [];
  syncInputWithPendingFiles();
  renderPendingFiles();

  // Uploaded one at a time - keeps the status list simple to follow and
  // avoids saturating the connection when several large files are dropped
  // at once. Revisit if concurrent uploads are needed later.
  for (const file of filesToUpload) {
    await uploadFile(file);
  }
}

async function uploadFile(file) {
  const row = addStatusRow(file.name);

  if (file.size > MAX_UPLOAD_BYTES) {
    setStatusRow(row, 'Too large (max 5GB)', 'red');
    return;
  }

  try {
    setStatusRow(row, 'Getting upload link…', 'grey');
    const { url, fields } = await requestPresignedPost(file);

    setStatusRow(row, 'Uploading…', 'grey');
    await postFileToS3(url, fields, file);

    setStatusRow(row, 'Uploaded', 'green');
  } catch (error) {
    console.error(`Upload failed for ${file.name}:`, error);
    setStatusRow(row, 'Upload failed', 'red');
  }
}

async function requestPresignedPost(file) {
  const response = await fetch('/api/presign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename: file.name,
      contentType: file.type || 'application/octet-stream',
    }),
  });

  if (!response.ok) {
    throw new Error(`Could not get an upload URL (status ${response.status})`);
  }

  return response.json();
}

async function postFileToS3(url, fields, file) {
  const formData = new FormData();
  Object.entries(fields || {}).forEach(([key, value]) => {
    formData.append(key, value);
  });
  // The file field must be appended last - S3 requires it to be the final
  // field in the multipart form.
  formData.append('file', file);

  const response = await fetch(url, { method: 'POST', body: formData });

  // S3 returns 204 (or 201 with a Location header) on a successful
  // presigned POST upload.
  if (!response.ok && response.status !== 204) {
    throw new Error(`Upload failed (status ${response.status})`);
  }
}

function addStatusRow(filename) {
  const li = document.createElement('li');
  li.className = 'govuk-body';

  const name = document.createElement('span');
  name.textContent = filename;

  const tag = document.createElement('strong');
  tag.className = 'govuk-tag govuk-tag--grey govuk-!-margin-left-2';
  tag.textContent = 'Queued';

  li.append(name, tag);
  statusList.appendChild(li);

  return tag;
}

function setStatusRow(tag, text, colour) {
  tag.textContent = text;
  tag.className = `govuk-tag govuk-tag--${colour} govuk-!-margin-left-2`;
}
