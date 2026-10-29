"""Model-free delivery regressions; no generated code or provider sessions are executed."""
import json
from pathlib import Path
import sys
import threading
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import connect as K, dashboard as D, project_workflow as F, coordinator as C


class DeliveryGuardTests(unittest.TestCase):
    def test_scheduler_log_retains_codes_without_exception_paths_or_secrets(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'prototype') as temporary:
            dashboard = D.Dashboard.__new__(D.Dashboard)
            dashboard.store = Mock(path=Path(temporary) / 'history.sqlite3')
            dashboard.scheduler_error('test', PermissionError(13, 'private credential=synthetic', r'C:\private\file'))
            dashboard.scheduler_error('test', RuntimeError('WORK_FOLDER_RECOVERY_PENDING'))
            raw = (Path(temporary) / 'scheduler_errors.log').read_text()
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertIsNone(rows[0]['code'])
            self.assertEqual(rows[0]['errno'], 13)
            self.assertEqual(rows[1]['code'], 'WORK_FOLDER_RECOVERY_PENDING')
            self.assertNotIn('private', raw)
            self.assertNotIn('credential', raw)

    def recovery_dashboard(self):
        dashboard = D.Dashboard.__new__(D.Dashboard)
        dashboard.lock = threading.Lock()
        dashboard.storage_retry_at = 0.0
        dashboard.engine = Mock(current=None, offline_only=True, host_context={'appcontainer': False, 'elevated': False})
        dashboard.store = Mock()
        dashboard.checks, dashboard.connect_cache = {'folder': ('old', {})}, {'ready': True}
        dashboard.previews = Mock()
        return dashboard

    def test_live_move_activation_failure_blocks_new_drafts_and_claims_until_recovered(self):
        # Actual move/recovery on synthetic files. The existing app stays open, but cannot use the old root.
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory(prefix='delivery_move_', dir=ROOT / 'prototype') as temporary:
            base = Path(temporary)
            old, new, setup = base / 'old', base / 'new', base / 'setup.json'
            (old / 'products/kept').mkdir(parents=True)
            (old / 'runs').mkdir()
            (old / 'products/kept/file.txt').write_text('synthetic evidence')
            setup.write_text(json.dumps({'work_root': str(old)}))
            dashboard = self.recovery_dashboard()
            dashboard.store.tasks.return_value = []
            with ExitStack() as patches:
                patches.enter_context(patch.dict(K.APP, {'SETUP_FILE': setup, 'load_setup': lambda: json.loads(setup.read_text())}))
                patches.enter_context(patch.object(K, 'work_root', return_value=old))
                patches.enter_context(patch.object(K, 'check_work_root', return_value=new))
                patches.enter_context(patch.object(K, 'lock_work_root', side_effect=lambda root:
                    [(root / name).mkdir(parents=True, exist_ok=True) for name in ('products', 'runs')]))
                patches.enter_context(patch.object(D.PR, 'PRODUCTS', old / 'products'))
                with patch.object(K, 'reload_app', side_effect=OSError('synthetic activation failure')):
                    with self.assertRaises(OSError):
                        dashboard.connect('folder', {'path': str(new)})
                    self.assertEqual(json.loads(setup.read_text())['work_root'], str(new))
                    self.assertEqual(D.PR.PRODUCTS, old / 'products')
                    self.assertTrue((new / 'products/kept/file.txt').is_file())
                    with patch.object(D.PR, 'create') as create:
                        with self.assertRaisesRegex(ValueError, 'WORK_FOLDER_RECOVERY_PENDING'):
                            dashboard.create({'name': 'Draft', 'description': 'Synthetic description', 'kind': 'python'})
                        create.assert_not_called()
                    with patch.object(D.PR, 'reconcile') as reconcile:
                        state = dashboard.state()
                        self.assertTrue(state['storage_recovery'])
                        self.assertEqual(state['products'], [])
                        reconcile.assert_not_called()
                    dashboard.tick()
                    dashboard.engine.tick.assert_not_called()
                dashboard.storage_retry_at = 0.0
                with patch.object(K, 'reload_app'):
                    dashboard.tick()
                self.assertEqual(D.PR.PRODUCTS, new / 'products')
                self.assertFalse((base / 'work_root_move.json').exists())
                self.assertEqual(dashboard.checks, {})
                self.assertIsNone(dashboard.connect_cache)
                dashboard.engine.tick.assert_called_once()

    def test_pending_move_blocks_all_automatic_actions_and_failed_recovery_is_retried(self):
        dashboard = self.recovery_dashboard()
        pending = Mock()
        pending.exists.return_value = True
        with patch.object(K, 'move_journal', return_value=pending), \
                patch.object(K, 'recover_work_root_move', return_value='INCOMPLETE') as recover, \
                patch.object(dashboard, 'reconcile') as reconcile:
            dashboard.scheduled_round()
            reconcile.assert_not_called()
            dashboard.tick()
            dashboard.tick()   # do not hammer the failed disk twice in half a second
            self.assertEqual(recover.call_count, 1)
            dashboard.engine.tick.assert_not_called()
            dashboard.storage_retry_at = 0.0
            def finish():
                pending.exists.return_value = False
                return 'FINISHED'
            recover.side_effect = finish
            dashboard.tick()
            self.assertEqual(recover.call_count, 2)
            dashboard.engine.tick.assert_called_once()

    def test_recovery_gate_is_checked_inside_the_mutating_http_action_lock(self):
        # No server, sockets, or session cookie access: exercise the real POST handler on a synthetic request.
        import io
        dashboard = self.recovery_dashboard()
        dashboard.token = 'synthetic-token'
        Handler = D.handler_class(dashboard, 8765)
        handler = Handler.__new__(Handler)
        handler.headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765',
            'Cookie': 'dani=synthetic-token', 'X-Dani-Token': 'synthetic-token', 'Content-Length': '2'}
        # __new__ bypasses BaseHTTPRequestHandler.setup(), which normally supplies
        # the socket. Keep the real request timeout path exercised by this fixture.
        handler.connection = Mock(spec=['settimeout'])
        handler.send = Mock()
        def pending():
            self.assertTrue(dashboard.lock.locked())
            raise ValueError('WORK_FOLDER_RECOVERY_PENDING')
        with patch.object(dashboard, 'require_storage', side_effect=pending) as guard, \
                patch.object(dashboard, 'create') as create, \
                patch.object(dashboard, 'upload_attachment') as upload, \
                patch.object(dashboard, 'remove_attachment') as remove:
            for route in ('/api/products', '/api/attachments', '/api/attachments/synthetic/remove'):
                with self.subTest(route=route):
                    handler.path, handler.rfile = route, io.BytesIO(b'{}')
                    handler.connection.reset_mock()
                    handler.send.reset_mock()
                    guard.reset_mock()
                    handler.do_POST()
                    handler.connection.settimeout.assert_called_once_with(35)
                    guard.assert_called_once()
                    create.assert_not_called()
                    upload.assert_not_called()
                    remove.assert_not_called()
                    handler.send.assert_called_once_with(400, {'error': 'WORK_FOLDER_RECOVERY_PENDING'})

    def test_model_free_dashboard_never_claims_even_preapproved_model_tasks(self):
        store = Mock()
        store.claim.return_value = None
        with patch.dict(C.APP['PROCESS']['DIAGNOSTICS'], {'execution_context': lambda: {'appcontainer': False, 'elevated': False}}), \
                patch.object(C, 'integration_ready', return_value=True):
            engine = C.Coordinator(store, offline_only=True)
            engine.tick()
        self.assertEqual(store.claim.call_args.args[1], {'refresh_evidence', 'offline_tests'})

    def test_model_free_dashboard_refuses_approval_before_any_side_effect(self):
        dashboard = D.Dashboard.__new__(D.Dashboard)
        dashboard.engine = Mock(offline_only=True)
        dashboard.store = Mock()
        with patch.object(D.PR, 'request_build') as build:
            for action in (lambda: dashboard.approve('synthetic'), dashboard.queue_review,
                           lambda: dashboard.approved('reviewer_profile', 'synthetic', {}),
                           lambda: dashboard.queue_host_check('synthetic')):
                with self.assertRaisesRegex(ValueError, 'MODEL_SESSIONS_DISABLED'):
                    action()
            build.assert_not_called()
            dashboard.store.add.assert_not_called()
            dashboard.store.approve.assert_not_called()

    def test_actual_review_prompt_counts_schema_omissions_and_json_escaping(self):
        handoff = {'diff': '', 'context_left_out_for_size': ['long/path/test.py'], 'sources': {}}
        schema = {'description': '\u2028' * 20, 'type': 'object'}
        overhead = len(F.review_prompt(handoff, schema))
        handoff['diff'] = 'x' * (F.REVIEW_HARD_CHARS - overhead)
        prompt = F.review_prompt(handoff, schema)
        self.assertEqual(len(prompt), F.REVIEW_HARD_CHARS)
        self.assertEqual(json.loads(prompt.split('\nEvidence:\n')[1]), handoff)
        handoff['diff'] += 'x'
        # The old base-only check accepts this; the actual request must reject it before a call.
        self.assertLess(len(json.dumps(handoff)), F.REVIEW_HARD_CHARS)
        with self.assertRaisesRegex(RuntimeError, 'PROJECT_CHANGE_TOO_LARGE_TO_REVIEW'):
            F.review_prompt(handoff, schema)

    def test_unresolved_folder_recovery_prevents_all_scheduler_startup(self):
        for outcome in ('INCOMPLETE', OSError('synthetic activation failure')):
            with self.subTest(outcome=type(outcome).__name__), patch.object(D, 'InstanceLock') as lock, \
                    patch.object(D, 'Store') as store, patch.object(D, 'Coordinator') as engine, \
                    patch.object(D, 'import_evidence') as evidence, \
                    patch.object(K, 'recover_work_root_move', **(
                        {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome})):
                with self.assertRaises((OSError, RuntimeError)):
                    D.serve(open_browser=False)
                store.assert_not_called()
                engine.assert_not_called()
                evidence.assert_not_called()
                lock.return_value.close.assert_called_once()


class LiveRunLocationTests(unittest.TestCase):
    """2026-10-29: the clean repository has AGENTS.md at its root, and the preflight refuses any AGENTS.md
    above a run. Live runs made under prototype/runs were all blocked (ANCESTOR_INSTRUCTIONS_REQUIRE_REVIEW),
    so no source review passed and every new product stopped before its plan."""

    @staticmethod
    def instructions_above(run):
        # The same walk as orchestrator.preflight(): run/repo and every parent folder.
        return [parent for parent in (run / 'repo', *(run / 'repo').parents)
                if any((parent / name).exists() for name in ['AGENTS.md', 'AGENTS.override.md']
                       + ([] if parent == Path.home() else ['.codex/config.toml']))]

    def test_a_live_run_is_made_in_the_work_folder_where_the_preflight_finds_no_instructions(self):
        import runpy
        import fixture_runs as TF
        prepare = runpy.run_path(str(C.PROTO / 'prepare_live_run.py'))['prepare']
        run = prepare(TF.reviewer_source())
        self.addCleanup(K.remove_tree, run)
        self.assertEqual(run.parent.resolve(), C.APP['WORK_RUNS'].resolve())
        self.assertFalse(run.resolve().is_relative_to(ROOT.resolve()))
        self.assertEqual(self.instructions_above(run), [])
        self.assertEqual(C.APP['verified_startup_gate'](run)['verdict'], 'PASS')

    def test_reviews_from_both_folders_are_found_newest_first(self):
        from dani_agent import milestone_review as R
        with tempfile.TemporaryDirectory(prefix='review_runs_', dir=ROOT / 'prototype') as temporary:
            old, work = Path(temporary) / 'old', Path(temporary) / 'work'
            for root, name in ((old, '20261020T000000Z_aaaaaaaa'), (work, '20261029T000000Z_bbbbbbbb'),
                               (old, '20261025T000000Z_cccccccc')):
                (root / name / 'artifacts').mkdir(parents=True)
                (root / name / 'artifacts/milestone_plan.json').write_text(json.dumps({'name': name}))
            with patch.object(R, 'RUNS', old), patch.dict(R.APP, {'WORK_RUNS': work}):
                self.assertEqual([plan['name'] for _, plan in R.reviews()],
                                 ['20261029T000000Z_bbbbbbbb', '20261025T000000Z_cccccccc', '20261020T000000Z_aaaaaaaa'])

    def test_the_reviewer_profile_is_found_in_the_work_folder_or_where_an_earlier_version_made_it(self):
        with tempfile.TemporaryDirectory(prefix='profile_runs_', dir=ROOT / 'prototype') as temporary:
            base = Path(temporary)
            data, old, work = base / 'data', base / 'old', base / 'work'
            for folder in (data, old / '20261018T000000Z_aaaaaaaa', work / '20261029T000000Z_bbbbbbbb'):
                folder.mkdir(parents=True)
            with patch.object(C, 'DATA', data), patch.object(C, 'RUNS', old), patch.dict(C.APP, {'WORK_RUNS': work}):
                for name, expected in (('20261029T000000Z_bbbbbbbb', work), ('20261018T000000Z_aaaaaaaa', old)):
                    (data / 'reviewer_profile.json').write_text(json.dumps({'run': name}))
                    self.assertEqual(C.reviewer_source(), expected / name)


if __name__ == '__main__':
    unittest.main()
