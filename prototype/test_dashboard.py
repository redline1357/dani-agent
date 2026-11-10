"""Browser dashboard: access control, product actions, zip and preview; real local HTTP server, zero model calls."""
import _winapi
import http.client
import io
import socket
import subprocess
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import uuid
import zipfile
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import coordinator as C, dashboard as D, products as PR, milestone_review as R
from dani_agent.state import Store



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class Engine:
    host_context = {'appcontainer': False, 'elevated': False}
    def progress(self):
        return {'phase': 'idle'}
    def cancel(self, task_id=None):
        pass


class DashboardCase(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'data' / ('dashboard_test_' + uuid.uuid4().hex[:8])
        self.root.mkdir()
        for patcher in (patch.object(PR, 'PRODUCTS', self.root / 'products'),
                        patch.object(D, 'integration_ready', return_value=True),
                        patch.object(R, 'verified_current', return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = Store(self.root / 'state.sqlite3')
        self.dash = D.Dashboard(self.store, Engine(), 'secret-token')
        self.dash.programs_ok = True   # this fixture represents confirmed, unchanged programs
        self.addCleanup(self.dash.previews.stop_all)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), None)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = D.handler_class(self.dash, self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = 'http://127.0.0.1:' + str(self.port)

    def request(self, method, path, body=None, *, cookie=True, token=True, host=None, origin=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        headers = {'Host': host or '127.0.0.1:' + str(self.port)}
        if cookie:
            headers['Cookie'] = 'dani=secret-token'
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
            if token:
                headers['X-Dani-Token'] = 'secret-token'
            if origin:
                headers['Origin'] = self.origin if origin is True else origin
        conn.request(method, path, json.dumps(body or {}) if method == 'POST' else None, headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data

    def create(self):
        status, _, data = self.request('POST', '/api/products',
            {'name': 'Mug Shop', 'description': 'A small shop that sells mugs.', 'max_steps': 3})
        self.assertEqual(status, 200, data)
        return json.loads(data)


class DashboardTests(DashboardCase):
    def test_page_requires_the_launcher_token_and_sets_a_strict_cookie(self):
        self.assertEqual(self.request('GET', '/', cookie=False)[0], 403)
        status, headers, _ = self.request('GET', '/?token=secret-token', cookie=False)
        self.assertEqual(status, 303)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.request('GET', '/?token=wrong', cookie=False)[0], 403)
        status, _, page = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'content="secret-token"', page)

    def test_state_reports_which_setup_steps_are_open(self):
        # The page tells "connect your accounts" from "Dani Agent is checking itself" by these (2026-11-10).
        self.dash.connect_cache = {'ready': False, 'missing': ['self_test'], 'checks': {'self_test': 'running'}}
        state = json.loads(self.request('GET', '/api/state')[2])
        self.assertEqual((state['connect_ready'], state['connect_missing'], state['connect_checks']),
                         (False, ['self_test'], {'self_test': 'running'}))

    def test_foreign_host_origin_or_missing_token_is_refused(self):
        self.assertEqual(self.request('GET', '/api/state', host='evil.example')[0], 403)        # DNS rebinding
        self.assertEqual(self.request('POST', '/api/products', {'name': 'x'}, token=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/products', {'name': 'x'}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('POST', '/api/products', {'name': 'x'}, cookie=False)[0], 403)
        self.assertEqual(self.request('GET', '/api/state', cookie=False)[0], 403)

    def test_create_then_approve_queues_exactly_one_bound_build(self):
        product = self.create()
        task = self.store.get(product['build_task'])
        self.assertEqual(task['state'], 'awaiting_approval')
        self.assertEqual(task['payload']['budget']['max_steps'], 3)
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/approve')
        self.assertEqual(status, 200, data)
        self.assertEqual(self.store.get(product['build_task'])['state'], 'queued')
        self.assertEqual(self.request('POST', '/api/products/' + product['id'] + '/approve')[0], 400)   # single use
        state = json.loads(self.request('GET', '/api/state')[2])
        self.assertEqual(state['products'][0]['task_state'], 'queued')

    def outdated_review_approval(self):
        product = self.create()
        with patch.object(R, 'verified_current', return_value=False), \
                patch.object(R, 'prepare', return_value={'run': 'fake', 'candidate_sha': 'x', 'fingerprint': 'y'}):
            status, _, data = self.request('POST', '/api/products/' + product['id'] + '/approve')
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)['state'], 'awaiting_review')
        self.assertEqual(self.store.get(product['build_task'])['state'], 'awaiting_approval')   # not yet
        reviews = [t for t in self.store.tasks() if t['kind'] == 'milestone_review']
        self.assertEqual([t['state'] for t in reviews], ['queued'])                          # one approved review
        return product, reviews[0]

    def test_approving_with_an_outdated_review_runs_it_first_then_builds(self):
        product, review = self.outdated_review_approval()
        with patch.object(R, 'verified_current', return_value=False):
            self.dash.after_review()                                                         # review still pending
        self.assertEqual(PR.load(product['id'])['state'], 'awaiting_review')
        self.dash.after_review()                                                             # review passed
        self.assertEqual(PR.load(product['id'])['state'], 'approved')
        self.assertEqual(self.store.get(product['build_task'])['state'], 'queued')

    def test_source_update_before_review_claim_replaces_stale_review_without_renewing_approval(self):
        product, old_review = self.outdated_review_approval()
        self.assertIsNone(self.store.claim('new-source', ('milestone_review',)))
        self.assertEqual(self.store.get(old_review['id'])['state'], 'awaiting_approval')
        self.dash.programs_ok = True
        with patch.object(D, 'fingerprint', return_value='new-source'), \
                patch.object(R, 'verified_current', return_value=False), \
                patch.object(R, 'prepare', return_value={'run': 'new-run', 'candidate_sha': 'new-sha', 'fingerprint': 'new-source'}):
            self.dash.after_review()
            paused = PR.load(product['id'])
            self.assertEqual((paused['state'], paused['reason']), ('stopped', 'APPROVAL_OUTDATED_BY_DANI_AGENT_UPDATE'))
            self.assertEqual(self.store.get(old_review['id'])['state'], 'cancelled')
            self.assertEqual(self.store.get(product['build_task'])['state'], 'cancelled')
            self.dash.auto_resume()
        resumed = PR.load(product['id'])
        self.assertEqual(resumed['state'], 'awaiting_review')
        self.assertNotEqual(resumed['build_task'], product['build_task'])
        reviews = [task for task in self.store.tasks() if task['kind'] == 'milestone_review' and task['state'] == 'queued']
        self.assertEqual(len(reviews), 1)
        self.assertNotEqual(reviews[0]['id'], old_review['id'])
        with self.store.connection() as db:
            old = db.execute('SELECT fingerprint, consumed FROM approvals WHERE task_id=?', (old_review['id'],)).fetchone()
            self.assertNotEqual(old['fingerprint'], 'new-source')
            self.assertIsNone(old['consumed'])
        claimed = self.store.claim('new-source', ('milestone_review',))
        self.assertEqual(claimed['id'], reviews[0]['id'])

    def test_a_failed_review_stops_the_build_without_running_it(self):
        product, review = self.outdated_review_approval()
        self.store.cancel_pending(review['id'])                                              # e.g. review blocked
        with patch.object(R, 'verified_current', return_value=False):
            self.dash.after_review()
        stopped = PR.load(product['id'])
        self.assertEqual((stopped['state'], stopped['reason']), ('stopped', 'INDEPENDENT_REVIEW_DID_NOT_PASS'))
        self.assertEqual(self.store.get(product['build_task'])['state'], 'cancelled')

    def test_a_build_waiting_for_its_review_can_be_cancelled(self):
        product, _ = self.outdated_review_approval()
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/cancel')
        self.assertEqual((status, json.loads(data)['state']), (200, 'stopped'))
        self.dash.after_review()
        self.assertEqual(self.store.get(product['build_task'])['state'], 'cancelled')       # never started

    def test_a_product_with_a_junction_is_not_previewed(self):
        product = self.create()
        app = Path(PR.load(product['id'])['app'])
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'private.txt').write_text('private')
        _winapi.CreateJunction(str(outside), str(app / 'public' / 'linked'))
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/preview')
        self.assertEqual((status, json.loads(data)['error']), (400, 'PROJECT_LINK_OR_REPARSE_POINT'))

    def test_a_non_ascii_token_gets_an_answer_not_a_dropped_connection(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=30) as conn:
            conn.sendall(b'GET /?token=\xe9\xe9 HTTP/1.1\r\nHost: 127.0.0.1:' + str(self.port).encode()
                         + b'\r\nConnection: close\r\n\r\n')
            self.assertTrue(conn.recv(64).startswith(b'HTTP/1.0 403'))

    def test_approving_a_stopped_product_binds_its_new_build_task(self):
        product = self.create()
        self.store.cancel_pending(product['build_task'])
        stopped = PR.load(product['id'])
        stopped['state'] = 'stopped'
        PR.save(stopped)
        approved = self.dash.approve(product['id'])
        self.assertNotEqual(approved['build_task'], product['build_task'])
        self.assertEqual((approved['state'], self.store.get(approved['build_task'])['state']), ('approved', 'queued'))

    def test_zip_refuses_a_junction_inside_the_product(self):
        product = self.create()
        app = Path(PR.load(product['id'])['app'])
        outside = self.root / 'outside_zip'
        outside.mkdir()
        (outside / 'private.txt').write_text('private')
        _winapi.CreateJunction(str(outside), str(app / 'public' / 'linked'))
        with self.assertRaises(RuntimeError):
            self.dash.zip_bytes(product['id'])

    def test_cancel_waits_for_a_task_being_claimed(self):
        engine = C.Coordinator(self.store)
        task = {'id': 'claimed-task', 'kind': 'offline_tests'}
        claimed, answer = threading.Event(), []
        def claim(fingerprint, allowed=None):
            claimed.set()            # 'running' in the store from here on
            time_to_cancel.join(timeout=.3)
            return dict(task)
        time_to_cancel = threading.Thread(target=lambda: (claimed.wait(5), answer.append(engine.cancel('claimed-task'))))
        time_to_cancel.start()
        with patch.object(engine.store, 'claim', claim), patch.object(engine, '_execute', lambda task: None):
            engine.host_context = {'appcontainer': False, 'elevated': False}
            engine.tick()
        time_to_cancel.join(5)
        self.assertEqual(answer, [True])
        self.assertTrue(engine.cancel_event.is_set())

    def test_polish_button_requests_a_polish_bound_to_its_approval(self):
        product = self.create()
        record = PR.load(product['id'])
        record.update(state='ready', steps=[{'title': 'x', 'files': ['public/app.js'], 'state': 'done',
            'review': {'verdict': 'pass', 'findings': [{'severity': 'low', 'file': 'public/app.js', 'line': None, 'explanation': 'n'}]}}])
        PR.save(record)
        self.store.cancel_pending(record['build_task'])
        self.assertEqual(json.loads(self.request('GET', '/api/state')[2])['products'][0]['open_notes'], 1)
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/polish')
        self.assertEqual((status, json.loads(data)['state'], json.loads(data)['build_mode']), (200, 'awaiting_approval', 'polish'))
        self.assertEqual(self.store.get(json.loads(data)['build_task'])['payload']['mode'], 'polish')

    def ready_with_notes(self):
        product = self.create()
        record = PR.load(product['id'])
        record.update(state='ready', steps=[{'title': 'x', 'files': ['public/app.js'], 'state': 'done',
            'review': {'verdict': 'pass', 'findings': [{'severity': 'low', 'file': 'public/app.js', 'line': None, 'explanation': 'n'}]}}])
        PR.save(record)
        self.store.cancel_pending(record['build_task'])
        self.request('POST', '/api/products/' + product['id'] + '/polish')
        return product

    def ready_after_a_polish(self, notes=1):
        product = self.create()
        record = PR.load(product['id'])
        findings = [{'severity': 'low', 'file': 'public/app.js', 'line': None, 'explanation': 'note ' + str(i)} for i in range(notes)]
        record.update(state='ready', steps=[{'title': 'x', 'files': ['public/app.js'], 'state': 'done',
                                             'review': {'verdict': 'pass', 'findings': findings}},
                                            {'title': PR.POLISH_TITLE, 'files': ['public/app.js'], 'state': 'done', 'polish': True,
                                             'handled_notes': []}])
        PR.save(record)
        self.store.cancel_pending(record['build_task'])
        return product

    def sources(self, task_id):
        found = [e['payload'] for e in self.store.history(task_id) if e['kind'] == 'approval_source']
        return [(json.loads(f) if isinstance(f, str) else f)['source'] for f in found]

    def test_leftover_notes_are_polished_automatically_once(self):
        product = self.ready_after_a_polish()
        self.dash.auto_polish()
        after = PR.load(product['id'])
        task = self.store.get(after['build_task'])
        self.assertEqual((after['state'], after['build_mode'], task['state'], task['payload']['mode']), ('approved', 'polish', 'queued', 'polish'))
        self.assertEqual(self.sources(task['id']), ['AUTOMATIC_POLISH_ENABLED_BY_USER'])
        self.assertNotIn('deferred_approval_source', after)                 # no label left on the product
        # The polish ran (build() keeps the notes as tried) and failed: the same notes are not retried by itself.
        ran = PR.load(product['id'])
        ran.pop('auto_polish_pending')
        ran.update(state='ready', polish_reason='PRODUCT_STEP_3_CORRECTION_LIMIT_REACHED')
        PR.save(ran)
        self.store.cancel_pending(task['id'])
        self.dash.auto_polish()
        self.assertEqual(PR.load(product['id'])['build_task'], task['id'])
        self.request('POST', '/api/products/' + product['id'] + '/polish')   # the button still works...
        self.request('POST', '/api/products/' + product['id'] + '/approve')
        self.assertEqual(self.sources(PR.load(product['id'])['build_task']), ['DASHBOARD_HUMAN_APPROVAL'])   # ...and is human

    def test_interrupted_polish_waits_for_its_resume_time_and_confirmed_programs(self):
        product = self.ready_after_a_polish()
        record = PR.load(product['id'])
        record['resume_after'] = '2999-01-01T00:00:00+00:00'
        PR.save(record)
        with patch.object(self.dash, 'polish') as polish:
            self.dash.auto_polish()
            polish.assert_not_called()
        record['resume_after'] = '2000-01-01T00:00:00+00:00'
        PR.save(record)
        self.dash.programs_ok = False
        with patch.object(self.dash, 'polish') as polish:
            self.dash.auto_polish()
            polish.assert_not_called()
        self.dash.programs_ok = True
        self.dash.auto_polish()
        after = PR.load(product['id'])
        self.assertEqual((after['state'], after['build_mode']), ('approved', 'polish'))
        self.assertEqual(self.store.get(after['build_task'])['state'], 'queued')

    def test_an_automatic_attempt_that_cannot_start_is_undone_and_a_later_click_is_human(self):
        product = self.ready_after_a_polish()
        with patch.object(D, 'integration_ready', side_effect=[True, False]):   # ready when checked, gone at approve
            self.dash.auto_polish()
        after = PR.load(product['id'])
        self.assertEqual((after['state'], after.get('deferred_approval_source')), ('ready', None))
        self.assertEqual(self.store.get(after['build_task'])['state'], 'cancelled')   # nothing left waiting
        self.assertEqual(PR.open_notes(after) and len(after.get('auto_polish_tried') or []), 0)   # given back: never ran
        self.request('POST', '/api/products/' + product['id'] + '/polish')
        self.request('POST', '/api/products/' + product['id'] + '/approve')
        self.assertEqual(self.sources(PR.load(product['id'])['build_task']), ['DASHBOARD_HUMAN_APPROVAL'])

    def test_an_automatic_polish_waits_for_the_review_and_keeps_its_source(self):
        product = self.ready_after_a_polish()
        with patch.object(R, 'verified_current', return_value=False), \
                patch.object(R, 'prepare', return_value={'run': 'fake', 'candidate_sha': 'x', 'fingerprint': 'y'}):
            self.dash.auto_polish()
        waiting = PR.load(product['id'])
        self.assertEqual((waiting['state'], waiting['deferred_approval_source']), ('awaiting_review', 'AUTOMATIC_POLISH_ENABLED_BY_USER'))
        self.dash.after_review()                                           # the review passed
        after = PR.load(product['id'])
        self.assertEqual(after['state'], 'approved')
        self.assertNotIn('deferred_approval_source', after)
        self.assertEqual(self.sources(after['build_task']), ['AUTOMATIC_POLISH_ENABLED_BY_USER'])

    def test_a_failed_review_gives_the_notes_back_without_another_review_for_that_version(self):
        product = self.ready_after_a_polish()
        with patch.object(R, 'verified_current', return_value=False), \
                patch.object(R, 'prepare', return_value={'run': 'fake', 'candidate_sha': 'x', 'fingerprint': 'y'}):
            self.dash.auto_polish()
            review = next(t for t in self.store.tasks() if t['kind'] == 'milestone_review')
            self.store.cancel_pending(review['id'])                       # the review did not pass
            self.dash.after_review()
            after = PR.load(product['id'])
            self.assertEqual((after['state'], after.get('auto_polish_tried'), after.get('deferred_approval_source')), ('ready', [], None))
            reviews = sum(t['kind'] == 'milestone_review' for t in self.store.tasks())
            self.dash.auto_polish()                                        # same Dani Agent version: no second review
            self.assertEqual(sum(t['kind'] == 'milestone_review' for t in self.store.tasks()), reviews)
            self.assertEqual(PR.load(product['id'])['state'], 'ready')

    def test_products_built_before_polish_existed_are_not_polished_by_themselves(self):
        product = self.ready_with_notes()        # notes, but no polish step ever ran
        self.request('POST', '/api/products/' + product['id'] + '/cancel')
        before = [t['id'] for t in self.store.tasks()]
        self.dash.auto_polish()
        self.assertEqual([t['id'] for t in self.store.tasks()], before)

    def stopped_product(self, reason):
        product = self.create()
        self.store.cancel_pending(product['build_task'])
        stopped = PR.load(product['id'])
        stopped.update(state='stopped', reason=reason)
        PR.save(stopped)
        return stopped

    def test_a_stopped_product_resumes_by_itself_whatever_the_reason(self):
        # 2026-10-24, the owner: "product never stop with any reason". Only the user's own Cancel stays stopped.
        reasons = ('PRODUCT_STEP_2_TIMEOUT', 'PRODUCT_STEP_1_PROJECT_DEVELOPER_BLOCKED', 'PRODUCT_STEP_3_INTERNAL_ERROR_KeyError',
                   'BUILD_CANCELLED',   # under this version: a dashboard shutdown, not the user
                   'PRODUCT_STEP_2_SOURCE_CHANGED_DURING_APPROVED_WORKFLOW')
        for reason in reasons:
            with self.subTest(reason=reason):
                product = self.stopped_product(reason)
                self.dash.programs_ok = True
                self.dash.auto_resume()
                after = PR.load(product['id'])
                self.assertEqual((after['state'], after['resume_plan'], after['auto_resumes']), ('approved', True, 1))
                self.assertEqual(self.sources(after['build_task']), ['AUTOMATIC_RESUME_NEVER_STOP'])
                self.store.cancel_pending(after['build_task'])        # it stopped again, at once
                again = PR.load(product['id'])
                again.update(state='stopped', reason=reason)
                PR.save(again)
                tasks = len(self.store.tasks())
                self.dash.auto_resume()                               # a pause first: no loop that burns the quota
                self.assertEqual(len(self.store.tasks()), tasks)
                again = PR.load(product['id'])
                again['auto_resumed_at'] = '2000-01-01T00:00:00+00:00'  # the pause is over
                PR.save(again)
                self.dash.auto_resume()
                self.assertEqual(PR.load(product['id'])['auto_resumes'], 2)
                self.store.cancel_pending(PR.load(product['id'])['build_task'])
                done = PR.load(product['id'])
                done.update(state='ready')                            # out of the way of the next reason
                PR.save(done)
        self.assertEqual(D.RESUME_WAITS[-1], 3600)

    def test_a_product_the_user_cancelled_is_never_resumed(self):
        product = self.create()
        self.request('POST', '/api/products/' + product['id'] + '/cancel')
        cancelled = PR.load(product['id'])
        self.assertEqual(cancelled['state'], 'stopped')
        record = dict(cancelled)
        record.pop('cancelled_by_user')                           # e.g. a worker's last save overwrote the file
        PR.save(record)
        self.dash.programs_ok = True
        tasks = len(self.store.tasks())
        self.dash.auto_resume()
        self.assertEqual((PR.load(product['id'])['state'], len(self.store.tasks())), ('stopped', tasks))
        self.request('POST', '/api/products/' + product['id'] + '/rebuild')   # the user builds it again: fine
        self.assertNotIn('cancelled_by_user', PR.load(product['id']))

    def test_cancels_from_earlier_versions_stay_cancelled_and_the_page_says_so(self):
        # Review 8cb1801c: products cancelled before the marker existed must not be resumed after an upgrade.
        old = [self.stopped_product(reason) for reason in ('CANCELLED_BEFORE_START', 'BUILD_CANCELLED')]
        other = self.stopped_product('PRODUCT_STEP_1_PROJECT_DEVELOPER_BLOCKED')
        self.assertEqual(PR.mark_old_cancels(), 2)
        self.assertEqual(PR.mark_old_cancels(), 0)                     # once only: later shutdowns are not cancels
        self.dash.programs_ok = True
        self.dash.auto_resume()
        self.assertEqual([PR.load(p['id'])['state'] for p in old], ['stopped', 'stopped'])
        self.assertEqual(PR.load(other['id'])['state'], 'approved')
        record = PR.load(old[0]['id'])
        record.pop('cancelled_by_user')
        PR.save(record)
        self.store.record(record['build_task'], 'cancelled_by_user', {'product_id': record['id']})
        shown = {p['id']: p for p in self.dash.state()['products']}
        self.assertTrue(shown[record['id']]['cancelled_by_user'])     # from the task history

    def test_f1_a_cancel_also_turns_off_automatic_polish(self):
        # CODEX_REVIEW_3 F1: auto_polish() approved another polish of a product its user had cancelled.
        product = self.ready_after_a_polish()
        self.dash.cancel(product['id'])
        before = len(self.store.tasks())
        self.dash.auto_polish()
        self.assertEqual(len(self.store.tasks()), before)

    def test_f5_a_product_waiting_for_its_approval_does_not_hold_up_another(self):
        waiting = self.create()                                       # awaiting the user's approval: runs nothing
        self.assertEqual(PR.load(waiting['id'])['state'], 'awaiting_approval')
        paused = self.stopped_product('PRODUCT_STEP_1_PROJECT_DEVELOPER_BLOCKED')
        self.dash.programs_ok = True
        self.dash.auto_resume()
        self.assertEqual(PR.load(paused['id'])['state'], 'approved')
        self.assertEqual(PR.load(waiting['id'])['state'], 'awaiting_approval')

    def test_f4_a_dead_worker_is_noticed_and_resumed_with_no_browser_open(self):
        product = self.create()
        record = PR.load(product['id'])
        record['state'] = 'building'                                  # its worker died; nobody opens the page
        PR.save(record)
        self.store.cancel_pending(product['build_task'])
        self.dash.programs_ok = True
        with patch.object(self.dash, 'follow_updates'), patch.object(self.dash, 'auto_setup'):
            self.dash.scheduled_round()                               # reconcile, then resume, in one round
        after = PR.load(product['id'])
        self.assertEqual((after['state'], after['resume_plan']), ('approved', True))

    def test_a_paused_build_resumes_at_the_time_it_recorded(self):
        product = self.stopped_product('PRODUCT_STEP_2_PROJECT_TESTS_FAILED_CORRECTION_LIMIT')
        record = PR.load(product['id'])
        record['resume_after'] = '2999-01-01T00:00:00+00:00'
        PR.save(record)
        self.dash.programs_ok = True
        self.dash.auto_resume()
        self.assertEqual(PR.load(product['id'])['state'], 'stopped')  # not yet
        record = PR.load(product['id'])
        record['resume_after'] = '2000-01-01T00:00:00+00:00'
        PR.save(record)
        self.dash.auto_resume()
        after = PR.load(product['id'])
        self.assertEqual(after['state'], 'approved')
        self.assertNotIn('auto_resumes', after)                       # its own pause, not a resume pause

    def test_one_failing_scheduled_action_no_longer_skips_the_others(self):
        calls = []
        with patch.object(self.dash, 'after_review', side_effect=RuntimeError('SYNTHETIC_SCHEDULER_FAILURE')), \
                patch.object(self.dash, 'follow_updates'), patch.object(self.dash, 'auto_setup'), \
                patch.object(self.dash, 'auto_polish'), patch.object(self.dash, 'auto_resume', side_effect=lambda: calls.append(1)):
            self.dash.scheduled_round()
        self.assertEqual(calls, [1])
        log = (Path(self.store.path).parent / 'scheduler_errors.log').read_text(encoding='utf-8')
        self.assertIn('SYNTHETIC_SCHEDULER_FAILURE', log)
        self.assertIn('"action": "after_review"', log)

    def test_the_approval_says_failed_steps_are_tried_until_they_pass(self):
        self.assertEqual(PR.limits(PR.budget(3))['retries'], 'UNTIL_PASSED_WITH_GROWING_PAUSES')

    def test_a_resume_waits_while_a_program_update_is_confirmed(self):
        product = self.stopped_product('PRODUCT_STEP_3_PROGRAM_UPDATED_DURING_BUILD')
        self.dash.programs_ok = False
        self.dash.auto_resume()
        self.assertEqual(PR.load(product['id'])['state'], 'stopped')
        self.dash.programs_ok = True
        self.dash.auto_resume()
        self.assertEqual(PR.load(product['id'])['state'], 'approved')

    def test_a_product_stopped_by_a_failed_review_of_dani_agent_resumes_once_per_version(self):
        # A failed review of Dani Agent's own code is retried with the next Dani Agent version, not on a timer.
        product = self.stopped_product('INDEPENDENT_REVIEW_DID_NOT_PASS')
        self.dash.programs_ok = True
        with patch('dani_agent.dashboard.fingerprint', return_value='version-1'):
            self.dash.auto_resume()
            after = PR.load(product['id'])
            self.assertEqual((after['state'], after['auto_resumed_version']), ('approved', 'version-1'))
            self.store.cancel_pending(after['build_task'])        # the review of version-1 failed too
            again = PR.load(product['id'])
            again.update(state='stopped', reason='INDEPENDENT_REVIEW_DID_NOT_PASS')
            PR.save(again)
            self.dash.auto_tried.clear()
            tasks = len(self.store.tasks())
            self.dash.auto_resume()                               # same version: not again
            self.assertEqual(len(self.store.tasks()), tasks)
        with patch('dani_agent.dashboard.fingerprint', return_value='version-2'):
            self.dash.auto_resume()                               # a new version: tried again
            self.assertEqual(PR.load(product['id'])['state'], 'approved')

    def test_a_build_interrupted_by_a_program_update_is_labelled(self):
        with patch('dani_agent.connect.programs_changed', return_value=True):
            self.assertEqual(PR.program_stop('PRODUCT_STEP_3_PROJECT_DEVELOPER_BLOCKED_COMMAND_TOOL_UNAVAILABLE'),
                             'PRODUCT_STEP_3_PROGRAM_UPDATED_DURING_BUILD')
            self.assertEqual(PR.program_stop('PRODUCT_STEP_1_QUOTA_EXHAUSTED'), 'PRODUCT_STEP_1_QUOTA_EXHAUSTED')
        with patch('dani_agent.connect.programs_changed', return_value=False):
            self.assertEqual(PR.program_stop('PRODUCT_STEP_2_TESTS_FAILED'), 'PRODUCT_STEP_2_TESTS_FAILED')

    def test_cancelling_a_polish_keeps_the_finished_product_ready(self):
        product = self.ready_with_notes()
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/cancel')
        after = json.loads(data)
        self.assertEqual((status, after['state'], after['reason'], after['polish_reason']), (200, 'ready', None, 'CANCELLED_BEFORE_START'))

    def test_a_failed_review_before_a_polish_keeps_the_product_ready(self):
        product = self.ready_with_notes()
        with patch.object(R, 'verified_current', return_value=False), \
                patch.object(R, 'prepare', return_value={'run': 'fake', 'candidate_sha': 'x', 'fingerprint': 'y'}):
            self.request('POST', '/api/products/' + product['id'] + '/approve')
        self.assertEqual(PR.load(product['id'])['state'], 'awaiting_review')
        review = next(t for t in self.store.tasks() if t['kind'] == 'milestone_review')
        self.store.cancel_pending(review['id'])            # the review did not pass
        with patch.object(R, 'verified_current', return_value=False):
            self.dash.after_review()
        after = PR.load(product['id'])
        self.assertEqual((after['state'], after['polish_reason']), ('ready', 'INDEPENDENT_REVIEW_DID_NOT_PASS'))

    def test_cancel_only_stops_the_selected_task(self):
        engine = C.Coordinator(self.store)
        engine.current = {'id': 'next-task'}               # the selected build already finished
        self.assertFalse(engine.cancel('selected-task'))
        self.assertFalse(engine.cancel_event.is_set())
        self.assertTrue(engine.cancel('next-task'))
        self.assertTrue(engine.cancel_event.is_set())

    def test_invalid_product_input_is_reported(self):
        status, _, data = self.request('POST', '/api/products', {'name': '', 'description': 'too short'})
        self.assertEqual(status, 400)
        self.assertIn('PRODUCT_NAME', json.loads(data)['error'])

    def test_zip_contains_the_app_without_git_metadata(self):
        product = self.create()
        status, headers, data = self.request('GET', '/api/products/' + product['id'] + '/zip')
        self.assertEqual(status, 200)
        self.assertIn('mug-shop.zip', headers['Content-Disposition'])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn('mug-shop/public/index.html', names)
        self.assertFalse(any('/.git/' in n for n in names))

    def preview(self, product):
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/preview')
        self.assertEqual(status, 200, data)
        url = json.loads(data)['url']
        self.assertRegex(url, r'^http://127\.0\.0\.2:\d+/$')   # not the dashboard's cookie host
        return url

    def fetch(self, url, method='GET', body=None):
        conn = http.client.HTTPConnection(url.split('/')[2], timeout=30)
        conn.request(method, '/' + url.split('/', 3)[3], body)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_live_preview_runs_the_products_own_server(self):
        product = self.create()
        url = self.preview(product)
        self.assertIn(b'Mug Shop', self.fetch(url)[1])                              # the page
        self.assertEqual(self.fetch(url + 'app.js')[0], 200)                          # its scripts
        self.assertEqual(self.fetch(url + 'api/health'), (200, b'{"ok":true}'))     # and its API routes
        state = json.loads(self.request('GET', '/api/state')[2])
        self.assertEqual(state['products'][0]['preview_url'], url)
        self.assertEqual(self.request('POST', '/api/products/' + product['id'] + '/stop_preview')[0], 200)
        self.assertIsNone(json.loads(self.request('GET', '/api/state')[2])['products'][0]['preview_url'])
        self.assertIn("frame-src 'self' http://127.0.0.2:*", self.request('GET', '/')[1]['Content-Security-Policy'])

    def test_previewed_code_cannot_write_files_or_start_programs(self):
        product = self.create()
        app = Path(PR.load(product['id'])['app'])
        (app / 'server.mjs').write_text(
            "import { writeFileSync } from 'node:fs';\n"
            "import { execSync } from 'node:child_process';\n"
            "export function handle(req, res) {\n"
            "  const out = [];\n"
            "  try { writeFileSync(new URL('./written.txt', import.meta.url), 'x'); out.push('wrote'); } catch (e) { out.push(e.code); }\n"
            "  try { execSync('whoami'); out.push('ran'); } catch (e) { out.push(e.code); }\n"
            "  res.end(out.join(' '));\n"
            "}\n", encoding='utf-8')
        url = self.preview(product)
        self.assertEqual(self.fetch(url)[1], b'ERR_ACCESS_DENIED ERR_ACCESS_DENIED')
        self.assertFalse((app / 'written.txt').exists())

    def test_previewed_code_never_sees_the_dashboard_cookie(self):
        product = self.create()
        app = Path(PR.load(product['id'])['app'])
        (app / 'server.mjs').write_text(
            "export function handle(req, res) { res.end(JSON.stringify([req.headers.cookie || null, req.headers.authorization || null])); }\n",
            encoding='utf-8')
        url = self.preview(product)
        conn = http.client.HTTPConnection(url.split('/')[2], timeout=30)
        conn.request('GET', '/', headers={'Cookie': 'dani=secret-token', 'Authorization': 'Bearer secret-token'})
        self.assertEqual(json.loads(conn.getresponse().read()), [None, None])
        conn.close()
        self.assertEqual(self.request('GET', '/api/state', host=url.split('/')[2])[0], 403)   # not a dashboard host

    def test_preview_is_refused_while_building_and_stopped_by_a_new_build(self):
        product = self.create()
        url = self.preview(product)
        self.assertEqual(self.request('POST', '/api/products/' + product['id'] + '/approve')[0], 200)
        self.assertIsNone(self.dash.previews.url(product['id']))
        with self.assertRaises(OSError):
            self.fetch(url)
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/preview')
        self.assertEqual((status, json.loads(data)['error']), (400, 'PREVIEW_AFTER_THE_BUILD_FINISHES'))


class DashboardReviewFixTests(DashboardCase):
    """Independent review 2026-10-21 findings on the first dashboard/product builder."""

    def test_malformed_requests_get_a_clean_error(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.putrequest('POST', '/api/products', skip_host=True)
        for name, value in (('Host', '127.0.0.1:' + str(self.port)), ('Cookie', 'dani=secret-token'),
                            ('X-Dani-Token', 'secret-token'), ('Origin', self.origin), ('Content-Length', 'abc')):
            conn.putheader(name, value)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()
        status, _, data = self.request('POST', '/api/products', {'name': ['x'], 'description': 'A small shop that sells mugs.'})
        self.assertEqual((status, json.loads(data)['error']), (400, 'INVALID_PRODUCT_FIELDS'))

    def test_abandoned_build_is_shown_stopped_and_can_be_built_again(self):
        product = self.create()
        self.store.cancel_pending(product['build_task'])          # e.g. killed or cancelled mid-build
        stuck = PR.load(product['id'])
        stuck['state'] = 'building'
        PR.save(stuck)
        state = json.loads(self.request('GET', '/api/state')[2])
        self.assertEqual(state['products'][0]['state'], 'stopped')
        status, _, data = self.request('POST', '/api/products/' + product['id'] + '/rebuild')
        self.assertEqual(status, 200, data)
        rebuilt = json.loads(data)
        self.assertEqual(rebuilt['state'], 'awaiting_approval')
        self.assertNotEqual(rebuilt['build_task'], product['build_task'])
        self.assertEqual(self.store.get(rebuilt['build_task'])['state'], 'awaiting_approval')

    def test_open_folder_ignores_a_tampered_record(self):
        product = self.create()
        record = PR.load(product['id'])
        record['app'] = r'C:\Windows\notepad.exe'
        PR.save(record)
        with patch.object(D.os, 'startfile') as startfile:
            self.assertEqual(self.request('POST', '/api/products/' + product['id'] + '/open')[0], 200)
        startfile.assert_called_once_with(str(PR.PRODUCTS / 'mug-shop'))

    def test_step_count_is_not_asked_claude_plans_it_within_the_ceiling(self):
        status, _, data = self.request('POST', '/api/products', {'name': 'Hi', 'description': 'Say hi to a typed name.'})
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)['budget']['max_steps'], PR.MAX_STEPS)
        page = self.request('GET', '/')[2]
        self.assertNotIn(b'Maximum build steps', page)

    def test_python_product_type_is_offered_and_created(self):
        state = json.loads(self.request('GET', '/api/state')[2])
        self.assertEqual(set(state['kinds']), {'web', 'python'})
        status, _, data = self.request('POST', '/api/products',
            {'name': 'Budget Tool', 'description': 'Tracks monthly spending.', 'max_steps': 3, 'kind': 'python'})
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)['kind'], 'python')
        status, _, data = self.request('POST', '/api/products',
            {'name': 'X', 'description': 'Tracks monthly spending.', 'kind': 'desktop'})
        self.assertEqual((status, json.loads(data)['error']), (400, 'UNKNOWN_PRODUCT_TYPE'))

    def test_zip_size_is_capped(self):
        product = self.create()
        with patch.object(D, 'MAX_ZIP_FILES', 1):
            status, _, data = self.request('GET', '/api/products/' + product['id'] + '/zip')
        self.assertEqual((status, json.loads(data)['error']), (400, 'PRODUCT_TOO_LARGE_FOR_ZIP_USE_OPEN_FOLDER'))

    def test_integration_refuses_a_planted_junction(self):
        product = self.create()
        app = PR.PRODUCTS / 'mug-shop'
        outside = self.root / 'outside'
        outside.mkdir()
        import subprocess
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(app / 'public' / 'lib'), str(outside)], check=True,
                       capture_output=True)
        with self.assertRaisesRegex(RuntimeError, 'PROJECT_LINK_OR_REPARSE_POINT'):
            PR.safe_target(app, 'public/lib/cart.mjs')
        self.assertEqual(list(outside.iterdir()), [])


SHIM = """
const intervals = [];
// Model the DOM operations used by PAGE, rather than returning a new unrelated object for every lookup.
// This remains a synthetic JavaScript test, not a replacement for the real browser checks.
class TestNode {
  constructor(tag, text = '') {
    this.tagName = tag === '#text' ? undefined : tag.toUpperCase();
    this.nodeType = tag === '#text' ? 3 : 1;
    this.childNodes = []; this.parentNode = null; this._text = text;
    this.style = {}; this.dataset = {}; this.className = ''; this.attributes = new Map();
  }
  get children() { return this.childNodes.filter(child => child.nodeType === 1); }
  get textContent() { return this.nodeType === 3 ? this._text : this.childNodes.map(child => child.textContent).join(''); }
  set textContent(value) {
    if (this.nodeType === 3) this._text = String(value);
    else this.replaceChildren(...(String(value) ? [String(value)] : []));
  }
  contains(other) { return other instanceof TestNode && (this === other || this.childNodes.some(child => child.contains(other))); }
  append(...values) {
    for (const value of values) {
      const child = value instanceof TestNode ? value : new TestNode('#text', String(value));
      if (child.contains(this)) throw new Error('DOM ancestor cycle');
      if (child.parentNode) child.parentNode.childNodes = child.parentNode.childNodes.filter(item => item !== child);
      child.parentNode = this; this.childNodes.push(child);
    }
  }
  replaceChildren(...values) {
    if (globalThis.document && this.childNodes.some(child => child.contains(document.activeElement))) document.activeElement = document.body;
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = []; this.append(...values);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); if (name === 'id') this.id = String(value); }
  getAttribute(name) { return name === 'id' ? this.id || null : this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); if (name === 'id') this.id = ''; }
  addEventListener() {}
  focus() { if (!this.disabled && document.body.contains(this)) document.activeElement = this; }
}
const node = tag => new TestNode(tag);
const descendants = root => [root, ...root.childNodes.flatMap(descendants)];
const body = node('body');
for (const id of ['new', 'connect', 'list', 'main', 'previewBox', 'status']) {
  const item = node(['new', 'connect'].includes(id) ? 'button' : 'div'); item.id = id; body.append(item);
}
globalThis.window = globalThis;
globalThis.document = {body, createElement: node, querySelector: () => ({content: 'token'}),
  getElementById: id => descendants(body).find(item => item.id === id) || null, activeElement: body};
const stored = new Map();
globalThis.sessionStorage = {getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, String(value))};
globalThis.location = {reload() {}};
globalThis.confirm = () => true;
globalThis.alert = () => {};
globalThis.setInterval = f => { intervals.push(f); return intervals.length; };
globalThis.fetch = async path => ({status: 200, ok: true, json: async () => String(path).includes('connect') ? DATA.conn : DATA.state});
"""
SCENARIO = """
(async () => {
  await new Promise(r => setImmediate(r));          // let the page's first refresh() finish
  conn = DATA.conn; connecting = true;
  const cards = renderConnect();
  if (cards.length < 5) throw new Error('Connect page incomplete: ' + cards.length + ' cards');
  state = DATA.state; selected = DATA.state.products[0].id; connecting = false; creating = false;
  render();
  if (!renderProduct(DATA.state.products[0]).length) throw new Error('product page empty');
  if (!renderProduct(DATA.state.products[1]).length) throw new Error('polished product page empty');
  if (!nowRunning()) throw new Error('no now-running bar while a step runs');
  connecting = true; conn = DATA.conn;
  for (const f of intervals) await f();             // the page's own polling must not throw
  console.log('PAGE_OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""

FOCUS_SCENARIO = """
(async () => {
  await new Promise(r => setImmediate(r));
  const list = document.getElementById('list'), original = document.getElementById('product-' + DATA.state.products[0].id);
  if (!original || !list.contains(original) || !list.contains(original.children[0])) throw new Error('Descendant identity is missing');
  if (!list.contains(list) || list.contains(null) || list.contains(document.getElementById('new'))) throw new Error('contains() semantics are wrong');
  if (document.getElementById(list.id) !== list || document.getElementById('missing') !== null) throw new Error('ID lookup is not faithful');
  original.focus();
  if (document.activeElement !== original) throw new Error('Focus was not recorded');
  await refresh();
  const replacement = document.getElementById(original.id);
  if (replacement === original || list.contains(original) || original.parentNode !== null) throw new Error('Replacement did not detach the old control');
  if (document.activeElement !== replacement) throw new Error('Polling lost product-button focus');
  original.focus();
  if (document.activeElement !== replacement) throw new Error('Detached control stole focus');
  document.getElementById('new').onclick();
  const name = document.getElementById('product-name'); name.value = 'Unfinished description'; name.oninput({target: name}); name.focus();
  await refresh();
  if (document.getElementById('product-name') !== name || document.activeElement !== name || name.value !== draft.name) throw new Error('Polling replaced the active input');
  console.log('FOCUS_OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""

ATTACHMENT_SCENARIO = """
(async () => {
  await new Promise(r => setImmediate(r));
  const doc = {id: 'a'.repeat(32), name: 'requirements.docx', size: 100, characters: 50};
  const csv = {id: 'b'.repeat(32), name: 'data.csv', size: 20, characters: 10};
  const unused = {id: 'c'.repeat(32), name: 'unused.txt', size: 20, characters: 10};
  const literal = 'Synthetic requirements: <script>never executable</script>';
  const requests = [];
  globalThis.fetch = async (path, options = {}) => {
    requests.push({path, options});
    let value, status = 200;
    if (path === '/api/state') value = DATA.state;
    else if (path === '/api/attachments/' + doc.id) value = {...doc, text: literal};
    else if (path === '/api/attachments') value = {files: [csv, unused]};
    else if (path === '/api/attachments/' + doc.id + '/remove') { value = {error: 'ATTACHMENT_ALREADY_BOUND'}; status = 400; }
    else if ([csv, unused].some(file => path === '/api/attachments/' + file.id + '/remove')) value = {removed: true};
    else throw new Error('Unexpected endpoint: ' + path);
    return {status, ok: status === 200, json: async () => value};
  };
  const button = text => {
    const matches = descendants(document.getElementById('main')).filter(item => item.tagName === 'BUTTON' && item.textContent === text);
    if (matches.length !== 1) throw new Error('Expected one button: ' + text + ', got ' + matches.length);
    return matches[0];
  };
  const click = async text => { const item = button(text); if (item.disabled) throw new Error('Button is disabled: ' + text); await item.onclick({target: item}); };
  const preview = () => descendants(document.getElementById('draft-attachments')).find(item => item.tagName === 'PRE');
  draft = {...emptyDraft(), name: 'Document-only product', attachments: [doc]}; creating = true; connecting = false; render();
  const picker = document.getElementById('product-files');
  if (!picker.multiple || !picker.accept.includes('.docx') || !picker.accept.includes('.pdf')) throw new Error('File picker is missing');
  if (document.getElementById('product-description').value !== '') throw new Error('Document-only form unexpectedly needs typed text');
  button('Manage unused uploads'); button('Remove ' + doc.name); button('Create draft');
  await click('View extracted text');
  if (preview().hidden || preview().textContent !== literal || preview().children.length) throw new Error('Preview is missing or parsed as HTML');
  await refresh();
  if (preview().hidden || preview().textContent !== literal) throw new Error('Polling discarded the extracted preview');
  await click('Manage unused uploads'); button('Use ' + csv.name); button('Delete unused ' + unused.name);
  await click('Use ' + csv.name);
  if (draft.attachments.length !== 2 || draft.attachments[1].id !== csv.id) throw new Error('Unused upload was not attached');
  const saved = JSON.parse(sessionStorage.getItem('dani-project-draft'));
  if (saved.attachments.length !== 2 || JSON.stringify(saved).includes(literal)) throw new Error('Draft persistence lost IDs or retained extracted content');
  await click('Remove ' + csv.name); await click('Delete unused ' + unused.name);
  if (draft.attachments.length !== 1 || draft.unused.length) throw new Error('Removal left stale attachment records');
  await click('Cancel'); // A lost create response can leave an already-bound ID in the local draft.
  if (draft.attachments.length || draft.name || creating) throw new Error('Bound-file response trapped the local draft');
  for (const id of [doc.id, csv.id, unused.id]) {
    const removal = requests.filter(request => request.path === '/api/attachments/' + id + '/remove');
    if (removal.length !== 1 || removal[0].options.method !== 'POST' || removal[0].options.headers['X-Dani-Token'] !== 'token') throw new Error('Unsafe or duplicate removal request');
  }
  if (requests.some(request => request.path.includes('/approve'))) throw new Error('Rendering an upload triggered model approval');
  console.log('ATTACHMENTS_OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


USABILITY_SCENARIO = """
(async () => {
  await new Promise(r => setImmediate(r));
  const all = () => descendants(document.getElementById('main'));
  const buttons = () => all().filter(item => item.tagName === 'BUTTON');
  const button = text => buttons().find(item => item.textContent === text);
  const text = () => document.getElementById('main').textContent;
  const draftProduct = {...DATA.state.products[0], id: 'draft0000001', name: 'Draft', state: 'awaiting_approval', steps: [], preview_url: null,
    description: '\\u06cc\\u06a9 \\u0628\\u0631\\u0646\\u0627\\u0645\\u0647'};
  const paused = {...draftProduct, id: 'paused000001', name: 'Paused', state: 'stopped', reason: 'PRODUCT_STEP_1_QUOTA_EXHAUSTED'};
  const cancelled = {...paused, id: 'cancel000001', name: 'Cancelled', cancelled_by_user: true};
  DATA.state.products.push(draftProduct, paused, cancelled);
  DATA.state.progress = {phase: 'idle'}; DATA.state.review_task_state = null;
  connecting = false; creating = false; selected = draftProduct.id; await refresh();

  // 1. A click that is still being answered stays disabled through the 2.5-second redraw, and is sent once.
  let release, sent = 0;
  const answer = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    if (String(path).endsWith('/approve')) { sent += 1; await new Promise(r => { release = r; }); return {status: 200, ok: true, json: async () => ({})}; }
    return answer(path, options);
  };
  const approve = button('Approve and build');
  if (!approve || approve.disabled || approve.className !== 'primary') throw new Error('Approve is not the main action of a draft');
  const clicked = approve.onclick({target: approve});
  await new Promise(r => setImmediate(r));
  await refresh();                                   // the page's own polling redraws everything
  const redrawn = buttons();
  if (!redrawn.length || redrawn.some(item => !item.disabled)) throw new Error('A redraw re-enabled the buttons of a pending action');
  if (!button('Working\\u2026')) throw new Error('The pending action does not say it is working');
  await redrawn[0].onclick({target: redrawn[0]});    // a second click while pending sends nothing
  if (sent !== 1) throw new Error('The pending action was sent ' + sent + ' times');
  release(); await clicked;
  if (buttons().some(item => item.disabled)) throw new Error('Buttons stayed disabled after the answer');

  // 2. Nothing to preview or download before a step is done; a right-to-left description keeps its direction.
  if (button('Preview') || all().some(item => item.tagName === 'A')) throw new Error('A draft offers Preview or Download');
  const description = all().find(item => item.tagName === 'P' && item.textContent === draftProduct.description);
  if (!description || description.dir !== 'auto') throw new Error('The description has no automatic text direction');
  if (!text().includes('What you approve') || !text().includes('tried again, as often as it takes')) throw new Error('The approval scope is not in full view');

  // 3. A paused build resumes by itself: Build again is the main action only after the user's own Cancel.
  selected = paused.id; render();
  if (!button('Build again') || button('Build again').className === 'primary') throw new Error('Paused: Build again must not be the main action');
  if (!text().includes('resumes it by itself')) throw new Error('Paused: no automatic-resume text');
  selected = cancelled.id; render();
  if (button('Build again').className !== 'primary') throw new Error('Cancelled: Build again must be the main action');

  // 4. Only Dani Agent's own checks are open: the page does not ask the user to connect accounts.
  const banner = () => all().find(item => item.className === 'banner');
  Object.assign(state, {connect_ready: false, connect_missing: ['self_test', 'claude_startup'], connect_checks: {self_test: 'running', claude_startup: 'waiting', codex_sandbox: 'passed'}});
  render();
  if (!banner() || !banner().textContent.includes('checking itself') || banner().textContent.includes('Finish connecting')) throw new Error('Self-check shown as a connect request');
  state.connect_checks.self_test = 'failed'; render();
  if (!banner().textContent.includes('did not pass')) throw new Error('A failed check is not reported');
  state.connect_missing = ['sign_in', 'self_test']; render();
  if (!banner().textContent.includes('Finish connecting')) throw new Error('A missing sign-in is not reported');

  // 5. Readiness is asked again while it is not reached, and the answer clears the banner without a reload.
  const paths = [];
  globalThis.fetch = async path => { paths.push(String(path)); return {status: 200, ok: true, json: async () => String(path).includes('connect') ? {...DATA.conn, ready: true, missing: []} : DATA.state}; };
  connectAsked = 0;
  for (const f of intervals) await f();
  if (!paths.includes('/api/connect')) throw new Error('Readiness was not asked again');
  if (state.connect_ready !== true || banner()) throw new Error('The banner stayed after the checks passed');

  // 6. While a path is being typed on the Connect page, the background recheck does not redraw it.
  connecting = true; conn = {...DATA.conn}; render();
  const field = all().find(item => item.tagName === 'INPUT' && item.placeholder.startsWith('Or paste'));
  field.value = 'C:/typed/half'; field.focus();
  await recheckConnect();
  if (document.activeElement !== field || !document.getElementById('main').contains(field)) throw new Error('The recheck replaced a field being typed in');

  // 7. A self-test is named, and a retry pause is not called "running".
  connecting = false; selected = paused.id;
  state.progress = {phase: 'starting', task_kind: 'offline_tests'}; render();
  if (!nowRunning().textContent.includes('self-test')) throw new Error('The self-test is not named');
  state.progress = {phase: 'product_retrying', product: paused.id, what: 'step 1', attempt: 2, wait_seconds: 600, reason: 'QUOTA_EXHAUSTED'};
  if (nowRunning().textContent.includes('Now running') || !nowRunning().textContent.includes('Waiting to try again')) throw new Error('A pause is shown as running');
  console.log('USABILITY_OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


class PageScriptTests(unittest.TestCase):
    """2026-10-22: the Python tests never ran the page's JavaScript, so a ReferenceError shipped. Run it."""

    def test_pending_actions_banners_and_status_lines_say_what_is_true(self):
        # 2026-11-10: the redraw re-enabled a pending Approve; "finish connecting" showed during a self-test and
        # stayed until a reload; a paused build's main button was Build again; a pause was called "Now running".
        out = self.run_page(D.PAGE, USABILITY_SCENARIO)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('USABILITY_OK', out.stdout)

    def page_data(self):
        step = {'title': 'Firework', 'files': ['public/app.js'], 'state': 'running', 'difficulty': 'hard',
                'models': {'codex': {'model': 'gpt-strong', 'effort': 'high'}, 'claude': {'model': 'opus', 'effort': 'high'}}}
        product = {'id': 'abc123def456', 'name': 'test3', 'description': 'Even numbers get fireworks.', 'kind': 'web',
                   'state': 'building', 'reason': None, 'app': 'C:/DaniAgentWork/products/abc123def456/test3',
                   'budget': {'max_steps': 8, 'settings': {'max_codex_sessions': 3, 'max_claude_sessions': 3, 'seconds_per_session': 240}},
                   'steps': [step, dict(step, state='failed', difficulty='simple', reason='PRODUCT_STEP_2_MODEL_NOT_IN_YOUR_PLAN_CODEX',
                                        developer_summary='blocked')], 'preview_url': 'http://127.0.0.2:5000/', 'task_state': 'running'}
        polished = dict(product, id='fed654cba321', name='test4', state='ready', open_notes=2, preview_url=None, task_state='completed',
                        polish_reason='PRODUCT_STEP_3_CORRECTION_LIMIT_REACHED', build_mode='polish',
                        steps=[dict(step, state='done', notes_handled=False, review={'verdict': 'pass', 'findings': [
                                   {'severity': 'low', 'file': 'public/app.js', 'line': 3, 'explanation': 'Handle empty input.'}]}),
                               dict(step, title="Polish: act on Claude's review notes", state='done', polish=True,
                                    developer_summary='Note 1: fixed - handled empty input', notes=[])])
        state = {'products': [product, polished], 'kinds': {'web': 'Website / web app', 'python': 'Python app'}, 'max_steps': 8,
                 'progress': {'phase': 'product_step', 'product': product['id'], 'step': 1, 'of': 2, 'role': 'codex',
                              'agent': 'Codex', 'model': 'gpt-strong', 'effort': 'high'},
                 'review_current': False, 'review_task_state': 'running', 'integration_ready': True, 'connect_ready': True}
        tools = {name: {'path': 'C:/x/' + name + '.exe', 'version': '1', 'ok': True} for name in ('codex', 'claude', 'python', 'git')}
        conn = {'tools': tools, 'install': {name: 'install it' for name in tools}, 'found': {'codex': [{'path': 'C:/y/codex.exe', 'version': '2'}]},
                'login': {'codex': {'ok': True, 'signed_in': True}, 'claude': {'ok': False, 'signed_in': False, 'plan': None},
                          'environment_overrides': ['ANTHROPIC_BASE_URL', 'OPENAI_API_KEY'],
                          'environment_inherited': ['ANTHROPIC_BASE_URL'], 'environment_in_windows': ['OPENAI_API_KEY']},
                'folder': {'path': 'C:/DaniAgentWork', 'exists': True, 'private': True, 'ready': True},
                'self_test': True, 'reviewer_profile': 'current', 'codex_host_check': False,
                'checks': {'self_test': 'passed', 'claude_startup': 'running', 'codex_sandbox': 'failed'},
                'models': {'codex': {'available': ['gpt-strong', 'gpt-everyday'], 'refused': ['gpt-old']},
                           'claude': {'available': ['opus', 'sonnet', 'claude-opus-4-8'], 'refused': ['haiku'],
                                      'labels': {'opus': 'Opus (latest)', 'claude-opus-4-8': 'Opus 4.8'}, 'needs_credits': ['Fable 5.1']}},
                'everyday': {'codex': 'gpt-everyday', 'claude': 'sonnet'}, 'ready': False}
        return {'state': state, 'conn': conn}

    def run_page(self, page, scenario=SCENARIO):
        script = page.split('<script>')[1].split('</script>')[0]
        program = 'const DATA = ' + json.dumps(self.page_data()) + ';\n' + SHIM + script + scenario
        node = str(PR.W.NODE) if PR.W.NODE.is_file() else 'node'
        return subprocess.run([node, '-'], input=program, capture_output=True, text=True, timeout=60)

    def test_the_page_script_renders_every_view_and_polls_without_errors(self):
        out = self.run_page(D.PAGE)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('PAGE_OK', out.stdout)

    def test_polling_preserves_product_focus_and_active_draft_inputs(self):
        out = self.run_page(D.PAGE, FOCUS_SCENARIO)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('FOCUS_OK', out.stdout)

    def test_new_product_attachment_preview_recovery_and_cancel_controls(self):
        out = self.run_page(D.PAGE, ATTACHMENT_SCENARIO)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn('ATTACHMENTS_OK', out.stdout)


class ExclusivePortTests(unittest.TestCase):
    def test_no_other_program_can_share_the_dashboard_port(self):
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(('127.0.0.1', 0))
        squatter.listen()
        self.addCleanup(squatter.close)
        with self.assertRaises(OSError):   # serve() then falls back to a free port
            D.Server(('127.0.0.1', squatter.getsockname()[1]), None)
        server = D.Server(('127.0.0.1', 0), None)
        self.addCleanup(server.server_close)
        late = socket.socket()
        late.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.addCleanup(late.close)
        with self.assertRaises(OSError):
            late.bind(('127.0.0.1', server.server_address[1]))

if __name__ == '__main__':
    unittest.main()
