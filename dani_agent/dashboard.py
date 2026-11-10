"""Local browser dashboard: describe a product, approve once, watch it being built, download it.

Listens on 127.0.0.1 only. Access needs the secret token from the launcher link (stored as an
HttpOnly SameSite=Strict cookie); every change also needs the token header, a local Host header and
a same-origin Origin, so other websites in the browser cannot drive it. Runs the normal coordinator.
"""
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import datetime
import base64
import binascii
import io
import json
import os
import re
from pathlib import Path
import secrets
import socket
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
import zipfile

from .coordinator import (APP, DATA, Coordinator, InstanceLock, atomic_json, check_diagnostic_plan, fingerprint,
                          import_evidence, integration_ready, recover_saved_results)
from .state import Store
from . import products as PR
from .preview import Previews

MAX_BODY = 64 * 1024
MAX_UPLOAD_BODY = 7 * 1024 * 1024  # one 5 MiB file, base64 encoded, plus bounded metadata
MAX_ZIP_FILES, MAX_ZIP_BYTES = 5000, 50 * 1024 * 1024
REVIEW_STATUS_SECONDS = 20
CONNECT_CHECK_SECONDS = 60   # sign-in and folder-permission checks start programs; reuse them for a minute
AFTER_REVIEW_SECONDS = 5
AUTO_SETUP_SECONDS = 30   # verify_offline_tests() and fingerprint() hash sources; no need to look more often
PROGRAM_CHECK_SECONDS = 60   # each look runs codex --version and claude --version
# Pauses before automatic resume number 2, 3, ... of a product that keeps stopping (the first is at once).
RESUME_WAITS = (60, 300, 900, 1800, 3600)


def seconds_since(stamp):
    try:
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float('inf')


class Server(ThreadingHTTPServer):
    """Owns its port exclusively. Python's HTTP servers set SO_REUSEADDR, which on Windows lets a second
    program bind the same 127.0.0.1 port and receive requests, including the one-time token link."""
    allow_reuse_address = False

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def same(given, expected):
    return type(given) is str and secrets.compare_digest(given.encode('utf-8', 'surrogateescape'), expected.encode())


class Dashboard:
    def __init__(self, store, engine, token, previews=None):
        self.store, self.engine, self.token = store, engine, token
        self.previews = previews or Previews()
        self.lock = threading.Lock()
        self.review_cache = (0.0, False)
        self.checks, self.found, self.connect_cache = {}, None, None
        self.tried_file = Path(store.path).parent / 'auto_setup_tried.json'
        self.auto_tried, self.auto_at = self.load_tried(), 0.0
        self.programs_at, self.programs_ok = 0.0, False   # follow_updates(): not looked yet
        self.check_lock = threading.Lock()
        self.storage_retry_at = 0.0

    def storage_pending(self):
        from . import connect
        try:
            return connect.move_journal().exists()
        except OSError:
            return True   # an unreadable recovery marker is not proof that the old runtime root is safe

    def require_storage(self):
        if self.storage_pending():
            raise ValueError('WORK_FOLDER_RECOVERY_PENDING')

    def tick(self):
        """Recover a live failed move before claiming any work; share the folder action's lock."""
        from . import connect
        with self.lock:
            if self.storage_pending():
                if time.monotonic() < self.storage_retry_at or self.engine.current:
                    return
                self.storage_retry_at = time.monotonic() + AFTER_REVIEW_SECONDS
                try:
                    outcome = connect.recover_work_root_move()
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    return   # keep the dashboard's read-only recovery status; retry on a later tick
                if outcome == 'INCOMPLETE' or self.storage_pending():
                    return
                self.checks.clear()
                self.connect_cache = None
            self.engine.tick()

    # ---- Connect page (first run) -------------------------------------------------------------
    def checked(self, name, compute, force=False):
        with self.check_lock:   # the scheduler thread (auto_setup) and the HTTP threads share this cache
            at, value = self.checks.get(name, (0.0, None))
        if force or value is None or time.monotonic() - at > CONNECT_CHECK_SECONDS:
            value = compute()   # outside the lock: a slow check never blocks the other threads
            with self.check_lock:
                self.checks[name] = (time.monotonic(), value)
        return value

    def load_tried(self):
        """Automatic checks already tried for a Dani Agent / program version; kept across restarts so a failed
        one is not retried (and a session spent) on every start."""
        try:
            return {tuple(item) for item in json.loads(self.tried_file.read_text(encoding='utf-8'))}
        except (OSError, ValueError, TypeError):
            return set()

    def tried(self, key):
        self.auto_tried.add(key)
        atomic_json(self.tried_file, sorted(list(item) for item in self.auto_tried)[-200:])

    def connect_status(self, force=False):
        from . import connect
        login = self.checked('login', connect.sign_in_status, force)
        folder = self.checked('folder', connect.work_root_status, force)
        self.connect_cache = {**connect.status(self.store, login, folder), 'found': self.found}
        return self.connect_cache

    def connect(self, action, body):
        from . import connect
        from . import model_settings as M
        if type(body) is not dict:
            raise ValueError('INVALID_REQUEST')
        self.require_storage()
        if action == 'discover':
            self.found = connect.discover()
        elif action == 'confirm':
            if self.engine.current:
                raise ValueError('WAIT_UNTIL_THE_CURRENT_TASK_FINISHES')
            connect.confirm(body.get('tool'), body.get('path'))
            self.checks.clear()
        elif action == 'login':
            connect.start_login(body.get('tool'))
        elif action == 'refresh':
            self.checks.clear()
        elif action == 'lock':
            connect.lock_work_root()
            self.checks.pop('folder', None)
        elif action == 'selftest':   # Retry after a failed automatic run
            if not any(t['kind'] == 'offline_tests' and t['state'] in ('queued', 'running') for t in self.store.tasks()):
                self.store.add('offline_tests', 'Self-test (no AI)', {})
        elif action == 'reviewer':   # Retry
            self.ensure_reviewer_profile()
        elif action == 'hostcheck':   # Retry
            self.queue_host_check('DASHBOARD_HUMAN_APPROVAL')
        elif action == 'folder':
            if self.engine.current:
                raise ValueError('WAIT_UNTIL_THE_CURRENT_TASK_FINISHES')
            self.previews.stop_all()   # their servers hold product files open
            connect.move_work_root(body.get('path'), self.store)
            self.checks.pop('folder', None)
        else:
            raise ValueError('NOT_FOUND')
        return self.connect_status()

    def approved(self, kind, objective, payload, source='DASHBOARD_HUMAN_APPROVAL'):
        self.require_live_mode()
        task_id = self.store.add(kind, objective, payload)
        self.store.approve(task_id, fingerprint())
        self.store.record(task_id, 'approval_source', {'source': source, 'scope': 'setup check: ' + objective})
        return task_id

    def ensure_reviewer_profile(self, source='DASHBOARD_HUMAN_APPROVAL'):
        """Queue (approved) a new zero-tool startup check unless the current one still verifies."""
        from . import connect
        pending = any(t['kind'] == 'reviewer_profile' and t['state'] in ('queued', 'running') for t in self.store.tasks())
        if not pending and connect.reviewer_profile_state() != 'current':
            self.approved('reviewer_profile', 'Claude zero-tool reviewer startup check', {}, source)

    def queue_host_check(self, source):
        """Codex's sandbox check on this PC; after_connect() approves the one-session diagnostic it creates."""
        self.require_live_mode()
        from . import model_settings as M
        tasks = self.store.tasks()
        if any(t['kind'] in ('codex_preflight', 'codex_diagnostic') and t['state'] in ('queued', 'running') for t in tasks):
            return
        model = M.auto_settings()['codex_model']
        for task in tasks:   # one whose approval was invalidated (e.g. Dani Agent changed) is replaced, never renewed
            if task['kind'] == 'codex_diagnostic' and task['state'] == 'awaiting_approval':
                self.store.cancel_pending(task['id'], reason='REPLACED_BY_A_NEW_SANDBOX_CHECK')
        task_id = self.store.add('codex_preflight', 'Codex sandbox check on this PC (no models)',
                                 {'model': model, 'approved_on_connect_page': True, 'fingerprint': fingerprint(), 'approval_source': source})
        self.store.record(task_id, 'approval_source', {'source': source, 'scope': 'one Codex session (sandbox check)'})

    def auto_setup(self):
        """The setup checks run by themselves once their prerequisites are met (the user chose automatic setup):
        the self-test (no AI), then Claude's no-tools startup check and Codex's sandbox check (one session each).
        Each is tried once per Dani Agent version and program version; a failed one waits for Retry.

        The slow facts (folder permissions, sign-in, self-test evidence, Claude's profile) are gathered first
        without the dashboard lock; only the decision to queue holds it, so a page action never waits long and
        nothing is queued while a page action such as a folder move runs."""
        from . import connect
        if time.monotonic() - self.auto_at < AUTO_SETUP_SECONDS:
            return
        self.auto_at = time.monotonic()
        tools = APP['TOOLS']
        if not all(name in tools and Path(tools[name]['path']).is_file() for name in connect.TOOLS):
            return
        if not self.checked('folder', connect.work_root_status).get('ready'):
            return
        version, self_test = fingerprint(), connect.self_test_current()
        login = self.checked('login', connect.sign_in_status) if self_test else {}
        profile = connect.reviewer_profile_state() if (login.get('claude') or {}).get('ok') else 'current'
        with self.lock:
            self.require_storage()
            tasks = self.store.tasks()
            active = {t['kind'] for t in tasks if t['state'] in ('awaiting_approval', 'queued', 'running')}
            if not self_test:
                if 'offline_tests' not in active and ('self_test', version) not in self.auto_tried:
                    self.tried(('self_test', version))
                    self.store.add('offline_tests', 'Self-test (no AI, automatic)', {})
                return
            if getattr(self.engine, 'offline_only', False):
                return   # no automatic startup sessions or model approvals in a model-free launch
            if login.get('environment_overrides'):
                return
            claude = ('claude_startup', version, tools['claude'].get('sha256'))
            if (login.get('claude') or {}).get('ok') and 'reviewer_profile' not in active and claude not in self.auto_tried and profile != 'current':
                self.tried(claude)
                self.ensure_reviewer_profile('AUTOMATIC_SETUP_CHECK_ENABLED_BY_USER')
            codex = ('codex_sandbox', version, tools['codex'].get('sha256'))
            running = {t['kind'] for t in tasks if t['state'] in ('queued', 'running')}
            if ((login.get('codex') or {}).get('ok') and not integration_ready(self.store) and codex not in self.auto_tried
                    and not running & {'codex_preflight', 'codex_diagnostic'}):
                self.tried(codex)
                try:
                    self.queue_host_check('AUTOMATIC_SETUP_CHECK_ENABLED_BY_USER')
                except ValueError:
                    pass   # no Codex model listed yet; the Connect page says so

    def after_connect(self):
        """Approve the one-session Codex probe whose host check the user requested on the Connect page.

        Only for the exact Dani Agent source the user clicked on, and only once: a diagnostic whose approval
        was used or invalidated (e.g. the code changed) waits for a new click instead of being renewed."""
        tasks = self.store.tasks()
        clicked = {(t['result'] or {}).get('run'): t['payload'].get('fingerprint') for t in tasks
                   if t['kind'] == 'codex_preflight' and t['state'] == 'completed'
                   and t['payload'].get('approved_on_connect_page') is True}
        sources = {(t['result'] or {}).get('run'): t['payload'].get('approval_source') or 'DASHBOARD_HUMAN_APPROVAL'
                   for t in tasks if t['kind'] == 'codex_preflight' and t['state'] == 'completed'}
        for task in tasks:
            if task['kind'] != 'codex_diagnostic' or task['state'] != 'awaiting_approval' or task['payload'].get('run') not in clicked:
                continue
            if clicked[task['payload']['run']] != fingerprint():
                continue
            with self.store.connection() as db:
                if db.execute('SELECT 1 FROM approvals WHERE task_id=?', (task['id'],)).fetchone():
                    continue
            check_diagnostic_plan(task)
            self.store.approve(task['id'], fingerprint())
            self.store.record(task['id'], 'approval_source', {'source': sources[task['payload']['run']],
                'scope': 'one Codex session to test model ' + str(task['payload'].get('model'))})

    def review_current(self):
        # verified_current() hashes every reviewed source; the dashboard polls every 2.5 s.
        checked, value = self.review_cache
        if time.monotonic() - checked > REVIEW_STATUS_SECONDS:
            from .milestone_review import verified_current
            value = verified_current()
            self.review_cache = (time.monotonic(), value)
        return value

    # ---- read model -------------------------------------------------------------------------
    def state(self):
        with self.lock:   # not while approve() is between saving the product and queueing its task
            if self.storage_pending():
                return {'products': [], 'progress': {'phase': 'work_folder_recovery_pending'},
                    'host': self.engine.host_context, 'storage_recovery': True,
                    'offline_only': getattr(self.engine, 'offline_only', False), 'review_current': False,
                    'review_task_state': None, 'integration_ready': False, 'connect_ready': False,
                    'max_steps': PR.MAX_STEPS, 'kinds': {key: value['label'] for key, value in PR.KINDS.items()}}
            PR.reconcile(self.store)
        tasks = {t['id']: t for t in self.store.tasks()}
        items = []
        for product in PR.products():
            task = tasks.get(product.get('build_task') or '')
            # cancelled_by_user from the task history too: a worker's last save can overwrite the product's own flag
            items.append({**product, 'cancelled_by_user': self.cancelled_by_user(product),
                          'task_state': task['state'] if task else None, 'open_notes': len(PR.open_notes(product)),
                          'preview_url': self.previews.url(product['id'])})
        review_task = next((t for t in tasks.values() if t['kind'] == 'milestone_review'
                            and t['state'] in ('awaiting_approval', 'queued', 'running')), None)
        return {'products': items, 'progress': self.engine.progress(), 'host': self.engine.host_context,
                'offline_only': getattr(self.engine, 'offline_only', False),
                'review_current': self.review_current(), 'review_task_state': review_task['state'] if review_task else None,
                'integration_ready': integration_ready(self.store), 'max_steps': PR.MAX_STEPS,
                'connect_ready': (self.connect_cache or {}).get('ready'),
                'connect_missing': (self.connect_cache or {}).get('missing'), 'connect_checks': (self.connect_cache or {}).get('checks'),
                'kinds': {key: value['label'] for key, value in PR.KINDS.items()}}

    # ---- actions ----------------------------------------------------------------------------
    def create(self, body):
        self.require_storage()
        if type(body) is not dict:
            raise ValueError('INVALID_REQUEST')
        name, description, steps = body.get('name', ''), body.get('description', ''), body.get('max_steps', PR.MAX_STEPS)
        product_kind = body.get('kind', 'web')
        if (type(name) is not str or type(description) is not str or type(steps) is not int
                or type(product_kind) is not str):
            raise ValueError('INVALID_PRODUCT_FIELDS')
        attachments = body.get('attachments', [])
        if type(attachments) is not list:
            raise ValueError('ATTACHMENT_IDS_INVALID')
        product = PR.create(name, description, steps, None, product_kind=product_kind,
                            **({'attachments': attachments} if attachments else {}))   # automatic models
        PR.request_build(self.store, product['id'])
        return PR.load(product['id'])

    def upload_attachment(self, body):
        self.require_storage()
        from . import attachments as A, attachment_ingest
        if type(body) is not dict or set(body) != {'name', 'data_base64'} or type(body['data_base64']) is not str:
            raise ValueError('ATTACHMENT_UPLOAD_INVALID')
        if len(body['data_base64']) > ((A.MAX_FILE_BYTES + 2) // 3) * 4:
            raise ValueError('ATTACHMENT_FILE_SIZE_INVALID')
        try:
            raw = base64.b64decode(body['data_base64'], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('ATTACHMENT_UPLOAD_INVALID') from None
        return A.upload(PR.meta_root() / 'uploads', body['name'], raw, extractor=attachment_ingest.extract)

    def remove_attachment(self, attachment_id):
        self.require_storage()
        from . import attachments as A
        A.remove(PR.meta_root() / 'uploads', attachment_id)
        return {'removed': True}

    def approve(self, product_id, source=None):
        self.require_live_mode()
        from .milestone_review import verified_current
        self.previews.stop(product_id)   # the build changes its files
        task = self.store.get(PR.request_build(self.store, product_id))
        product = PR.load(product_id)   # after request_build(), which may have saved a new build task
        if task['state'] != 'awaiting_approval':
            raise ValueError('BUILD_ALREADY_APPROVED_OR_FINISHED')
        if not integration_ready(self.store):
            raise ValueError('CODEX_HOST_DIAGNOSTIC_REQUIRED_FIRST')
        if not verified_current():
            # The same decision covers the independent review of Dani Agent's changed code; after_review()
            # approves the build only once that review has passed, on behalf of whoever decided now.
            self.ensure_review()
            product.update(state='awaiting_review', deferred_approval_source=source or 'DASHBOARD_HUMAN_APPROVAL')
            PR.save(product)
            return PR.load(product_id)
        # Mark approved BEFORE the task becomes claimable, so the worker's later states win.
        product['state'] = 'approved'
        PR.save(product)
        try:
            self.store.approve(task['id'], fingerprint())
        except Exception:
            product['state'] = 'awaiting_approval'
            PR.save(product)
            raise
        # Who decided is passed in by the caller (a click passes nothing): it is never read from the product, so
        # nothing left on a record can relabel a later click.
        self.store.record(task['id'], 'approval_source', {'source': source or 'DASHBOARD_HUMAN_APPROVAL',
            'product_id': product_id, 'limits': task['payload']['limits']})
        return PR.load(product_id)

    def auto_polish(self):
        """Review notes a build could not fit are polished automatically, with no click (the user asked for that).
        Only for products whose build already ran a polish, once per note, one product at a time; a failed attempt
        is not repeated by itself (the Polish button stays available)."""
        from .milestone_review import verified_current
        with self.lock:
            self.require_storage()
            if self.automatic_work_busy() or not integration_ready(self.store) or self.programs_changed():
                return
            version, reviewed = fingerprint(), verified_current()
            if not reviewed and ('polish_review', version) in self.auto_tried:
                return   # one review per Dani Agent version for this: a failed one is not retried, spending sessions
            for product in PR.products():
                if product['state'] != 'ready' or not any(row.get('polish') for row in product.get('steps', [])):
                    continue
                if self.cancelled_by_user(product):   # the user's Cancel turns automatic work off (CODEX_REVIEW_3 F1)
                    continue
                if product.get('resume_after') and seconds_since(product['resume_after']) < 0:
                    continue   # a yielded polish respects its quota/update pause, like a normal build
                tried = set(product.get('auto_polish_tried') or [])
                keys = {PR.note_key(n) for n in PR.open_notes(product)}
                if not keys - tried:
                    continue
                # Counted as tried only once the polish really runs (build() keeps them; end_request() gives them back).
                product.update(auto_polish_tried=sorted(tried | keys), auto_polish_pending=sorted(keys - tried))
                PR.save(product)
                if not reviewed:
                    self.tried(('polish_review', version))
                try:
                    self.polish(product['id'])
                    self.approve(product['id'], source='AUTOMATIC_POLISH_ENABLED_BY_USER')   # changed Dani Agent code is reviewed first
                except (ValueError, RuntimeError, OSError) as exc:   # undone completely: nothing is left waiting or labelled
                    failed = PR.load(product['id'])
                    task = self.store.get(failed['build_task']) if failed.get('build_task') else None
                    if task and task['state'] in ('awaiting_approval', 'queued'):
                        self.store.cancel_pending(task['id'], reason='AUTOMATIC_POLISH_COULD_NOT_START')
                    PR.end_request(failed, str(exc) or type(exc).__name__)
                    PR.save(failed)
                return

    def polish(self, product_id):
        self.require_storage()
        self.previews.stop(product_id)
        PR.request_build(self.store, product_id, mode='polish')
        return PR.load(product_id)

    def rebuild(self, product_id):
        self.require_storage()
        self.previews.stop(product_id)
        PR.request_build(self.store, product_id)
        return PR.load(product_id)

    def follow_updates(self):
        """Codex and Claude Code update themselves (2026-10-24: the Codex app's Store update removed the folder a
        StockPilot step ran from). An update of the confirmed installation is confirmed by itself
        (connect.follow_updates); looked at once a minute, the lock held only to confirm."""
        from . import connect
        if time.monotonic() - self.programs_at < PROGRAM_CHECK_SECONDS:
            return
        self.programs_at = time.monotonic()
        changed = connect.programs_changed()
        if changed:
            with self.lock:
                self.require_storage()
                connect.follow_updates()
            changed = connect.programs_changed()
        self.programs_ok = not changed

    def programs_changed(self):
        return not self.programs_ok

    def reconcile(self):
        with self.lock:   # never while a page action (e.g. approve) is between saving the product and queueing
            self.require_storage()
            PR.reconcile(self.store)

    def after_connect_locked(self):
        with self.lock:   # never while a page action (e.g. a folder move) runs
            self.require_storage()
            self.after_connect()

    def scheduled_round(self):
        """Everything the dashboard does by itself, in order. Each runs on its own: one that fails no longer skips
        the rest, and its error is kept (scheduler_errors.log) instead of being thrown away (CODEX_REVIEW_3).
        reconcile() runs here too, so a worker that died is noticed with no browser open (CODEX_REVIEW_3 F4)."""
        if self.storage_pending():
            return   # tick() owns recovery, before claims; no automatic mutations use the stale root
        for name, action in (('reconcile', self.reconcile), ('after_review', self.after_review),
                             ('after_connect', self.after_connect_locked), ('follow_updates', self.follow_updates),
                             ('auto_setup', self.auto_setup), ('auto_polish', self.auto_polish),
                             ('auto_resume', self.auto_resume), ('previews', self.previews.expire)):
            if getattr(self.engine, 'offline_only', False) and name in ('after_review', 'after_connect', 'auto_polish', 'auto_resume'):
                continue
            try:
                action()
            except Exception as exc:
                self.scheduler_error(name, exc)

    def scheduler_error(self, name, exc):
        """Keep the last scheduler errors (type, code and place only: no environment, paths of other users or
        credentials), so a paused product can be explained."""
        try:
            path = Path(self.store.path).parent / 'scheduler_errors.log'
            lines = path.read_text(encoding='utf-8').splitlines()[-199:] if path.exists() else []
            frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
            message = str(exc)
            code = message if re.fullmatch(r'[A-Z][A-Z0-9_]{0,199}', message) else None
            lines.append(json.dumps({'at': PR.now(), 'action': name, 'error': type(exc).__name__, 'code': code,
                                     'errno': getattr(exc, 'errno', None), 'winerror': getattr(exc, 'winerror', None),
                                     'where': (Path(frame.filename).name + ':' + str(frame.lineno)) if frame else None}))
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        except Exception:
            pass   # the scheduler must keep running even if its log cannot be written

    def automatic_work_busy(self):
        """Approved work is queued or running, or a product's build waits for a review that is under way. A product
        that only waits for its user's approval does not count: it runs nothing, and must not hold up another
        product's automatic resume or polish (CODEX_REVIEW_3 F5)."""
        tasks = self.store.tasks()
        if any(t['kind'] in ('product_build', 'milestone_review') and t['state'] in ('queued', 'running') for t in tasks):
            return True
        approved = {t['id'] for t in tasks if t['kind'] == 'product_build' and t['state'] == 'awaiting_approval'}
        return any(p['state'] in ('awaiting_review', 'approved') and p.get('build_task') in approved for p in PR.products())

    def cancelled_by_user(self, product):
        if product.get('cancelled_by_user'):
            return True
        try:
            return bool(product.get('build_task')) and any(
                e['kind'] == 'cancelled_by_user' for e in self.store.history(product['build_task']))
        except ValueError:
            return False

    def auto_resume(self):
        """A product never stays stopped unless its user cancelled it (2026-10-24, the owner: "product never stop
        with any reason"). The build retries its own steps; this catches what ends the build job itself (the PC
        restarted, the worker died, Dani Agent was updated mid-build, the review of Dani Agent's code failed). The
        product is built again by itself, continuing its plan, recorded as an automatic approval: at once, then after
        growing pauses (RESUME_WAITS) if it keeps stopping. After a failed review of Dani Agent's own code it waits for
        the next Dani Agent version (that version is reviewed first). Same guards as auto_polish()."""
        from .milestone_review import verified_current
        with self.lock:
            self.require_storage()
            if self.automatic_work_busy() or not integration_ready(self.store):
                return
            version, reviewed = fingerprint(), verified_current()
            if not reviewed and ('resume_review', version) in self.auto_tried:
                return
            for product in PR.products():
                reason = str(product.get('reason') or '')
                if product['state'] != 'stopped' or self.cancelled_by_user(product) or 'ATTACHMENT_' in reason:
                    continue
                if reason == 'INDEPENDENT_REVIEW_DID_NOT_PASS':
                    if product.get('auto_resumed_version') == version:
                        continue
                    product['auto_resumed_version'] = version   # once per Dani Agent version
                else:
                    if self.programs_changed():   # Codex or Claude Code updated itself: confirmed first
                        continue
                    if product.get('resume_after'):
                        # The build paused itself with a time to try again (a longer retry pause or a quota wait);
                        # it counts its own attempts, so this is not one of the pauses below.
                        if seconds_since(product['resume_after']) < 0:
                            continue
                    else:
                        count = product.get('auto_resumes', 0)
                        if count and seconds_since(product.get('auto_resumed_at')) < RESUME_WAITS[min(count - 1, len(RESUME_WAITS) - 1)]:
                            continue
                        product.update(auto_resumes=count + 1, auto_resumed_at=PR.now())
                product['resume_plan'] = True   # continue the plan already made; finished steps are kept
                source = 'AUTOMATIC_RESUME_NEVER_STOP'
                PR.save(product)
                if not reviewed:
                    self.tried(('resume_review', version))
                try:
                    self.rebuild(product['id'])
                    self.approve(product['id'], source=source)
                except (ValueError, RuntimeError, OSError) as exc:   # undone completely, like auto_polish()
                    failed = PR.load(product['id'])
                    task = self.store.get(failed['build_task']) if failed.get('build_task') else None
                    if task and task['state'] in ('awaiting_approval', 'queued'):
                        self.store.cancel_pending(task['id'], reason='AUTOMATIC_RESUME_COULD_NOT_START')
                    PR.end_request(failed, str(exc) or type(exc).__name__)
                    PR.save(failed)
                return

    def cancel(self, product_id):
        product = PR.load(product_id)
        task = self.store.get(product['build_task']) if product.get('build_task') else None
        if task:   # the user's own decision: the one stop auto_resume() never undoes (kept in the task history,
            # which a running worker's last save of the product cannot overwrite)
            self.store.record(task['id'], 'cancelled_by_user', {'product_id': product_id})
        product['cancelled_by_user'] = True
        PR.save(product)
        if task and task['state'] == 'running':
            self.engine.cancel(task['id'])
        elif task and task['state'] in ('awaiting_approval', 'queued') or product['state'] == 'awaiting_review':
            if task and task['state'] in ('awaiting_approval', 'queued'):
                self.store.cancel_pending(task['id'])
            PR.end_request(product, 'CANCELLED_BEFORE_START')   # a cancelled polish leaves the product ready
            PR.save(product)
        return PR.load(product_id)

    def review_pending(self):
        return any(t['kind'] == 'milestone_review' and t['state'] in ('awaiting_approval', 'queued', 'running')
                   for t in self.store.tasks())

    def ensure_review(self):
        if not self.review_pending():
            self.queue_review()

    def request_review(self):
        from .milestone_review import verified_current
        if verified_current():
            self.review_cache = (time.monotonic(), True)
            raise ValueError('INDEPENDENT_REVIEW_ALREADY_CURRENT')
        if self.review_pending():
            raise ValueError('INDEPENDENT_REVIEW_ALREADY_PENDING')
        return self.queue_review()

    def queue_review(self):
        self.require_live_mode()
        from .milestone_review import prepare
        payload = prepare()
        task_id = self.store.add('milestone_review', 'Independent review of current Dani Agent source', payload)
        self.store.approve(task_id, fingerprint())
        self.store.record(task_id, 'approval_source', {'source': 'DASHBOARD_HUMAN_APPROVAL',
            'scope': 'independent Claude source review', 'claude_sessions': payload.get('review_batch_count', 1),
            'retries': 0, 'seconds': None})
        self.review_cache = (0.0, False)
        return {'task_id': task_id}

    def require_live_mode(self):
        if getattr(self.engine, 'offline_only', False):
            raise ValueError('MODEL_SESSIONS_DISABLED_IN_THIS_LAUNCH')
        self.require_storage()

    def after_review(self):
        """Start builds that were approved while the review was outdated once it passes; stop them if it does not."""
        from .milestone_review import verified_current
        with self.lock:
            self.require_storage()
            waiting = [p for p in PR.products() if p['state'] == 'awaiting_review']
            if not waiting:
                return
            if verified_current():
                self.review_cache = (time.monotonic(), True)
                for product in waiting:
                    source = product.pop('deferred_approval_source', None)   # removed before it is saved again
                    product['state'] = 'awaiting_approval'
                    PR.save(product)
                    try:
                        self.approve(product['id'], source=source)
                    except (ValueError, RuntimeError, OSError) as exc:
                        product = PR.load(product['id'])
                        PR.end_request(product, str(exc) or type(exc).__name__)
                        PR.save(product)
            else:
                # claim() invalidates a queued review's old source approval just as it does a build's. Leaving
                # that task awaiting_approval would make review_pending() true forever and trap every product
                # awaiting_review. Retire it; the existing automatic resume path prepares a fresh source-bound
                # task and review. The invalidated approval is never renewed or consumed.
                outdated = False
                current = fingerprint()
                for task in self.store.tasks():
                    if (task['kind'] == 'milestone_review' and task['state'] == 'awaiting_approval'
                            and task['payload'].get('fingerprint') != current):
                        self.store.cancel_pending(task['id'], reason='APPROVAL_OUTDATED_BY_DANI_AGENT_UPDATE')
                        outdated = True
                if not self.review_pending():
                    reason = 'APPROVAL_OUTDATED_BY_DANI_AGENT_UPDATE' if outdated else 'INDEPENDENT_REVIEW_DID_NOT_PASS'
                    for product in waiting:
                        task = self.store.get(product['build_task'])
                        if task['state'] in ('awaiting_approval', 'queued'):
                            self.store.cancel_pending(task['id'], reason=reason)
                        PR.end_request(product, reason)
                        PR.save(product)

    def zip_bytes(self, product_id):
        self.require_storage()
        product = PR.load(product_id)
        app = PR.app_dir(product)
        prefix = Path(app.name)
        files = PR.product_files(app)   # a link or junction anywhere in the product refuses the zip
        if len(files) > MAX_ZIP_FILES or sum(p.stat().st_size for p in files) > MAX_ZIP_BYTES:
            raise RuntimeError('PRODUCT_TOO_LARGE_FOR_ZIP_USE_OPEN_FOLDER')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, prefix / path.relative_to(app))
        return buffer.getvalue()

    def open_folder(self, product_id):
        self.require_storage()
        os.startfile(str(PR.app_dir(PR.load(product_id))))   # a verified directory, never a file
        return {'opened': True}

    def preview(self, product_id):
        self.require_storage()
        return {'url': self.previews.start(PR.load(product_id))}

    def stop_preview(self, product_id):
        PR.folder(product_id)
        self.previews.stop(product_id)
        return {'stopped': True}


def handler_class(dashboard, port):
    hosts = {'127.0.0.1:' + str(port), 'localhost:' + str(port)}
    origins = {'http://' + h for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'DaniAgent'

        def log_message(self, *args):
            pass

        def authorized(self):
            if self.headers.get('Host') not in hosts:
                return False
            jar = cookies.SimpleCookie(self.headers.get('Cookie', ''))
            return 'dani' in jar and same(jar['dani'].value, dashboard.token)

        def send(self, status, body, kind='application/json; charset=utf-8', extra=None):
            data = body if isinstance(body, bytes) else (json.dumps(body) if kind.startswith('application/json') else body).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/':
                given = parse_qs(url.query).get('token', [''])[0]
                if self.headers.get('Host') in hosts and given and same(given, dashboard.token):
                    self.send(303, b'', 'text/plain', {'Location': '/',
                        'Set-Cookie': 'dani=' + dashboard.token + '; HttpOnly; SameSite=Strict; Path=/'})
                    return
                if not self.authorized():
                    self.send(403, LOCKED, 'text/html; charset=utf-8')
                    return
                self.send(200, PAGE.replace('__TOKEN__', dashboard.token), 'text/html; charset=utf-8',
                          {'Content-Security-Policy': "default-src 'self'; style-src 'unsafe-inline'; "
                           "script-src 'unsafe-inline'; frame-src 'self' http://127.0.0.2:*; img-src 'self' data:"})
                return
            parts = [p for p in url.path.split('/') if p]
            if not self.authorized():
                self.send(403, {'error': 'OPEN_DANI_AGENT_FROM_THE_LAUNCHER'})
                return
            try:
                if url.path == '/api/state':
                    self.send(200, dashboard.state())
                elif url.path == '/api/connect':
                    self.send(200, dashboard.connect_status())
                elif url.path == '/api/attachments':
                    from . import attachments as A
                    dashboard.require_storage()
                    self.send(200, {'files': A.drafts(PR.meta_root() / 'uploads')})
                elif len(parts) == 3 and parts[:2] == ['api', 'attachments']:
                    from . import attachments as A
                    self.send(200, A.preview(PR.meta_root() / 'uploads', parts[2]))
                elif len(parts) == 4 and parts[:2] == ['api', 'products'] and parts[3] == 'zip':
                    data = dashboard.zip_bytes(parts[2])
                    name = PR.app_name(PR.load(parts[2])) + '.zip'
                    self.send(200, data, 'application/zip', {'Content-Disposition': 'attachment; filename="' + name + '"'})
                else:
                    self.send(404, {'error': 'NOT_FOUND'})
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                self.send(400, {'error': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__})

        def do_POST(self):
            if (not self.authorized() or self.headers.get('Origin') not in origins
                    or not same(self.headers.get('X-Dani-Token', ''), dashboard.token)):
                self.send(403, {'error': 'FORBIDDEN'})
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            route = urlsplit(self.path).path
            maximum = MAX_UPLOAD_BODY if route == '/api/attachments' else MAX_BODY
            if self.headers.get('Transfer-Encoding') or not 0 <= length <= maximum:
                self.send(413, {'error': 'REQUEST_SIZE_INVALID'})
                return
            try:
                self.connection.settimeout(35)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('INCOMPLETE_REQUEST')
                body = json.loads(raw or b'{}')
                parts = [p for p in urlsplit(self.path).path.split('/') if p]
                with dashboard.lock:
                    dashboard.require_storage()
                    if parts == ['api', 'products']:
                        self.send(200, dashboard.create(body))
                    elif parts == ['api', 'attachments']:
                        self.send(200, dashboard.upload_attachment(body))
                    elif len(parts) == 4 and parts[:2] == ['api', 'attachments'] and parts[3] == 'remove':
                        self.send(200, dashboard.remove_attachment(parts[2]))
                    elif len(parts) == 4 and parts[:2] == ['api', 'products'] and parts[3] in ('approve', 'cancel', 'open', 'rebuild', 'preview', 'stop_preview', 'polish'):
                        action = {'approve': dashboard.approve, 'cancel': dashboard.cancel,
                                  'open': dashboard.open_folder, 'rebuild': dashboard.rebuild,
                                  'preview': dashboard.preview, 'stop_preview': dashboard.stop_preview,
                                  'polish': dashboard.polish}[parts[3]]
                        self.send(200, action(parts[2]))
                    elif parts == ['api', 'review']:
                        self.send(200, dashboard.request_review())
                    elif len(parts) == 3 and parts[:2] == ['api', 'connect']:
                        self.send(200, dashboard.connect(parts[2], body))
                    else:
                        self.send(404, {'error': 'NOT_FOUND'})
            except (ValueError, RuntimeError, OSError, KeyError, TypeError, AttributeError) as exc:
                self.send(400, {'error': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__})

    return Handler


def serve(port=8765, open_browser=True, *, offline_only=False):
    lock = InstanceLock(DATA / 'coordinator.lock')
    lock.acquire()
    try:
        from . import connect
        if connect.recover_work_root_move() == 'INCOMPLETE':
            raise RuntimeError('WORK_ROOT_MOVE_RECOVERY_REQUIRED')
        store = Store(DATA / 'dani_agent.sqlite3')
        recover_saved_results(store)
        import_evidence(store)
        engine = Coordinator(store, offline_only=offline_only)
        try:
            PR.mark_old_cancels()    # once: cancels from earlier versions stay cancelled (before reconcile, so a
                                     # build cut off by the last shutdown is resumed, not taken for a cancel)
            PR.reconcile(store)      # nothing runs yet: stopped builds are marked before their folders move
            PR.migrate_layout()      # products\<id>\<code> -> products\<name> + products\.dani\<id>
        except (OSError, RuntimeError, ValueError):
            pass   # a product that could not move is left as it was
        dashboard = Dashboard(store, engine, secrets.token_urlsafe(24))
        try:
            server = Server(('127.0.0.1', port), handler_class(dashboard, port))
        except OSError:
            server = Server(('127.0.0.1', 0), None)
            port = server.server_address[1]
            server.RequestHandlerClass = handler_class(dashboard, port)
        stop = threading.Event()
        def schedule():
            checked = 0.0
            while not stop.is_set():
                try:
                    dashboard.tick()
                    if time.monotonic() - checked > AFTER_REVIEW_SECONDS:
                        checked = time.monotonic()
                        dashboard.scheduled_round()
                except Exception as exc:
                    dashboard.scheduler_error('tick', exc)
                time.sleep(.5)
        threading.Thread(target=schedule, daemon=True).start()
        url = 'http://127.0.0.1:' + str(port) + '/?token=' + dashboard.token
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        try:
            server.serve_forever()
        finally:
            stop.set()
            engine.cancel()
            dashboard.previews.stop_all()
    finally:
        lock.close()


LOCKED = """<!doctype html><meta charset="utf-8"><title>Dani Agent</title>
<body style="font-family:system-ui;padding:3rem"><h1>Dani Agent</h1>
<p>Open the dashboard with <b>Start-Dani-Dashboard.cmd</b>. It opens a private link for this session.</p></body>"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="dani-token" content="__TOKEN__"><title>Dani Agent</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--text:#1b2130;--muted:#566072;--line:#dfe3ea;--field:#8a93a4;--accent:#3b5bdb;--accent-ink:#fff;--accent-soft:#eef1fd;
  --ok:#1b6e2f;--ok-bg:#e2f4e5;--warn:#8a5200;--warn-bg:#fdf0d3;--bad:#b3261e;--bad-bg:#fde8e6;--chip:#eceff4;--focus:#1c3fd1}
@media (prefers-color-scheme:dark){:root{--bg:#13161b;--panel:#1b1f26;--text:#e7eaf0;--muted:#a3acbb;--line:#2e343f;--field:#737c8d;--accent:#8fa4ff;--accent-ink:#0d1433;--accent-soft:#222943;
  --ok:#86dd93;--ok-bg:#163421;--warn:#f3c56b;--warn-bg:#3a2b09;--bad:#ffa198;--bad-bg:#451a1b;--chip:#272c36;--focus:#b9c5ff}}
*{box-sizing:border-box}body{margin:0;font:15px/1.55 system-ui,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
header{display:flex;align-items:center;gap:.9rem;padding:.7rem 1.5rem;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:2}
header h1{font-size:1.1rem;margin:0;white-space:nowrap}header .tag{color:var(--muted);font-size:.9rem}
header .status{margin-inline-start:auto;display:inline-flex;align-items:center;gap:.45rem;font-size:.85rem;font-weight:600;color:var(--muted);padding:.2rem .7rem;border-radius:999px;background:var(--chip)}
header .status::before{content:"";width:.5rem;height:.5rem;border-radius:50%;background:currentColor;flex:none}
header .status.busy{color:var(--warn);background:var(--warn-bg)}header .status.down{color:var(--bad);background:var(--bad-bg)}
.layout{display:grid;grid-template-columns:288px minmax(0,1fr);min-height:calc(100vh - 53px)}
aside{border-inline-end:1px solid var(--line);padding:1rem;background:var(--panel)}.nav{display:grid;gap:.5rem}#list{margin-top:1rem}
main{padding:1.5rem;max-width:960px;width:100%;min-width:0;overflow-wrap:anywhere}
h2{font-size:1.35rem;line-height:1.25;margin:0}h3{font-size:1.02rem;margin:0 0 .6rem}p{margin:.6rem 0}a{color:var(--accent)}
button{font:inherit;font-weight:500;min-height:2.35rem;border:1px solid var(--line);background:var(--panel);color:var(--text);padding:.4rem .9rem;border-radius:8px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--field)}button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
button.primary:hover:not(:disabled){filter:brightness(1.1)}button:disabled{opacity:.55;cursor:default}
a.button{display:inline-flex;align-items:center;min-height:2.35rem;border:1px solid var(--line);background:var(--panel);color:var(--text);padding:.4rem .9rem;border-radius:8px;font-weight:500;text-decoration:none}a.button:hover{border-color:var(--field)}
button.link{min-height:0;border:0;padding:0;background:none;color:var(--accent);font-weight:600;text-decoration:underline}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.item{display:flex;flex-direction:column;align-items:flex-start;gap:.2rem;width:100%;text-align:start;border:1px solid transparent;background:none;padding:.5rem .7rem;margin:.15rem 0}
.item:hover:not(:disabled){background:var(--chip);border-color:transparent}.item.sel,.item.sel:hover:not(:disabled){background:var(--accent-soft);border-color:var(--accent)}
input,textarea,select{font:inherit;width:100%;padding:.55rem .65rem;border:1px solid var(--field);border-radius:8px;background:var(--panel);color:var(--text)}
textarea{resize:vertical}input[type=file]{padding:.4rem;border-style:dashed}
label{display:block;margin:1rem 0 .3rem;font-weight:600}.hint{color:var(--muted);font-size:.88rem;margin:.35rem 0 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1.15rem 1.25rem;margin-bottom:1rem}
.chip{display:inline-block;padding:.08rem .55rem;border-radius:999px;background:var(--chip);font-size:.78rem;font-weight:600;color:var(--muted)}
.chip.ok{color:var(--ok);background:var(--ok-bg)}.chip.warn{color:var(--warn);background:var(--warn-bg)}.chip.bad{color:var(--bad);background:var(--bad-bg)}
.muted{color:var(--muted)}.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
.row input,.row select{width:auto;flex:1 1 14rem;min-width:0}.row select{flex:0 0 auto}
.desc{white-space:pre-wrap;margin:.7rem 0 0}.desc.clamp{max-height:7.8em;overflow:hidden;-webkit-mask-image:linear-gradient(#000 55%,transparent);mask-image:linear-gradient(#000 55%,transparent)}
.scope{border:1px solid var(--line);border-inline-start:3px solid var(--accent);border-radius:8px;padding:.7rem .9rem;margin:.9rem 0;background:var(--accent-soft)}
.scope b{display:block;margin-bottom:.2rem}details{margin:.9rem 0}summary{cursor:pointer;font-weight:600;color:var(--muted)}details p{color:var(--muted);font-size:.92rem}
.notice{border-radius:8px;padding:.65rem .9rem;margin:.9rem 0;background:var(--warn-bg);border:1px solid var(--warn)}.notice.bad{background:var(--bad-bg);border-color:var(--bad)}
.actions{margin-top:1rem}.progress{height:.45rem;border-radius:999px;background:var(--chip);overflow:hidden;margin:.4rem 0 1rem}
.progress span{display:block;height:100%;border-radius:999px;background:var(--ok)}
.steps{list-style:none;margin:0;padding:0}.steps li{display:grid;grid-template-columns:1.8rem minmax(0,1fr);gap:.7rem;padding:.8rem 0;border-top:1px solid var(--line)}
.steps li:first-child{border-top:0;padding-top:.2rem}.steps li:last-child{padding-bottom:.1rem}
.num{width:1.8rem;height:1.8rem;border-radius:50%;background:var(--chip);color:var(--muted);font-weight:700;font-size:.82rem;display:grid;place-items:center}
.step-done .num{background:var(--ok-bg);color:var(--ok)}.step-running .num,.step-retrying .num{background:var(--warn-bg);color:var(--warn)}.step-failed .num{background:var(--bad-bg);color:var(--bad)}
.head{display:flex;flex-wrap:wrap;gap:.25rem .6rem;align-items:center}.meta{color:var(--muted);font-size:.88rem;margin-top:.15rem}
.files,code{font:.84rem ui-monospace,Consolas,monospace}.files{color:var(--muted);margin-top:.15rem}code{background:var(--chip);padding:.1rem .35rem;border-radius:5px}
.notes{margin:.3rem 0 0;padding-inline-start:1.1rem;color:var(--muted);font-size:.88rem}.steps .notes li{display:list-item;padding:.1rem 0;border:0}
.banner{display:flex;flex-wrap:wrap;gap:.5rem .8rem;align-items:center;padding:.75rem 1rem;border-radius:10px;border:1px solid var(--line);border-inline-start:3px solid var(--warn);background:var(--panel);margin-bottom:1rem}
.banner>span{flex:1 1 16rem}.err{color:var(--bad);min-height:1.2em}
.running{display:flex;align-items:center;gap:.7rem;padding:.8rem 1.1rem;border-radius:10px;margin-bottom:1rem;
  background:var(--warn-bg);border:2px solid var(--warn);font-weight:700;font-size:1.02rem}
.running .dot{width:.75rem;height:.75rem;border-radius:50%;background:var(--warn);flex:none;animation:pulse 1.2s ease-in-out infinite}
.running .sub{font-weight:500}.running.waiting{border-width:1px}.running.waiting .dot{animation:none;opacity:.55}
@keyframes pulse{50%{opacity:.25}}@media (prefers-reduced-motion:reduce){.running .dot{animation:none}}
iframe{width:100%;height:520px;border:1px solid var(--line);border-radius:10px;background:#fff}pre{white-space:pre-wrap;margin:.3rem 0;font:.84rem/1.5 ui-monospace,Consolas,monospace}
@media (max-width:760px){header{padding:.6rem 1rem;gap:.6rem}header .tag{display:none}.layout{grid-template-columns:minmax(0,1fr)}
  aside{border-inline-end:0;border-bottom:1px solid var(--line);padding:.75rem 1rem}.nav{grid-template-columns:1fr 1fr}
  #list{display:flex;gap:.4rem;overflow-x:auto;margin-top:.7rem;padding:2px 2px .35rem}#list p{margin:0}
  .item{flex:0 0 auto;width:auto;max-width:72vw;margin:0;border-color:var(--line)}
  main{padding:1rem}.card{padding:1rem}button,a.button,input,select{min-height:2.75rem}button.link{min-height:0}iframe{height:420px}}
</style></head><body>
<header><h1>Dani Agent</h1><span class="tag">describe it &rarr; approve once &rarr; get the product</span><span class="status" id="status" role="status"></span></header>
<div class="layout"><aside><div class="nav"><button class="primary" id="new">+ New product</button>
<button id="connect">Connect accounts</button></div><nav id="list" aria-label="Products"></nav></aside>
<main><div id="main"></div><div id="previewBox"></div></main></div>
<script>
const TOKEN = document.querySelector('meta[name=dani-token]').content;
let state = null, selected = null, creating = false, connecting = false, conn = null, connErr = '';
const emptyDraft = () => ({name: '', description: '', kind: 'web', attachments: [], previews: {}, expanded: {}, unused: null, busy: false, error: ''});
let draft = emptyDraft();
try {
  const saved = JSON.parse(sessionStorage.getItem('dani-project-draft') || 'null');
  if (saved && typeof saved.name === 'string' && typeof saved.description === 'string'
      && typeof saved.kind === 'string' && Array.isArray(saved.attachments) && saved.attachments.length <= 5
      && saved.attachments.every(f => f && /^[0-9a-f]{32}$/.test(f.id) && typeof f.name === 'string'
        && Number.isInteger(f.size) && Number.isInteger(f.characters))) {
    draft = {...emptyDraft(), name: saved.name.slice(0, 60), description: saved.description.slice(0, 4000),
      kind: saved.kind, attachments: saved.attachments};
    creating = !!(draft.name || draft.description || draft.attachments.length);
  }
} catch {}
const saveDraft = () => { try { sessionStorage.setItem('dani-project-draft', JSON.stringify({
  name: draft.name, description: draft.description, kind: draft.kind, attachments: draft.attachments})); } catch {} };
const el = (tag, props = {}, ...kids) => { const n = document.createElement(tag); Object.assign(n, props); kids.flat().forEach(k => { if (k !== null && k !== undefined && k !== false) n.append(k); }); return n; };
async function post(path, body) {
  const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Dani-Token': TOKEN}, body: JSON.stringify(body || {})});
  const data = await r.json(); if (!r.ok) throw new Error(data.error || r.status); return data;
}
const LABEL = {awaiting_approval: ['Waiting for your approval', 'warn'], awaiting_review: ['Checking Dani Agent first', 'warn'], approved: ['Approved, starting soon', 'warn'], planning: ['Planning', 'warn'],
  building: ['Building', 'warn'], ready: ['Ready', 'ok'], stopped: ['Paused, resumes by itself', 'warn'], cancelled: ['Cancelled', 'bad']};
LABEL.needs_attention = ['Needs attention', 'bad'];
const stateOf = p => p.state === 'stopped' && p.cancelled_by_user ? 'cancelled'
  : p.state === 'stopped' && (p.reason || '').includes('ATTACHMENT_') ? 'needs_attention' : p.state;
const chip = s => { const [t, c] = LABEL[s] || [s, '']; return el('span', {className: 'chip ' + c, textContent: t}); };
const human = r => (r || '').replaceAll('_', ' ').toLowerCase();
const REASONS = [
  ['ATTACHMENT_', 'A reference file is missing, changed, or cannot be read safely. Automatic work is paused. Check the attached documents before trying again.'],
  ['INTERNAL_ERROR', 'Dani Agent itself hit an unexpected error; your product is not at fault. It builds again by itself once Dani Agent is updated.'],
  ['PROGRAM_UPDATED_DURING_BUILD', 'Codex or Claude Code updated itself during the build. Dani Agent confirms the new version and builds again by itself.'],
  ['COMMAND_TOOL_UNAVAILABLE', 'Codex could not run commands on this PC (for example while the Codex app was updating). Dani Agent builds again by itself once Codex is ready.'],
  ['DEVELOPER_BLOCKED', 'Codex stopped this step without changing anything. Dani Agent tries the step again by itself.'],
  ['DEVELOPER_OUTSIDE_SCOPE', 'Codex tried to change files outside this step\'s plan, so that attempt was discarded. Dani Agent tries again by itself.'],
  ['TESTS_FAILED_CORRECTION_LIMIT', 'The tests still failed after 2 correction rounds. Dani Agent tries the step again by itself, with a stronger model if needed.'],
  ['CORRECTION_LIMIT_REACHED', 'Claude still found problems after 2 correction rounds. Dani Agent tries the step again by itself.'],
  ['QUOTA_EXHAUSTED', 'Your Codex or Claude subscription limit is used up for now. Dani Agent waits and goes on by itself.'],
  ['MODEL_NOT_IN_YOUR_PLAN', 'Your plan does not include the model Dani Agent picked for this step. It picks another one and goes on by itself.'],
  ['CHANGED_OUTSIDE_DANI_AGENT', 'The product folder was edited outside Dani Agent during the build. Your edits are kept and the build goes on from them.'],
  ['ORIGINAL_PROJECT_CHANGED', 'The product folder was edited outside Dani Agent during the build. Your edits are kept and the build goes on from them.'],
  ['INDEPENDENT_MILESTONE_APPROVAL_REQUIRED', 'Dani Agent\'s code changed during the build. Its new code is checked first, then the build goes on by itself.'],
  ['INDEPENDENT_REVIEW_DID_NOT_PASS', 'The independent check of Dani Agent\'s own code did not pass, so nothing was built. It builds again by itself once a fixed Dani Agent version passes the check.'],
  ['USER_CANCELLED', 'You cancelled the build.'], ['CANCELLED_BEFORE_START', 'You cancelled the build before it started.'],
  ['SELF_TEST_ALREADY_RUNNING', 'A self-test was already running on this PC, so a second one was not started. Its result shows when it finishes.'],
  ['OFFLINE_TESTS_STALLED', 'The self-test printed nothing for 10 minutes, so Dani Agent stopped it and everything it started.'],
  ['SOURCE_CHANGED_DURING_APPROVED_WORKFLOW', 'Dani Agent was updated during the build. Its new code is checked first, then the build goes on by itself.'],
  ['APPROVAL_OUTDATED_BY_DANI_AGENT_UPDATE', 'Dani Agent was updated after you approved and before the build started. Its new code is checked first, then the build goes on by itself.'],
  ['PROJECT_CHANGE_TOO_LARGE_TO_REVIEW', 'This step\'s change was too large to review in one go. Dani Agent tries the step again, asking for a smaller change.'],
  ['STALLED', 'A session showed no sign of work for 20 minutes. Dani Agent tries again by itself.'],
  ['TIMEOUT', 'A step was stopped by the time limit older versions of Dani Agent had. There is none now; the build goes on by itself.'],
];
const explain = r => { for (const [key, text] of REASONS) if ((r || '').includes(key)) return text; return human(r); };
const STEP = {done: ['Done', 'ok'], running: ['Running', 'warn'], retrying: ['Trying again', 'warn'], failed: ['Failed', 'bad'], pending: ['Not started', '']};
const stepChip = s => { const [t, c] = STEP[s] || [s, '']; return el('span', {className: 'chip ' + c, textContent: t}); };
let listShown = null;
function renderList() {
  const list = document.getElementById('list');
  const focused = list.contains(document.activeElement) ? document.activeElement.id : null;
  list.replaceChildren();
  if (!state.products.length) list.append(el('p', {className: 'muted', textContent: 'No products yet.'}));
  // Real buttons, so keyboard and screen-reader users can pick any product (CODEX_REVIEW_3 F11).
  let shown = null;
  for (const p of state.products) {
    const current = p.id === selected && !creating && !connecting;
    const item = el('button', {id: 'product-' + p.id, type: 'button', className: 'item' + (current ? ' sel' : ''),
      onclick: () => { selected = p.id; creating = false; connecting = false; render(); }}, el('bdi', {textContent: p.name}), el('small', {}, chip(stateOf(p))));
    if (current) { item.setAttribute('aria-current', 'page'); shown = item; }
    list.append(item);
  }
  if (focused) document.getElementById(focused)?.focus({preventScroll: true});
  // On a phone the list is one scrolling row: bring a newly chosen product into view, once.
  if (shown && listShown !== selected && shown.scrollIntoView) shown.scrollIntoView({block: 'nearest', inline: 'nearest'});
  if (shown) listShown = selected;
}
// An action's error stays until that action succeeds, through every 2.5-second redraw (CODEX_REVIEW_3 F10).
const actionErrors = {};
// A click that is still being answered keeps its buttons disabled through every redraw (2026-11-10: Approve took
// several seconds, the redraw re-enabled it, and it looked as if nothing had happened and could be sent twice).
const pending = {};
// Folded text and long descriptions stay as the user left them through every redraw.
const opened = {};
const fold = (key, title, ...kids) => el('details', {open: !!opened[key], ontoggle: e => { opened[key] = e.target.open; }}, el('summary', {textContent: title}), ...kids);
const ROLE = {codex: 'writing the code', claude: 'reviewing the code', tests: 'running the tests'};
const PHASE = {product_step: 'Building', product_planning: 'Planning', product_retrying: 'Waiting to try again', claude_milestone_review: 'Checking Dani Agent'};
const KIND = {offline_tests: 'Self-test', reviewer_profile: 'Claude startup check', codex_preflight: 'Codex sandbox check', codex_diagnostic: 'Codex sandbox check',
  milestone_review: 'Checking Dani Agent', product_build: 'Building'};
const SETUP = {offline_tests: 'the self-test: Dani Agent tests its own safety rules on this PC (no AI)', reviewer_profile: 'the Claude startup check (1 tiny session)',
  codex_preflight: 'the Codex sandbox check', codex_diagnostic: 'the Codex sandbox check (1 tiny session)'};
const quiet = g => !g.phase || ['idle', 'finished', 'progress_pending'].includes(g.phase) || g.phase.startsWith('normal_');
function nowRunning() {
  const g = state.progress || {};
  if (quiet(g)) return null;
  const product = state.products.find(x => x.id === g.product), name = product ? product.name + ': ' : '';
  const waiting = g.phase === 'product_retrying' && g.wait_seconds > 0;   // nothing runs during the pause
  const what = g.phase === 'claude_milestone_review' ? 'checking Dani Agent\'s own code'
    : g.phase === 'product_planning' ? 'planning ' + (product ? product.name : 'the product')
    : g.phase === 'product_step' ? name + 'step ' + g.step + ' of ' + g.of + ', ' + (ROLE[g.role] || human(g.role))
    : g.phase === 'product_retrying' ? name + g.what + ', attempt ' + g.attempt + (waiting ? ', in about ' + Math.ceil(g.wait_seconds / 60) + ' min' : '') + ' \u2014 ' + explain(g.reason)
    : !g.product && SETUP[g.task_kind] ? SETUP[g.task_kind]
    : human(g.phase);
  const who = waiting ? 'Waiting to try again'
    : 'Now running: ' + (g.model ? g.agent + ' ' + g.model + ' \u00b7 effort ' + g.effort : g.role === 'tests' ? 'Tests (no AI model)' : 'Dani Agent');
  return el('div', {className: 'running' + (waiting ? ' waiting' : ''), role: 'status'}, el('span', {className: 'dot'}),
    el('span', {}, who, el('span', {className: 'sub', textContent: ' \u2014 ' + what})));
}
function banner() {
  // The review runs by itself when a build is approved; only show it while it is happening.
  if (state.review_current || !state.review_task_state) return null;
  return el('div', {className: 'banner', role: 'status'}, el('span', {}, el('b', {textContent: 'Checking Dani Agent\'s own code. '}),
    'Dani Agent changed since it was last checked. Its source is reviewed in up to 3 Claude sessions, with no review retries. Your build starts by itself only if every part passes.'));
}
// The checks Dani Agent runs by itself. While only these are unfinished the user has nothing to connect
// (2026-10-29: "Finish connecting your accounts" showed during the self-test after an update).
const AUTO_CHECKS = {self_test: 'the self-test', claude_startup: 'the Claude startup check', codex_sandbox: 'the Codex sandbox check'};
const needsUser = v => !v.missing || !v.missing.length || v.missing.some(k => !(k in AUTO_CHECKS) || (v.checks || {})[k] === 'failed');
function connectBanner() {
  if (state.connect_ready !== false) return null;
  const view = {missing: state.connect_missing, checks: state.connect_checks};
  const auto = (view.missing || []).length > 0 && view.missing.every(k => k in AUTO_CHECKS);   // only Dani Agent's own checks are open
  const open = el('button', {textContent: auto ? 'Show the checks' : 'Connect accounts', onclick: () => { connecting = true; render(); }});
  if (auto && !needsUser(view)) return el('div', {className: 'banner', role: 'status'}, el('span', {}, el('b', {textContent: 'Dani Agent is checking itself. '}),
    'Your accounts are connected. After an update, Dani Agent runs ' + view.missing.map(k => AUTO_CHECKS[k]).join(', then ')
    + ' again before it builds. This can take a while; nothing is needed from you.'), open);
  const failed = (view.missing || []).filter(k => (view.checks || {})[k] === 'failed');
  if (auto && failed.length) return el('div', {className: 'banner'}, el('span', {}, el('b', {textContent: 'A safety check did not pass. '}),
    'Building waits until ' + AUTO_CHECKS[failed[0]] + ' passes. Open the checks and choose Retry.'), open);
  return el('div', {className: 'banner'}, el('span', {textContent: 'Finish connecting your Claude and Codex accounts before building.'}), open);
}
const UPLOAD_ERRORS = {
  ATTACHMENT_PDF_SUPPORT_NOT_INSTALLED: 'PDF support is not installed yet. Use Word (.docx) or text, or install the approved PDF reader.',
  ATTACHMENT_NORMAL_HOST_REQUIRED: 'Open Dani Agent from its Windows launcher to read documents safely.',
  ATTACHMENT_SANDBOX_SETUP_REQUIRED: 'Connect your local Codex and Python programs before uploading documents.',
  ATTACHMENT_PDF_NO_TEXT_OCR_UNSUPPORTED: 'This PDF has no readable text. Scanned pages need OCR first; upload a text-based PDF or Word document.',
  ATTACHMENT_PDF_ENCRYPTED_UNSUPPORTED: 'Password-protected PDFs are not supported. Upload an unencrypted copy.',
  ATTACHMENT_TEXT_TOO_LARGE: 'This document exceeds 25,000 text characters. Shorten it or upload a focused excerpt.',
  ATTACHMENT_CONTEXT_TOO_LARGE: 'The combined document context is too large. Use fewer files or shorter excerpts.',
  ATTACHMENT_FILE_SIZE_INVALID: 'Each file must be nonempty and at most 5 MiB.',
  ATTACHMENT_EXTRACTION_FAILED: 'This document could not be read safely. Check the format or upload a text export.'
};
const uploadError = code => UPLOAD_ERRORS[code] || human(code);
function renderNew() {
  const current = draft;
  const err = el('div', {className: 'err', textContent: current.error}); err.setAttribute('role', 'alert');
  const name = el('input', {id: 'product-name', maxLength: 60, dir: 'auto', value: current.name, placeholder: 'e.g. Mug Shop', oninput: e => { current.name = e.target.value; saveDraft(); }});
  const desc = el('textarea', {id: 'product-description', rows: 9, maxLength: 4000, dir: 'auto', value: current.description,
    placeholder: 'Describe the product, or attach a specification below. Add any priorities or instructions here.', oninput: e => { current.description = e.target.value; saveDraft(); }});
  const kind = el('select', {id: 'product-kind', onchange: e => { current.kind = e.target.value; saveDraft(); }}, Object.entries(state.kinds).map(([k, label]) => el('option', {value: k, textContent: label})));
  kind.value = current.kind;
  const uploads = el('div', {id: 'draft-attachments'});
  const unused = el('div', {id: 'unused-attachments'});
  const uploadStatus = el('p', {className: 'muted', textContent: current.busy ? 'Reading document safely…' : ''}); uploadStatus.setAttribute('role', 'status');
  function drawUnused() {
    unused.replaceChildren();
    if (current.unused === null) return;
    const available = current.unused.filter(file => !current.attachments.some(f => f.id === file.id));
    unused.append(el('p', {className: 'muted', textContent: available.length ? 'Files from unfinished drafts. Reuse one or remove it from local storage.' : 'No other unused uploads.'}));
    for (const file of available) unused.append(el('div', {className: 'row'}, el('bdi', {textContent: file.name}),
      el('button', {textContent: 'Use ' + file.name, disabled: current.busy, onclick: () => {
        if (current.busy) return;
        if (current.attachments.length >= 5) { current.error = 'Attach up to 5 files.'; err.textContent = current.error; return; }
        current.attachments.push(file); saveDraft(); drawFiles();
      }}), el('button', {textContent: 'Delete unused ' + file.name, disabled: current.busy, onclick: async () => {
        if (current.busy) return; current.busy = true; drawUnused();
        try { await post('/api/attachments/' + file.id + '/remove'); current.unused = current.unused.filter(f => f.id !== file.id); }
        catch (x) { current.error = uploadError(x.message); err.textContent = current.error; }
        finally { current.busy = false; drawUnused(); }
      }})));
  }
  function drawFiles() {
    uploads.replaceChildren();
    for (const file of current.attachments) {
      const preview = el('pre', {style: 'max-height:12rem;overflow:auto', dir: 'auto', hidden: !current.expanded[file.id], textContent: current.previews[file.id] || ''});
      uploads.append(el('div', {className: 'card', style: 'margin:.6rem 0'}, el('b', {}, el('bdi', {textContent: file.name})),
        el('p', {className: 'hint', style: 'margin-bottom:.6rem', textContent: Math.ceil(file.size / 1024) + ' KiB · ' + file.characters.toLocaleString() + ' text characters'}),
        el('div', {className: 'row'},
          el('button', {type: 'button', textContent: current.expanded[file.id] ? 'Hide extracted text' : 'View extracted text', disabled: current.busy, onclick: async e => {
            try { const response = await fetch('/api/attachments/' + file.id); const data = await response.json();
              if (!response.ok) throw new Error(data.error); current.previews[file.id] = data.text; current.expanded[file.id] = !current.expanded[file.id]; preview.textContent = data.text; preview.hidden = !current.expanded[file.id];
              e.target.textContent = preview.hidden ? 'View extracted text' : 'Hide extracted text'; }
            catch (x) { current.error = uploadError(x.message); err.textContent = current.error; }
          }}),
          el('button', {type: 'button', textContent: 'Remove ' + file.name, disabled: current.busy, onclick: async () => {
            if (current.busy) return; current.busy = true; drawFiles();
            try { await post('/api/attachments/' + file.id + '/remove'); current.attachments = current.attachments.filter(f => f.id !== file.id); if (current.unused) current.unused = current.unused.filter(f => f.id !== file.id); saveDraft(); }
            catch (x) { current.error = uploadError(x.message); err.textContent = current.error; }
            finally { current.busy = false; drawFiles(); }
          }})), preview));
    }
    drawUnused();
  }
  const fileInput = el('input', {id: 'product-files', type: 'file', multiple: true, accept: '.pdf,.docx,.txt,.md,.csv,.json', disabled: current.busy});
  const createButton = el('button', {className: 'primary', textContent: 'Create draft', disabled: current.busy, onclick: async e => {
    if (current.busy) return;
    current.busy = true; e.target.disabled = true; current.error = ''; err.textContent = '';
    try { const p = await post('/api/products', {name: current.name, description: current.description, kind: current.kind,
        attachments: current.attachments.map(f => f.id)});
      selected = p.id; creating = false; draft = emptyDraft(); saveDraft(); await refresh(); }
    catch (x) { current.error = uploadError(x.message); err.textContent = current.error; }
    finally { current.busy = false; e.target.disabled = false; }
  }});
  fileInput.onchange = async () => {
    if (current.busy) return;
    const files = Array.from(fileInput.files || []); fileInput.value = '';
    if (files.length + current.attachments.length > 5) { current.error = 'Attach up to 5 files.'; err.textContent = current.error; return; }
    current.busy = true; current.error = ''; err.textContent = ''; createButton.disabled = true; fileInput.disabled = true; drawFiles();
    try {
      for (const file of files) {
        if (!file.size || file.size > 5 * 1024 * 1024) throw new Error('ATTACHMENT_FILE_SIZE_INVALID');
        uploadStatus.textContent = 'Reading ' + file.name + ' safely…';
        const data = await new Promise((resolve, reject) => { const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('ATTACHMENT_UPLOAD_READ_FAILED')); reader.readAsDataURL(file); });
        const added = await post('/api/attachments', {name: file.name, data_base64: data});
        current.attachments.push(added); saveDraft(); drawFiles();
      }
    } catch (x) { current.error = uploadError(x.message); err.textContent = current.error; }
    finally { current.busy = false; createButton.disabled = false; fileInput.disabled = false; uploadStatus.textContent = current.attachments.length + ' file(s) attached'; drawFiles(); }
  };
  drawFiles();
  return el('div', {className: 'card'}, el('h2', {textContent: 'New product'}),
    el('p', {className: 'muted', textContent: 'Dani Agent plans the build with Claude, then Codex builds it step by step. Every step is tested and reviewed. Websites use plain HTML/CSS/JavaScript with Node.js built-ins; Python apps use the standard library (no package installs).'}),
    el('label', {textContent: 'Type', htmlFor: 'product-kind'}), kind, el('label', {textContent: 'Name', htmlFor: 'product-name'}), name,
    el('label', {textContent: 'What should it do?', htmlFor: 'product-description'}), desc,
    el('label', {textContent: 'Reference files (optional)', htmlFor: 'product-files'}), fileInput,
    el('p', {className: 'hint', textContent: 'PDF, Word (.docx), TXT, Markdown, CSV or JSON. Up to 5 files, 5 MiB each; 25,000 text characters per file and 50,000 combined including file labels. Text only: images and scanned pages are not read.'}),
    el('p', {className: 'hint', textContent: 'Files are read locally. After you approve building, their text is shared with your planner, developer and reviewer as project context. This does not train or fine-tune a model. Do not attach passwords or API keys.'}),
    uploadStatus, uploads,
    el('button', {type: 'button', textContent: 'Manage unused uploads', disabled: current.busy, onclick: async () => {
      if (current.busy) return; current.busy = true;
      try { const response = await fetch('/api/attachments'); const data = await response.json();
        if (!response.ok) throw new Error(data.error); current.unused = data.files; }
      catch (x) { current.error = uploadError(x.message); err.textContent = current.error; }
      finally { current.busy = false; drawUnused(); }
    }}), unused,
    el('p', {className: 'hint', style: 'margin-top:1rem', textContent: 'Creating a draft builds nothing and uses no AI session. The next screen shows what you approve; Claude then decides how many build steps the product needs (usually 2-8).'}), err,
    el('div', {className: 'row', style: 'margin-top:1rem'}, createButton,
      el('button', {textContent: 'Cancel', disabled: current.busy, onclick: async () => {
        if (current.busy) return; current.busy = true;
        try { for (const f of [...current.attachments]) {
          try { await post('/api/attachments/' + f.id + '/remove'); }
          catch (x) { if (x.message !== 'ATTACHMENT_ALREADY_BOUND') throw x; } // A lost create response never deletes the published product's evidence.
          current.attachments = current.attachments.filter(item => item.id !== f.id); saveDraft();
        } draft = emptyDraft(); saveDraft(); creating = false; render(); }
        catch (x) { current.error = uploadError(x.message); err.textContent = current.error; }
        finally { current.busy = false; drawFiles(); }
      }})));
}
function renderProduct(p) {
  const b = p.budget, s = b.settings, err = el('div', {className: 'err', textContent: actionErrors[p.id] || ''});
  err.setAttribute('role', 'alert');
  const busy = pending[p.id];
  const act = (label, path, primary) => el('button', {className: primary ? 'primary' : '', textContent: busy === path ? 'Working\u2026' : label, disabled: !!busy, onclick: async e => {
    if (pending[p.id]) return;
    pending[p.id] = path; err.textContent = '';
    for (const other of actions.children) other.disabled = true;
    e.target.textContent = 'Working\u2026';
    try { await post(path); delete actionErrors[p.id]; } catch (x) { actionErrors[p.id] = label + ': ' + explain(x.message); }
    delete pending[p.id]; await refresh(); if (!typing()) render(); }});
  const actions = el('div', {className: 'row actions'});
  const web = (p.kind || 'web') === 'web', ready = p.state === 'ready';
  const rows = p.steps || [], done = rows.filter(st => st.state === 'done').length, built = ready || done > 0;
  const cancelled = p.state === 'stopped' && p.cancelled_by_user, attention = stateOf(p) === 'needs_attention';
  if (p.state === 'awaiting_approval') actions.append(act('Approve and build', '/api/products/' + p.id + '/approve', true));
  if (['awaiting_approval', 'awaiting_review', 'approved', 'planning', 'building'].includes(p.state)) actions.append(act('Cancel', '/api/products/' + p.id + '/cancel'));
  // A paused build resumes by itself, so Build again is the main action only when it will not.
  if (p.state === 'stopped') actions.append(act('Build again', '/api/products/' + p.id + '/rebuild', cancelled || attention));
  // Before the first finished step there is only the empty starter: nothing to preview or download yet.
  if (web && built && !p.preview_url && !['approved', 'planning', 'building'].includes(p.state)) actions.append(act('Preview', '/api/products/' + p.id + '/preview', ready));
  if (p.preview_url) actions.append(act('Stop preview', '/api/products/' + p.id + '/stop_preview'));
  if (ready && p.open_notes) actions.append(act('Polish (Codex acts on ' + p.open_notes + ' review note' + (p.open_notes > 1 ? 's' : '') + ')', '/api/products/' + p.id + '/polish'));
  if (built) actions.append(el('a', {className: 'button', href: '/api/products/' + p.id + '/zip', textContent: 'Download zip'}));
  actions.append(act('Open folder', '/api/products/' + p.id + '/open', ready && !web));
  const steps = el('ol', {className: 'steps'});
  rows.forEach((st, i) => {
    const body = el('div', {}, el('div', {className: 'head'}, el('b', {dir: 'auto', textContent: st.title}), stepChip(st.state)));
    if ((st.files || []).length) body.append(el('div', {className: 'files', textContent: st.files.join(', ')}));
    if (st.difficulty) body.append(el('div', {className: 'meta', textContent: 'Difficulty: ' + st.difficulty + (st.models ? ' \u00b7 Codex ' + st.models.codex.model + ' (' + st.models.codex.effort
      + ' effort) \u00b7 Claude ' + st.models.claude.model + ' (' + st.models.claude.effort + ' effort)' : '')}));
    const results = [];
    if (st.tests) results.push('Tests: ' + st.tests.tests + ' run, ' + (st.tests.failures + st.tests.errors) + ' failed');
    if (st.review) results.push('Claude review: ' + st.review.verdict + ' (' + st.review.findings.length + ' note' + (st.review.findings.length === 1 ? '' : 's') + ')');
    if (results.length) body.append(el('div', {className: 'meta', textContent: results.join(' \u00b7 ')}));
    if (st.reason && st.state === 'failed') body.append(el('div', {className: 'err', textContent: explain(st.reason)}));
    if (st.reason && st.state === 'retrying') body.append(el('div', {className: 'meta', textContent: explain(st.reason)}));
    if (st.developer_summary && (st.state === 'failed' || st.polish)) body.append(el('pre', {className: 'muted', dir: 'auto', textContent: 'Codex said: ' + st.developer_summary}));
    if (st.review && st.review.findings.length && !st.polish) body.append(el('ul', {className: 'notes'},
      st.review.findings.map(f => el('li', {}, el('code', {textContent: f.file}), ' ' + f.explanation))));
    if (st.polish && st.notes) body.append(el('div', {className: 'meta', textContent: 'Codex was shown ' + st.notes.length + ' review note' + (st.notes.length === 1 ? '' : 's') + '.'}));
    steps.append(el('li', {className: 'step-' + (st.state || 'pending')}, el('span', {className: 'num', textContent: String(i + 1)}), body));
  });
  const bar = el('div', {className: 'progress'}, el('span', {style: 'width:' + (rows.length ? Math.round(100 * done / rows.length) : 0) + '%'}));
  bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-label', 'Steps done');
  bar.setAttribute('aria-valuemin', '0'); bar.setAttribute('aria-valuemax', String(rows.length)); bar.setAttribute('aria-valuenow', String(done));
  const howTo = web ? 'Run it yourself: Open folder, type cmd in the address bar, run  node server.mjs  and open http://localhost:3000 (Ctrl+C stops it).'
    : 'Run it yourself: Open folder, type cmd in the address bar, run  python main.py   (tests: python -m unittest).';
  const scope = (rows.length ? 'Plan: ' + rows.length + ' steps (Claude decided). ' : 'Claude decides the number of steps (up to ' + b.max_steps + '). ')
    + 'Each step uses 1 Codex + 1 Claude session, up to ' + s.max_codex_sessions + ' + ' + s.max_claude_sessions + ' if corrections are needed, plus 1 Claude planning session. If Dani Agent source has changed, approval also covers up to 3 independent Claude source-review sessions with no review retries. A step that still does not pass is tried again, as often as it takes, with pauses of up to an hour between tries; those tries use more sessions than this. No time limit: a session runs as long as it is working. Subscriptions only, no API fallback.';
  const text = p.description || '', long = text.length > 420 || text.split('\n').length > 6, whole = !!opened['description-' + p.id];
  return [el('div', {className: 'card'}, el('div', {className: 'row'}, el('h2', {dir: 'auto', textContent: p.name}), chip(stateOf(p)),
      el('span', {className: 'chip', textContent: state.kinds[p.kind || 'web']})),
      text ? el('p', {className: 'desc' + (long && !whole ? ' clamp' : ''), dir: 'auto', textContent: text}) : null,
      long ? el('button', {type: 'button', className: 'link', textContent: whole ? 'Show less' : 'Show the whole description', onclick: () => { opened['description-' + p.id] = !whole; render(); }}) : null,
      (p.attachments || []).length ? el('p', {className: 'meta'}, el('b', {textContent: 'Reference files shared on build approval: '}),
        (p.attachments || []).flatMap((f, i) => [i ? ', ' : null, el('bdi', {textContent: f.name}), ' (' + f.characters.toLocaleString() + ' text characters)'])) : null,
      p.reason && p.state === 'stopped' ? el('p', {className: 'notice' + (cancelled ? ' bad' : ''), textContent: cancelled
        ? 'Cancelled by you. Automatic work on this product is off until you click Build again.'
        : 'Paused: ' + explain(p.reason) + (p.resume_after ? ' Dani Agent tries again by itself at ' + new Date(p.resume_after).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) + '.'
          : ' Dani Agent resumes it by itself.') + ' Finished steps are kept.'}) : null,
      p.polish_reason ? el('p', {className: 'muted', textContent: 'A polish pass did not pass (' + explain(p.polish_reason) + '). The product is unchanged; the notes it was given are set aside.'}) : null,
      p.build_mode === 'polish' && p.state === 'awaiting_approval' ? el('p', {className: 'muted', textContent: 'Polish: Codex reads each open review note, fixes the ones it agrees with and explains the rest; tests and Claude review it like any step.'}) : null,
      // What an approval covers is in full view while it is being asked for, and one click away afterwards.
      p.state === 'awaiting_approval' ? el('div', {className: 'scope'}, el('b', {textContent: 'What you approve'}), scope)
        : fold('scope-' + p.id, 'What the approval covers', el('p', {textContent: scope})),
      actions, err),
    el('div', {className: 'card'}, el('div', {className: 'row', style: 'justify-content:space-between'}, el('h3', {textContent: 'Build plan', style: 'margin:0'}),
        rows.length ? el('span', {className: 'muted', textContent: done + ' of ' + rows.length + ' steps done'}) : null),
      rows.length ? [bar, steps] : el('p', {className: 'muted', textContent: 'Claude writes the plan after you approve.'})),
    el('div', {className: 'card'}, el('h3', {textContent: 'Files'}), el('p', {}, 'Folder: ', el('code', {dir: 'ltr', textContent: p.app})),
      el('p', {className: 'hint', textContent: howTo}))].filter(Boolean);
}
function renderPreview(p) {
  // Its own box, redrawn only when the preview starts or stops: re-inserting an iframe reloads the app.
  const box = document.getElementById('previewBox'), url = (!creating && p && p.preview_url) || '';
  if (box.dataset.url === url) return;
  box.dataset.url = url;
  box.replaceChildren(...(url ? [el('div', {className: 'card'}, el('div', {className: 'row', style: 'margin-bottom:.6rem'},
      el('h3', {textContent: 'Live preview', style: 'margin:0'}), el('a', {href: url, target: '_blank', rel: 'noopener', textContent: 'Open in a new tab'}),
      el('span', {className: 'muted', textContent: 'Runs the app\'s own server.mjs, read-only, for 15 minutes.'})),
    el('iframe', {src: url, sandbox: 'allow-scripts allow-forms allow-same-origin allow-modals'}))] : []));
}
const TOOL = {codex: 'Codex CLI', claude: 'Claude Code', python: 'Python 3.12+', git: 'Git'};
const CHECK = {passed: ['Passed', 'ok'], running: ['Running now', 'warn'], queued: ['Starting soon', 'warn'],
  failed: ['Failed', 'bad'], waiting: ['Waiting', '']};
const FOLDER_ERRORS = {TYPE_A_FOLDER_PATH: 'Type a folder path.', USE_A_FULL_PATH_ON_A_LOCAL_DRIVE_E_G_D_DANIAGENT: 'Use a full path on a drive of this PC, e.g. D:\\DaniAgent.',
  PICK_A_FOLDER_NOT_A_WHOLE_DRIVE: 'Pick a folder, not a whole drive.', MUST_BE_OUTSIDE_YOUR_USER_FOLDER_THE_SANDBOX_CANNOT_ENTER_IT: 'It must be outside your user folder (C:\\Users\\...): Codex\'s sandbox cannot work there.',
  NOT_INSIDE_DANI_AGENT_WINDOWS_OR_PROGRAM_FOLDERS: 'Not inside Windows, Program Files or Dani Agent\'s own folder.', THAT_DRIVE_DOES_NOT_EXIST_ON_THIS_PC: 'That drive does not exist on this PC.',
  MUST_BE_ON_A_LOCAL_DRIVE_NOT_USB_OR_NETWORK: 'It must be on a fixed drive of this PC (not USB or network).', MUST_BE_ON_AN_NTFS_DRIVE: 'That drive is not NTFS, so it cannot be made private.',
  PICK_AN_EMPTY_OR_NEW_FOLDER: 'Pick an empty or new folder.', WAIT_UNTIL_NO_BUILD_OR_CHECK_IS_WAITING_OR_RUNNING: 'Wait until no build or check is waiting or running.'};
async function connectCall(action, body) {
  connErr = '';
  try { conn = action ? await post('/api/connect/' + action, body) : await (await fetch('/api/connect')).json(); }
  catch (x) { connErr = FOLDER_ERRORS[x.message] || human(x.message); }
  render();
}
const ok = (good, yes, no) => el('span', {className: 'chip ' + (good ? 'ok' : 'warn'), textContent: good ? yes : no});
const btn = (label, action, body, primary) => el('button', {className: primary ? 'primary' : '', textContent: label,
  onclick: e => { e.target.disabled = true; connectCall(action, body); }});
function renderConnect() {
  if (!conn) { connectCall(); return [el('p', {className: 'muted', textContent: 'Checking this PC...'})]; }
  const c = conn, cards = [];
  // Each numbered step says whether it is done, needs the user, or runs by itself.
  const head = (title, [label, kind]) => el('div', {className: 'row', style: 'margin-bottom:.6rem'}, el('h3', {textContent: title, style: 'margin:0'}),
    el('span', {className: 'chip ' + kind, textContent: label}));
  const tag = good => good ? ['Done', 'ok'] : ['Needs you', 'warn'];
  const L = c.login || {}, f = c.folder || {}, states = Object.values(c.checks || {});
  const programsOk = Object.keys(TOOL).every(name => c.tools[name] && c.tools[name].ok);
  const signedIn = ['codex', 'claude'].every(name => L[name] && L[name].ok) && !(L.environment_inherited || []).length && !(L.environment_in_windows || []).length;
  cards.push(el('div', {className: 'card'}, el('h2', {textContent: 'Connect your own accounts'}),
    el('p', {className: 'muted', textContent: 'Dani Agent runs the official Claude Code and Codex programs on this PC with the accounts you are signed in to. It never sees or stores passwords or tokens, and uses only your subscriptions (no API keys).'}),
    c.ready ? el('p', {}, ok(true, 'Ready to build', '')) : el('p', {className: 'hint', textContent: 'Steps 1 to 3 need you. Steps 4 and 5 then run by themselves.'}),
    el('div', {className: 'err', textContent: connErr})));
  const programs = el('div', {className: 'card'}, head('1. Programs', tag(programsOk)));
  for (const [name, label] of Object.entries(TOOL)) {
    const t = c.tools[name], rows = (c.found || {})[name] || [];
    programs.append(el('div', {className: 'row', style: 'margin-top:.6rem'}, el('b', {textContent: label}),
      ok(t && t.ok, t ? t.version : '', 'Not set'), el('span', {className: 'files', dir: 'ltr', textContent: t ? t.path : ''})));
    for (const r of rows) programs.append(el('div', {className: 'row muted', style: 'margin-left:1rem'}, r.path + ' ' + (r.version || ''),
      r.problem ? el('span', {className: 'err', textContent: r.problem}) : btn('Use this', 'confirm', {tool: name, path: r.path})));
    if (c.found && !rows.length && !t) programs.append(el('div', {className: 'muted', style: 'margin-left:1rem', textContent: 'Not found. ' + c.install[name]}));
  }
  const custom = el('input', {placeholder: 'Or paste the full path of a program .exe'}), which = el('select', {},
    Object.entries(TOOL).map(([k, v]) => el('option', {value: k, textContent: v})));
  programs.append(el('div', {className: 'row', style: 'margin-top:.8rem'}, btn('Find programs', 'discover', {}, !c.found)),
    el('div', {className: 'row', style: 'margin-top:.5rem'}, which, custom,
      el('button', {textContent: 'Use path', onclick: () => connectCall('confirm', {tool: which.value, path: custom.value})})));
  custom.setAttribute('aria-label', 'Full path of a program'); which.setAttribute('aria-label', 'Program');
  cards.push(programs);
  const signIn = el('div', {className: 'card'}, head('2. Sign in (subscription)', tag(signedIn)));
  for (const [name, label, how] of [['codex', 'Codex', 'Sign in with ChatGPT'], ['claude', 'Claude', 'Sign in with your Claude plan']]) {
    const l = (c.login || {})[name];
    signIn.append(el('div', {className: 'row', style: 'margin-top:.4rem'}, el('b', {textContent: label}),
      ok(l && l.ok, 'Signed in' + (l && l.plan ? ' (' + l.plan + ')' : ''), !l ? 'Confirm the program first' : l.signed_in ? 'Signed in with an API key or console account - not supported' : 'Not signed in'),
      l && !l.ok ? btn(how, 'login', {tool: name}) : null));
  }
  if ((L.environment_inherited || []).length) signIn.append(el('p', {className: 'err', textContent: 'Dani Agent was started by a program that sets '
    + L.environment_inherited.join(', ') + ' (for example a terminal or a developer tool). These are not your account settings, and they would send '
    + 'Claude or Codex somewhere other than your subscription. Close Dani Agent and start it again from File Explorer with Start-Dani-Dashboard.cmd. '
    + '(Check again cannot clear these: they belong to this running copy of Dani Agent.)'}));
  if ((L.environment_in_windows || []).length) signIn.append(el('p', {className: 'err', textContent: L.environment_in_windows.join(', ')
    + ' is set in your Windows settings. Dani Agent only uses your subscriptions, so remove it: Start menu > "Edit environment variables for your account"'
    + ' (or the system ones), delete it, then restart Dani Agent.'}));
  signIn.append(el('div', {className: 'row', style: 'margin-top:.6rem'}, el('span', {className: 'muted', textContent: 'Sign-in opens a window; finish it in your browser, then'}), btn('Check again', 'refresh', {})));
  cards.push(signIn);
  const where = el('input', {placeholder: 'e.g. D:\\DaniAgent', value: f.path || ''});
  where.setAttribute('aria-label', 'Work folder path');
  cards.push(el('div', {className: 'card'}, head('3. Private work folder', tag(f.ready)),
    el('div', {className: 'row'}, el('code', {dir: 'ltr', textContent: f.path || ''}), ok(f.ready, 'Private (only you and Windows)', f.exists ? 'Not private' : 'Not created')),
    !f.exists ? el('div', {style: 'margin-top:.6rem'}, btn('Create private folder', 'lock', {}, true)) : f.private && !f.ready ? btn('Finish folder', 'lock', {}) : null,
    f.exists && !f.private ? el('p', {className: 'err', textContent: 'This folder already exists and other accounts can open it. Dani Agent will not change it: pick another folder below, or rename it and click Check again.'}) : null,
    el('p', {className: 'muted', textContent: 'Your products and work files live here. You can use another folder: it must be on a fixed NTFS drive of this PC and outside your user folder (Codex\'s sandbox cannot work inside C:\\Users). Dani Agent makes it private and moves your products into it.'}),
    el('div', {className: 'row'}, where, el('button', {textContent: 'Use this folder', onclick: e => { e.target.disabled = true; connectCall('folder', {path: where.value}); }}))));
  const chk = (key, label, detail, retry) => { const st = (c.checks || {})[key] || 'waiting', [t, cls] = CHECK[st];
    return el('div', {className: 'row', style: 'margin:.45rem 0'}, el('b', {textContent: label}), el('span', {className: 'chip ' + cls, textContent: t}),
      el('span', {className: 'muted', textContent: detail}), st === 'failed' ? btn('Retry', retry, {}) : null); };
  cards.push(el('div', {className: 'card'}, head('4. Automatic safety checks', states.length > 0 && states.every(v => v === 'passed') ? ['Done', 'ok']
      : states.includes('failed') ? ['A check failed', 'bad'] : states.some(v => v === 'running' || v === 'queued') ? ['Running by itself', 'warn']
      : ['Waits for the steps above', '']),
    el('p', {className: 'muted', textContent: 'These run by themselves, in this order, once the steps above are done. Each runs once per Dani Agent or program version.'}),
    chk('self_test', 'Self-test', 'no AI, about 10 minutes: Dani Agent tests its own safety rules on this PC', 'selftest'),
    chk('claude_startup', 'Claude startup check', '1 tiny Claude session: proves Claude starts with no tools, so it can only read and reply', 'reviewer'),
    chk('codex_sandbox', 'Codex sandbox check', '1 tiny Codex session: proves Codex\'s sandbox works on this PC', 'hostcheck')));
  const models = el('div', {className: 'card'}, el('h3', {textContent: '5. Models on your plans'}),
    el('p', {className: 'muted', textContent: 'Read from Codex and Claude Code themselves; nothing is sent to test them. For every build step, Claude rates how hard it is and Dani Agent picks the model and effort: simple = low effort, normal = medium, hard = high effort with the strongest model. A model your plan refuses is skipped from then on.'}));
  for (const [name, label] of [['codex', 'Codex'], ['claude', 'Claude']]) {
    const m = (c.models || {})[name] || {available: [], refused: []}, every = (c.everyday || {})[name];
    const row = el('div', {className: 'row', style: 'margin:.5rem 0'}, el('b', {textContent: label + ':'}));
    if (!m.available.length) row.append(el('span', {className: 'muted', textContent: 'Confirm the program and sign in first.'}));
    const labelOf = model => (m.labels || {})[model] || model;
    m.available.forEach((model, i) => row.append(el('span', {className: 'chip ' + (model === every || i === 0 ? 'ok' : ''),
      textContent: labelOf(model) + (model === every ? ' (everyday)' : '') + (i === 0 ? ' (hard steps)' : '')})));
    m.refused.forEach(model => row.append(el('span', {className: 'chip', style: 'text-decoration:line-through', textContent: labelOf(model) + ' (not in your plan)'})));
    (m.needs_credits || []).forEach(model => row.append(el('span', {className: 'chip muted', textContent: model + ' (needs paid usage credits - not used)'})));
    models.append(row);
  }
  cards.push(models);
  return cards;
}
const typing = () => { const active = document.activeElement; return !!draft.busy || !!(active && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)); };
function render() {
  if (!state) return; renderList();
  const main = document.getElementById('main'); main.replaceChildren();
  if (state.storage_recovery) { main.append(el('div', {className: 'banner', role: 'status', textContent: 'Recovering the work folder. Builds and changes are paused safely; Dani Agent will retry automatically.'})); renderPreview(null); return; }
  if (state.offline_only) main.append(el('div', {className: 'banner', textContent: 'Model-free validation mode: self-tests and product drafts are available. AI sessions and approvals are disabled in this launch.'}));
  const n = nowRunning(); if (n) main.append(n);
  if (connecting) { main.append(...renderConnect()); renderPreview(null); return; }
  const c = connectBanner(); if (c) main.append(c);
  const b = banner(); if (b) main.append(b);
  const p = state.products.find(x => x.id === selected);
  if (creating || !p) main.append(state.products.length && !creating ? el('p', {className: 'muted', textContent: 'Select a product or create a new one.'}) : renderNew());
  else main.append(...renderProduct(p));
  renderPreview(p);
}
async function refresh() {
  const status = document.getElementById('status');
  try { const r = await fetch('/api/state'); if (r.status === 403) { location.reload(); return; } state = await r.json(); }
  catch { status.textContent = 'Dani Agent is not running'; status.className = 'status down'; return; }
  const g = state.progress || {}, working = !quiet(g);
  status.textContent = working ? (PHASE[g.phase] || KIND[g.task_kind] || human(g.phase)) : 'Idle';
  status.className = 'status' + (working ? ' busy' : '');
  if (!selected && state.products.length && !creating) selected = state.products[0].id;
  if (!typing()) render();
}
// Readiness is asked again while it is not reached: the answer the page got when it opened is not the answer
// after the self-test finishes (2026-10-29: the "finish connecting" banner stayed until the page was reloaded).
let connectAsked = 0;
async function recheckConnect() {
  connectAsked = Date.now();
  try { const r = await fetch('/api/connect'); if (!r.ok) return; conn = await r.json(); } catch { return; }
  if (state) Object.assign(state, {connect_ready: conn.ready, connect_missing: conn.missing, connect_checks: conn.checks});
  if (!typing()) render();   // never while a path is being typed on the Connect page
}
document.getElementById('new').onclick = () => { creating = true; connecting = false; render(); };
document.getElementById('connect').onclick = () => { connecting = true; conn = null; render(); };
setInterval(() => {
  if (!state) return;
  const checking = conn && Object.values(conn.checks || {}).some(v => v === 'queued' || v === 'running');
  if (connecting ? checking : state.connect_ready === false && Date.now() - connectAsked > 15000) return recheckConnect();
}, 5000);
refresh().then(() => { if (state && state.connect_ready !== true) fetch('/api/connect').then(r => r.json()).then(v => { conn = v; connectAsked = Date.now();
  if (!v.ready && needsUser(v)) connecting = true;   // the checks Dani Agent runs by itself do not take over the page
  Object.assign(state, {connect_ready: v.ready, connect_missing: v.missing, connect_checks: v.checks}); render(); }).catch(() => {}); });
setInterval(refresh, 2500);
</script></body></html>"""
