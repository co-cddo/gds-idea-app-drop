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
import { partByteRange, runWithConcurrency, sleep } from './upload-concurrency.js';

// Must match backend_stack.py's MAX_UPLOAD_BYTES (5 GiB). Checked client-side
// purely to avoid a wasted round trip for obviously-too-large files - the
// backend's presigned POST condition is the real enforcement point.
const MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024;

// How many parts to upload in parallel for a multipart upload. The backend
// decides part size/count (see requestUploadPlan) - this only controls how
// many of those parts run at once from the browser.
const MULTIPART_CONCURRENCY = 5;

// A single part failing outright (rather than the whole file) is the point
// of multipart - retry a few times before giving up on the whole upload.
const MAX_PART_RETRIES = 3;
const PART_RETRY_BASE_DELAY_MS = 500;

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

  const startedAt = performance.now();

  try {
    setStatusRow(row, 'Getting upload link…', 'grey');
    const plan = await requestUploadPlan(file);

    if (plan.uploadId) {
      await uploadMultipart(plan, file, (percent) => {
        setStatusRow(row, `Uploading… ${percent}%`, 'grey');
      });
    } else {
      setStatusRow(row, 'Uploading…', 'grey');
      await postFileToS3(plan.url, plan.fields, file);
    }

    setStatusRow(row, 'Uploaded', 'green');
  } catch (error) {
    console.error(`Upload failed for ${file.name}:`, error);
    setStatusRow(row, 'Upload failed', 'red');
    reportUploadError(file, error, performance.now() - startedAt);
  }
}

// Asks the backend for an upload plan: either a single presigned POST
// (small files) or a multipart upload (large files, identified by the
// presence of `uploadId` in the response) - see backend_src/presign/
// handler.py's module docstring for the exact response shapes.
async function requestUploadPlan(file) {
  const response = await fetch('/api/presign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename: file.name,
      contentType: file.type || 'application/octet-stream',
      fileSize: file.size,
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

// Uploads every part of a multipart plan with bounded concurrency, retrying
// a failed part on its own rather than restarting the whole file, then
// completes the upload server-side. On any unrecoverable failure, tells the
// backend to abort - the S3 lifecycle rule cleans up abandoned multipart
// uploads anyway, but this frees the storage immediately rather than after
// a day, and cleanly ends the upload the moment it's known to have failed.
async function uploadMultipart(plan, file, onProgress) {
  const { uploadId, key, partSize, parts } = plan;
  const totalBytes = file.size;
  const completedParts = new Array(parts.length);
  let completedBytes = 0;

  onProgress(0);

  try {
    await runWithConcurrency(parts, MULTIPART_CONCURRENCY, async (part) => {
      const { start, end } = partByteRange(part.partNumber, partSize, totalBytes);
      const blob = file.slice(start, end);
      const eTag = await uploadPartWithRetry(part.url, blob);

      completedParts[part.partNumber - 1] = { partNumber: part.partNumber, eTag };
      completedBytes += blob.size;
      onProgress(Math.round((completedBytes / totalBytes) * 100));
    });
  } catch (error) {
    await abortMultipart(uploadId, key);
    throw error;
  }

  await completeMultipart(uploadId, key, completedParts);
}

async function uploadPartWithRetry(url, blob) {
  let lastError;

  for (let attempt = 1; attempt <= MAX_PART_RETRIES; attempt += 1) {
    try {
      const response = await fetch(url, { method: 'PUT', body: blob });
      if (!response.ok) {
        throw new Error(`Part upload failed (status ${response.status})`);
      }

      // Requires the uploads bucket's CORS rule to set
      // ExposedHeaders: ["ETag"] - it isn't one of the browser's
      // CORS-safelisted response headers, so without that this silently
      // returns null instead of throwing.
      const eTag = response.headers.get('ETag');
      if (!eTag) {
        throw new Error('Part upload succeeded but no ETag header was returned');
      }

      return eTag;
    } catch (error) {
      lastError = error;
      if (attempt < MAX_PART_RETRIES) {
        await sleep(PART_RETRY_BASE_DELAY_MS * attempt);
      }
    }
  }

  throw lastError;
}

async function completeMultipart(uploadId, key, parts) {
  const response = await fetch('/api/presign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'complete', uploadId, key, parts }),
  });

  if (!response.ok) {
    throw new Error(`Could not complete upload (status ${response.status})`);
  }
}

async function abortMultipart(uploadId, key) {
  try {
    await fetch('/api/presign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'abort', uploadId, key }),
    });
  } catch {
    // Best-effort - nothing more to do if even the abort call fails.
  }
}

// Best-effort failure report, sent back through the presign Lambda so
// upload failures are visible server-side - the actual upload goes
// straight from the browser to S3 (see postFileToS3 above), so without
// this a failed upload is otherwise invisible except in this browser's
// own console. Never allowed to affect the "Upload failed" UI state
// above, or itself be treated as a further failure.
function reportUploadError(file, error, elapsedMs) {
  fetch('/api/presign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      action: 'report-error',
      filename: file.name,
      fileSize: file.size,
      elapsedMs: Math.round(elapsedMs),
      error: String(error && error.message ? error.message : error),
    }),
  }).catch(() => {
    // Nothing more we can do - don't let a reporting failure cascade.
  });
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
