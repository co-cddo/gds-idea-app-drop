// Pure helpers for the multipart upload flow - no DOM/fetch dependencies,
// so they can be unit tested directly (see tests/upload-concurrency.test.js).
// upload.js layers the actual fetch/PUT/DOM work on top of these.

/**
 * Run `worker` over `items` with at most `limit` running concurrently.
 * A fixed-size pool of "lanes" each pull the next item off a shared queue
 * as soon as they're free, rather than chunking into fixed batches - so a
 * few slow parts don't stall the whole pool waiting for a batch boundary.
 */
export async function runWithConcurrency(items, limit, worker) {
  const queue = [...items];
  const laneCount = Math.max(1, Math.min(limit, queue.length));

  async function runLane() {
    while (queue.length > 0) {
      const item = queue.shift();
      await worker(item);
    }
  }

  await Promise.all(Array.from({ length: laneCount }, runLane));
}

/**
 * Compute the byte range [start, end) for a given 1-based part number.
 * Must match the same partSize-based division the backend used when it
 * decided totalParts (see backend_src/presign/handler.py's
 * _handle_create_multipart) - both sides derive ranges from the same
 * partSize/fileSize rather than the backend sending explicit ranges.
 */
export function partByteRange(partNumber, partSize, fileSize) {
  const start = (partNumber - 1) * partSize;
  const end = Math.min(start + partSize, fileSize);
  return { start, end };
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
