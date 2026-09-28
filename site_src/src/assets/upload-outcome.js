// Pure helpers for handing off the upload batch outcome from the upload
// form page to the success/error pages, via sessionStorage. No DOM here -
// see upload.js (writer) and success.njk/error.njk's inline scripts
// (readers) for where this is actually used.

const STORAGE_KEY = 'dropUploadOutcome';

/**
 * @param {{succeeded: string[], failed: {filename: string, error: string}[]}} outcome
 */
export function storeUploadOutcome(outcome) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(outcome));
  } catch {
    // sessionStorage unavailable (e.g. private browsing) - the destination
    // page falls back to a generic message, so this is safe to ignore.
  }
}

/**
 * Reads and clears the stored outcome (one-shot - a page refresh on
 * /success/ or /error/ shouldn't keep replaying stale data).
 *
 * @returns {{succeeded: string[], failed: {filename: string, error: string}[]}|null}
 */
export function consumeUploadOutcome() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    sessionStorage.removeItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw);
    if (
      !parsed ||
      !Array.isArray(parsed.succeeded) ||
      !Array.isArray(parsed.failed)
    ) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function outcomeDestinationUrl(outcome) {
  return outcome.failed.length > 0 ? '/error/' : '/success/';
}
