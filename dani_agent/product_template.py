"""Starter for every new product: a dependency-free Node.js web app (built-ins only, no npm installs)."""
import json
import re

SERVER = r"""// __NAME__ - dependency-free Node.js web server (Node built-ins only, no npm packages).
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const PUBLIC_DIR = fileURLToPath(new URL('./public/', import.meta.url));
const TYPES = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
};

// API routes. Return { status, body } (body is JSON) or null when not handled.
// Keep routes pure so node:test can call them directly, without opening sockets.
export function api(method, pathname, body = null) {
  if (method === 'GET' && pathname === '/api/health') return { status: 200, body: { ok: true } };
  return null;
}

// Map a URL path to a file inside public/, or null if it would escape that folder.
export function resolvePublic(pathname) {
  let decoded;
  try { decoded = decodeURIComponent(pathname); } catch { return null; }
  const file = resolve(PUBLIC_DIR, '.' + (decoded === '/' ? '/index.html' : decoded));
  return file.startsWith(PUBLIC_DIR.endsWith(sep) ? PUBLIC_DIR : PUBLIC_DIR + sep) ? file : null;
}

async function readJson(req) {
  let text = '';
  for await (const chunk of req) { text += chunk; if (text.length > 1e6) throw new Error('too large'); }
  return text ? JSON.parse(text) : null;
}

export async function handle(req, res) {
  const url = new URL(req.url, 'http://localhost');
  try {
    const body = ['POST', 'PUT', 'PATCH'].includes(req.method) ? await readJson(req) : null;
    const routed = api(req.method, url.pathname, body);
    if (routed) {
      res.writeHead(routed.status, { 'content-type': TYPES['.json'] });
      res.end(JSON.stringify(routed.body));
      return;
    }
    const file = resolvePublic(url.pathname);
    if (!file) throw new Error('outside public');
    const content = await readFile(file);
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
    res.end(content);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Not found');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.PORT) || 3000;
  createServer(handle).listen(port, () => console.log(`__NAME__ is running on http://localhost:${port}`));
}
"""

SERVER_TEST = r"""import assert from 'node:assert/strict';
import { test } from 'node:test';
import { api, resolvePublic } from './server.mjs';

test('health endpoint reports ok', () => {
  assert.deepEqual(api('GET', '/api/health'), { status: 200, body: { ok: true } });
});

test('unknown API routes are not handled', () => {
  assert.equal(api('GET', '/api/unknown'), null);
});

test('static files resolve inside public/ only', () => {
  assert.ok(resolvePublic('/').endsWith('index.html'));
  assert.equal(resolvePublic('/../server.mjs'), null);
  assert.equal(resolvePublic('/%2e%2e/server.mjs'), null);
});
"""

INDEX = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <main id="app">
    <h1>__TITLE__</h1>
    <p>This product is being built by Dani Agent.</p>
  </main>
  <script type="module" src="app.js"></script>
</body>
</html>
"""

STYLES = """:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; padding: 2rem 1rem; }
main { max-width: 52rem; margin: 0 auto; }
"""

APP_JS = """// Browser code. Put testable logic in public/lib/*.mjs modules and import them here.
"""

README = """# __TITLE__

__DESCRIPTION__

Built by Dani Agent as a dependency-free Node.js web app (Node built-ins only).

- Run: `node server.mjs`, then open http://localhost:3000
- Test: `node --test`
"""


PY_MAIN = '''"""__TITLE__ - command-line entry point (Python standard library only)."""
import argparse
import sys

import core


def main(argv=None):
    parser = argparse.ArgumentParser(description=core.DESCRIPTION)
    parser.add_argument('--version', action='version', version=core.VERSION)
    parser.parse_args(argv)
    print(core.greeting())
    return 0


if __name__ == '__main__':
    sys.exit(main())
'''

PY_CORE = '''"""Core logic for __TITLE__. Keep functions pure where possible so test_*.py can test them directly."""
VERSION = '0.1.0'
DESCRIPTION = __DESCRIPTION_LITERAL__


def greeting():
    return __TITLE_LITERAL__ + ' is ready.'
'''

PY_TEST = '''import io
import unittest
from contextlib import redirect_stdout

import core
import main


class StarterTests(unittest.TestCase):
    def test_greeting_names_the_product(self):
        self.assertTrue(core.greeting().endswith('is ready.'))

    def test_main_prints_the_greeting(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main.main([]), 0)
        self.assertIn(core.greeting(), output.getvalue())


if __name__ == '__main__':
    unittest.main()
'''

PY_README = """# __TITLE__

__DESCRIPTION__

Built by Dani Agent as a Python application (standard library only, no installs).

- Run: `python main.py`
- Test: `python -m unittest`
"""


def slug(name):
    value = re.sub(r'[^a-z0-9]+', '-', name.casefold()).strip('-')
    return value[:40] or 'product'


def files(name, description, kind='web'):
    """Text files of a new product, keyed by relative POSIX path."""
    one_line = ' '.join(description.split())
    if kind == 'python':
        doc_title = name.replace('\\', '/').replace('"', "'")   # safe inside a """docstring"""
        return {
            '.gitignore': '__pycache__/\n*.pyc\n',
            'README.md': PY_README.replace('__TITLE__', name).replace('__DESCRIPTION__', description.strip()),
            'main.py': PY_MAIN.replace('__TITLE__', doc_title),
            'core.py': PY_CORE.replace('__TITLE__', doc_title)
                .replace('__DESCRIPTION_LITERAL__', repr(one_line[:200])).replace('__TITLE_LITERAL__', repr(name)),
            'test_core.py': PY_TEST,
        }
    if kind != 'web':
        raise ValueError('UNKNOWN_PRODUCT_TYPE')
    safe_title = name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    package = {'name': slug(name), 'version': '0.1.0', 'private': True, 'type': 'module',
               'description': one_line[:200], 'scripts': {'start': 'node server.mjs', 'test': 'node --test'}}
    return {
        'package.json': json.dumps(package, indent=2) + '\n',
        '.gitignore': 'node_modules/\n',
        'README.md': README.replace('__TITLE__', name).replace('__DESCRIPTION__', description.strip()),
        'server.mjs': SERVER.replace('__NAME__', name.replace('\\', '').replace('`', '').replace('$', '')),
        'server.test.mjs': SERVER_TEST,
        'public/index.html': INDEX.replace('__TITLE__', safe_title),
        'public/styles.css': STYLES,
        'public/app.js': APP_JS,
    }
