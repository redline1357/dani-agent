"""Live preview of a web product: its own server.mjs handle(), so pages, module scripts and API routes
work exactly as with `node server.mjs`.

The product's code runs on the host, so it is contained the same way a careful user would run it:
- Node's permission model: read-only access to the product folder only; no file writes, child processes,
  worker threads or native add-ons;
- served from HOST, a loopback address of its own: cookies are scoped by host, not port, so the
  dashboard's session cookie (host 127.0.0.1 or localhost) is never sent to the product's server, and
  the launcher also drops any Cookie/Authorization header before the product sees a request;
- started from a minimal environment;
- a kill-on-close Job Object owned by the dashboard, so closing the dashboard ends every preview;
- stopped after LIFETIME seconds, on Stop, or before the product is built again.
The dashboard frames it from another host, which is another site, so the page cannot reach the
dashboard's page or its token.

Known limit (CODEX_REVIEW_2 #2): Node's permission model has no network control, so the product's code can
open other sockets or reach the internet, exactly as when the user runs `node server.mjs` themselves. An
OS-enforced network boundary (e.g. an AppContainer without network capabilities) is the future fix.
"""
import ctypes as c
import os
from pathlib import Path
import runpy
import socket
import subprocess
import threading
import time

from .coordinator import ROOT
from . import products as PR, workspaces as W

HOST = '127.0.0.2'
LIFETIME = 15 * 60
START_SECONDS = 10
WIN_API = ROOT / 'phase0_security/final/process_checks.py'
# Port first: a product's own "run as main" check compares argv[1] with its server.mjs, so it stays idle.
LAUNCHER = """
import { createServer } from 'node:http';
import { pathToFileURL } from 'node:url';
const [port, server, host] = process.argv.slice(1);
const { handle } = await import(pathToFileURL(server).href);
if (typeof handle !== 'function') { console.error('server.mjs must export handle(req, res)'); process.exit(2); }
createServer((req, res) => {
  for (const name of ['cookie', 'authorization', 'proxy-authorization']) delete req.headers[name];
  return handle(req, res);
}).listen(Number(port), host);
"""


def free_port():
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def listening(port):
    try:
        with socket.create_connection((HOST, port), timeout=.3):
            return True
    except OSError:
        return False


class Previews:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = {}   # product id -> (process, port, started)
        self.win = runpy.run_path(str(WIN_API), run_name='preview_job_api')
        self.job = self.win['check'](self.win['createjob'](None, None))
        limits = self.win['LIMITS']()
        limits.basic.flags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.win['check'](self.win['setjob'](self.job, 9, c.byref(limits), c.sizeof(limits)))

    def url(self, product_id):
        with self.lock:
            entry = self.running.get(product_id)
            return 'http://' + HOST + ':' + str(entry[1]) + '/' if entry and entry[0].poll() is None else None

    def start(self, product):
        if product.get('kind', 'web') != 'web':
            raise ValueError('PREVIEW_IS_FOR_WEBSITES_RUN_PYTHON_APPS_FROM_THEIR_FOLDER')
        if product['state'] in ('approved', 'planning', 'building'):
            raise ValueError('PREVIEW_AFTER_THE_BUILD_FINISHES')
        app = PR.app_dir(product)
        PR.product_files(app)   # refuses a link or junction anywhere in the product
        server = PR.safe_target(app, 'server.mjs')
        if not server.is_file() or not W.NODE.is_file():
            raise RuntimeError('PREVIEW_NEEDS_SERVER_MJS_AND_NODE')
        with self.lock:
            self.stop_locked(product['id'])
            port = free_port()
            log = open(PR.folder(product['id']) / 'preview.log', 'wb')
            try:
                process = subprocess.Popen(
                    [str(W.NODE), '--permission', '--allow-fs-read=' + str(app), '--input-type=module',
                     '-e', LAUNCHER, str(port), str(server), HOST],
                    cwd=str(app), env={'SystemRoot': os.environ.get('SystemRoot', r'C:\Windows'), 'PORT': str(port)},
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            finally:
                log.close()
            if not self.win['assign'](self.job, int(process._handle)):
                process.kill()
                raise RuntimeError('PREVIEW_CONTAINMENT_FAILED')
            self.running[product['id']] = (process, port, time.monotonic())
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            if listening(port):
                return 'http://' + HOST + ':' + str(port) + '/'
            time.sleep(.1)
        self.stop(product['id'])
        raise RuntimeError('PREVIEW_SERVER_DID_NOT_START_SEE_PREVIEW_LOG')

    def stop_locked(self, product_id):
        entry = self.running.pop(product_id, None)
        if entry and entry[0].poll() is None:
            entry[0].kill()
            entry[0].wait(timeout=5)

    def stop(self, product_id):
        with self.lock:
            self.stop_locked(product_id)

    def expire(self):
        with self.lock:
            for product_id, (process, _, started) in list(self.running.items()):
                if process.poll() is not None or time.monotonic() - started > LIFETIME:
                    self.stop_locked(product_id)

    def stop_all(self):
        with self.lock:
            for product_id in list(self.running):
                self.stop_locked(product_id)
