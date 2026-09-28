// Pure, DOM-free helper for accumulating File selections across separate
// drag/drop or file-picker interactions.
//
// Native <input type="file"> (and GOV.UK Frontend's drag-and-drop handler,
// which does `input.files = event.dataTransfer.files`) always REPLACES the
// previous FileList on each interaction - there is no browser-native way to
// append. upload.js uses this to maintain its own accumulated list instead.
//
// Kept dependency-free (no DOM/browser APIs) so it can be unit tested with
// plain Node - see merge-files.test.js.

export function isSameFile(a, b) {
  return a.name === b.name && a.size === b.size && a.lastModified === b.lastModified;
}

/**
 * Merge newly-selected files into an existing accumulated list, skipping
 * any that are already present (same name/size/lastModified).
 *
 * @param {File[]} existingFiles
 * @param {File[]} newFiles
 * @returns {File[]} a new array - does not mutate either input
 */
export function mergeFiles(existingFiles, newFiles) {
  const merged = [...existingFiles];
  for (const file of newFiles) {
    if (!merged.some((existing) => isSameFile(existing, file))) {
      merged.push(file);
    }
  }
  return merged;
}

/**
 * Remove a single file from an accumulated list (e.g. when the user clicks
 * "Remove" in the pending-files preview). Matches on name/size/lastModified,
 * same as mergeFiles' de-duplication.
 *
 * @param {File[]} files
 * @param {File} fileToRemove
 * @returns {File[]} a new array - does not mutate the input
 */
export function removeFile(files, fileToRemove) {
  return files.filter((file) => !isSameFile(file, fileToRemove));
}
