import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { govukEleventyPlugin } from '@x-govuk/govuk-eleventy-plugin';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Deliberately much smaller than the real MULTIPART_THRESHOLD_BYTES/
// PART_SIZE_BYTES in backend_stack.py (100MB each) - this only exists so a
// developer can drop a small test file locally and still see the
// multipart/progress-bar UI branch, without needing a 100MB+ file just to
// exercise it. Nothing here needs to match production sizing.
const MOCK_MULTIPART_THRESHOLD_BYTES = 1 * 1024 * 1024;
const MOCK_PART_SIZE_BYTES = 512 * 1024;

export default function(eleventyConfig) {
  eleventyConfig.addPlugin(govukEleventyPlugin, {
    header: {
      // Replaces the GOV.UK crown/logotype entirely (no toggle exists to
      // just hide it) - this isn't an official gov.uk domain/service, so
      // the crown isn't ours to use.
      logotype: {
        text: 'Drop'
      },
      // Standard GOV.UK phase banner - service is new/still in beta.
      phaseBanner: {
        tag: {
          text: 'Beta'
        },
        html: 'This is a new service — your feedback will help us to improve it.'
      }
    },
    footer: {
      // The footer also shows a crown/coat-of-arms graphic by default -
      // same copyright concern as the header logo, so disable it too.
      logo: false,
      meta: {
        text: 'Built by the GDS IDEA Unit'
      }
    }
  });

  // Our own upload.js lives alongside the govuk-frontend assets the plugin
  // already copies to the same output "assets" directory.
  eleventyConfig.addPassthroughCopy({ 'src/assets': 'assets' });

  // Dev-server-only mocks, so the full upload flow (drag/drop, presign,
  // S3 POST) can be exercised locally with `npm start` - no AWS credentials,
  // no deployed infrastructure required. None of this runs in production;
  // the built site never includes these routes, and the real /api/presign
  // is served by the presign Lambda behind the ALB instead.
  eleventyConfig.setServerOptions({
    middleware: [
      function(req, res, next) {
        // Mock /.auth/user (reads from dev_mocks/user.json)
        if (req.url === '/.auth/user') {
          const mockPath = path.resolve(__dirname, '../dev_mocks/user.json');
          if (fs.existsSync(mockPath)) {
            res.setHeader('Content-Type', 'application/json');
            res.end(fs.readFileSync(mockPath, 'utf8'));
            return;
          }
        }

        // Mock /api/presign: returns a fake presigned POST (small files) or
        // a fake multipart plan (files over MOCK_MULTIPART_THRESHOLD_BYTES),
        // pointing at the local /dev-mock-upload* endpoints below instead
        // of real S3 URLs. Also handles the {"action": ...} variants - the
        // real Lambda does real work for these, the mock just acks/logs.
        if (req.url === '/api/presign' && req.method === 'POST') {
          readRequestBody(req).then((body) => {
            let parsed;
            try {
              parsed = JSON.parse(body || '{}');
            } catch {
              res.statusCode = 400;
              res.setHeader('Content-Type', 'application/json');
              res.end(JSON.stringify({ error: 'Request body must be valid JSON' }));
              return;
            }

            if (parsed.action === 'report-error') {
              console.log('[dev-mock] upload_reported_failed', parsed);
              res.setHeader('Content-Type', 'application/json');
              res.end(JSON.stringify({ ok: true }));
              return;
            }

            if (parsed.action === 'complete') {
              console.log('[dev-mock] multipart_completed', parsed);
              res.setHeader('Content-Type', 'application/json');
              res.end(JSON.stringify({ ok: true }));
              return;
            }

            if (parsed.action === 'abort') {
              console.log('[dev-mock] multipart_aborted', parsed);
              res.setHeader('Content-Type', 'application/json');
              res.end(JSON.stringify({ ok: true }));
              return;
            }

            const filename = parsed.filename || 'file';
            const key = `uploads/dev-mock/${Date.now()}-${filename}`;
            const fileSize = typeof parsed.fileSize === 'number' ? parsed.fileSize : 0;

            if (fileSize > MOCK_MULTIPART_THRESHOLD_BYTES) {
              const totalParts = Math.ceil(fileSize / MOCK_PART_SIZE_BYTES);
              const parts = Array.from({ length: totalParts }, (_, i) => ({
                partNumber: i + 1,
                url: `/dev-mock-upload-part?part=${i + 1}`
              }));
              res.setHeader('Content-Type', 'application/json');
              res.end(
                JSON.stringify({
                  uploadId: 'dev-mock-upload-id',
                  key,
                  partSize: MOCK_PART_SIZE_BYTES,
                  totalParts,
                  parts
                })
              );
              return;
            }

            res.setHeader('Content-Type', 'application/json');
            res.end(JSON.stringify({ url: '/dev-mock-upload', fields: { key }, key }));
          });
          return;
        }

        // Mock a single multipart part upload: drains the body, returns an
        // ETag header (mirroring what upload.js reads from a real S3 PUT
        // response) and a 200. Same-origin, so - unlike the real S3 CORS
        // config - no ExposedHeaders setting is needed for this to work.
        if (req.url.startsWith('/dev-mock-upload-part') && req.method === 'PUT') {
          readRequestBody(req).then(() => {
            const part = new URL(req.url, 'http://localhost').searchParams.get('part') || '0';
            res.setHeader('ETag', `"dev-mock-etag-${part}"`);
            res.statusCode = 200;
            res.end();
          });
          return;
        }

        // Mock the actual S3 upload: drains the multipart body and responds
        // 204, mirroring a real presigned POST's success response. Doesn't
        // persist anything - this is only for exercising the UI locally.
        if (req.url === '/dev-mock-upload' && req.method === 'POST') {
          readRequestBody(req).then(() => {
            res.statusCode = 204;
            res.end();
          });
          return;
        }

        next();
      }
    ]
  });

  return {
    dataTemplateEngine: 'njk',
    htmlTemplateEngine: 'njk',
    markdownTemplateEngine: 'njk',
    dir: {
      input: 'src',
      output: process.env.ELEVENTY_OUTPUT_DIR || '_site'
    }
  };
}

function readRequestBody(req) {
  return new Promise((resolve) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
    });
    req.on('end', () => resolve(body));
  });
}
