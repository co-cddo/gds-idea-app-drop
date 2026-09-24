import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { govukEleventyPlugin } from '@x-govuk/govuk-eleventy-plugin';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default function(eleventyConfig) {
  eleventyConfig.addPlugin(govukEleventyPlugin, {
    header: {
      serviceName: 'yeet'
    },
    footer: {
      meta: {
        text: 'Built with the GDS IDEA static site construct'
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

        // Mock /api/presign: returns a fake presigned POST pointing at the
        // local /dev-mock-upload endpoint below, instead of a real S3 URL.
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

            const filename = parsed.filename || 'file';
            const key = `uploads/dev-mock/${Date.now()}-${filename}`;
            res.setHeader('Content-Type', 'application/json');
            res.end(JSON.stringify({ url: '/dev-mock-upload', fields: { key }, key }));
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
