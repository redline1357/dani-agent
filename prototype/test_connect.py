"""Connect page: program confirmation, sign-in status, private folder, model evidence. Zero model calls."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import connect as K, coordinator as C, dashboard as D, model_settings as M, products as PR, projects as P
from dani_agent.state import Store
import fixture_runs as TF


class Done:
    def __init__(self, stdout='', returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, '', returncode


class ModelEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='dani_models_'))
        self.addCleanup(shutil.rmtree, self.folder, True)
        for patcher in (patch.object(M, 'PROBES', self.folder / 'model_probes.json'),
                        patch.dict(M.APP['TOOLS'], {'codex': {'path': 'x', 'version': 'codex-cli 1.0'},
                                                    'claude': {'path': 'y', 'version': '9.9 (Claude Code)'}})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_models_come_from_the_clis_own_lists_without_test_prompts(self):
        with patch.object(M, 'codex_catalog', return_value=['gpt-a', 'gpt-b']):
            self.assertEqual(M.ranked_models('codex'), ['gpt-a', 'gpt-b'])
            self.assertEqual(M.ranked_models('claude')[:3], ['opus', 'sonnet', 'haiku'])   # latest first, then older
            self.assertEqual(M.validate({**M.DEFAULTS, 'codex_model': 'gpt-b'})['codex_model'], 'gpt-b')
            with self.assertRaisesRegex(ValueError, 'MODEL_NOT_SUBSCRIPTION_VERIFIED'):
                M.validate({**M.DEFAULTS, 'codex_model': 'gpt-unlisted'})
            with patch.dict(M.APP['TOOLS'], {'codex': {'path': 'x'}}):   # no confirmed version: nothing
                self.assertEqual(M.ranked_models('codex'), [])

    def test_without_codexs_list_no_model_is_assumed(self):
        with patch.object(M, 'codex_catalog', return_value=[]):
            self.assertEqual(M.ranked_models('codex'), [])
            M.record_probe('codex', 'gpt-x', True, 'run1')   # only what already worked
            self.assertEqual(M.ranked_models('codex'), ['gpt-x'])

    def test_a_refused_model_is_skipped_until_the_cli_updates(self):
        with patch.object(M, 'codex_catalog', return_value=['gpt-a', 'gpt-b']):
            M.record_probe('codex', 'gpt-a', False, 'run1')   # a real session said: not in your plan
            self.assertEqual((M.ranked_models('codex'), M.refused_models('codex')), (['gpt-b'], ['gpt-a']))
            with patch.dict(M.APP['TOOLS'], {'codex': {'path': 'x', 'version': 'codex-cli 2.0'}}):
                self.assertEqual(M.ranked_models('codex'), ['gpt-a', 'gpt-b'])   # an update gives it a new chance
            M.record_probe('codex', 'gpt-x', True, 'run2')    # worked though not listed
            self.assertIn('gpt-x', M.ranked_models('codex'))

    def test_automatic_settings_choose_by_difficulty(self):
        with patch.object(M, 'codex_catalog', return_value=['gpt-strong', M.DEFAULTS['codex_model']]):
            auto = M.auto_settings()
            self.assertEqual((auto['mode'], auto['codex_model'], auto['claude_model']), ('auto', M.DEFAULTS['codex_model'], 'sonnet'))
            for difficulty, effort, codex, claude in (('simple', 'low', M.DEFAULTS['codex_model'], 'sonnet'),
                                                      ('normal', 'medium', M.DEFAULTS['codex_model'], 'sonnet'),
                                                      ('hard', 'high', 'gpt-strong', 'opus')):
                step = M.for_step(auto, difficulty)
                self.assertEqual((step['codex_effort'], step['claude_effort'], step['codex_model'], step['claude_model']),
                                 (effort, effort, codex, claude))
                M.validate(step)
            with self.assertRaises(ValueError):
                M.for_step(auto, 'extreme')
            manual = {**M.DEFAULTS, 'mode': 'manual', 'codex_effort': 'low'}
            self.assertEqual(M.for_step(manual, 'hard'), manual)
            older = {k: v for k, v in M.DEFAULTS.items() if k != 'mode'}   # saved before modes existed
            self.assertEqual(M.for_step(M.validate(older), 'hard'), older)
            M.record_probe('codex', M.DEFAULTS['codex_model'], False, 'r')   # everyday model refused
            self.assertEqual(M.auto_settings()['codex_model'], 'gpt-strong')

    def test_a_refused_everyday_model_is_replaced_so_build_again_works(self):
        with patch.object(M, 'codex_catalog', return_value=[M.DEFAULTS['codex_model'], 'gpt-b']):
            approved = M.auto_settings()                       # what the product was created (and approved) with
            M.record_probe('codex', M.DEFAULTS['codex_model'], False, 'run1')   # then the plan refused it
            step = M.for_step(approved, 'normal')
            self.assertEqual(step['codex_model'], 'gpt-b')
            M.validate(step)                                   # so the rebuild is not stuck on the refused model

    def test_claude_lists_latest_and_older_models_but_never_paid_credit_ones(self):
        home = Path(tempfile.mkdtemp(prefix='dani_home_'))
        self.addCleanup(shutil.rmtree, home, True)
        (home / '.claude.json').write_text(json.dumps({'cachedGrowthBookFeatures': {'tengu_usage_overage_included_models': ['Fable']},
            'additionalModelOptionsCache': [{'value': 'claude-fable-5-1[1m]', 'label': 'Fable', 'description': 'Fable 5.1 \u00b7 Most capable'}]}))
        with patch.object(M.Path, 'home', return_value=home):
            listed = K.models()['claude']
        self.assertEqual(listed['available'][:3], ['opus', 'sonnet', 'haiku'])
        self.assertIn('claude-opus-4-8', listed['available'])
        self.assertEqual(listed['labels']['claude-opus-4-8'], 'Opus 4.8')
        self.assertEqual(listed['needs_credits'], ['Fable 5.1'])
        self.assertFalse(any('fable' in m for m in listed['available']))

    def test_a_refusal_is_recognised_only_when_it_is_about_the_model(self):
        self.assertFalse(M.model_refused({'error': 'The model is temporarily not available, please try again'}))
        self.assertFalse(M.model_refused({'error': 'model overloaded: not available for your plan right now, try again'}))
        self.assertTrue(M.model_refused({'error': 'This model is not available on your plan.'}))
        self.assertTrue(M.model_refused({'error': 'The model gpt-x does not exist or you do not have access to it.'}))
        self.assertTrue(M.model_refused({'events': [{'type': 'error', 'message': "model 'opus' is not available on your plan"}]}))
        self.assertTrue(M.model_refused({'events': [{'type': 'error', 'message': 'not_found_error: model: claude-x'}]}))
        # Codex: process.py keeps the message only in sanitized_error_contexts
        self.assertTrue(M.model_refused({'events': [{'type': 'turn.failed'}], 'sanitized_error_contexts': [
            {'stream': 'stderr', 'text': "The 'gpt-x' model is not supported when using Codex with a ChatGPT account."}]}))
        self.assertFalse(M.model_refused({'events': [{'type': 'result', 'subtype': None}]}))   # no crash
        self.assertFalse(M.model_refused({'error': 'timeout'}))
        self.assertFalse(M.model_refused({'events': [{'type': 'agent_message', 'text': 'the model does not exist yet'}]}))

    def test_legacy_evidence_holds_only_for_the_cli_version_confirmed_at_import(self):
        with patch.object(M, 'legacy_evidence', return_value=Path('C:/runs/old_success')):
            with patch.dict(M.APP['TOOLS'], {'claude': {}}):          # only Codex confirmed so far
                self.assertEqual([e['provider'] for e in M.import_legacy_evidence()], ['codex'])
            entries = M.import_legacy_evidence()                       # Claude confirmed later: its turn now
            self.assertEqual([e['provider'] for e in entries], ['claude'])
            self.assertEqual(M.import_legacy_evidence(), [])           # once per CLI
        self.assertEqual({(p['cli_version'], p['source']) for p in M.probes()},
                         {('codex-cli 1.0', 'LEGACY_SUCCESSFUL_RUN'), ('9.9 (Claude Code)', 'LEGACY_SUCCESSFUL_RUN')})
        self.assertIn(M.DEFAULTS['codex_model'], M.verified_models('codex'))

    def test_model_names_cannot_be_options_or_arbitrary_text(self):
        for bad in ('--dangerously-bypass', '-m', 'a b', '', 'x' * 81, None, 'gpt;rm'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                M.model_name(bad)
        for good in ('gpt-6-astra', 'sonnet', 'sonnet[1m]', 'claude-sonnet-4-5-20250929'):
            self.assertEqual(M.model_name(good), good)


class ProgramTests(unittest.TestCase):
    def test_problems_block_wrong_programs_and_python_in_the_user_profile(self):
        home = Path.home()
        self.enterContext(patch.object(K, 'own_folder', return_value=True))
        self.assertIsNone(K.problem('codex', Path('C:/x/codex.exe'), 'codex-cli 0.163.0'))
        self.assertIsNotNone(K.problem('codex', Path('C:/x/codex.exe'), 'Python 3.12.1'))
        self.assertIsNone(K.problem('claude', Path('C:/x/claude.exe'), '2.1.292 (Claude Code)'))
        self.assertIsNotNone(K.problem('claude', Path('C:/x/claude.exe'), None))
        self.assertIsNone(K.problem('python', Path('C:/Program Files/Python312/python.exe'), 'Python 3.12.6'))
        self.assertIn('3.12', K.problem('python', Path('C:/Python311/python.exe'), 'Python 3.11.9'))
        self.assertIn('user folder', K.problem('python', home / 'AppData/Local/Programs/Python/Python312/python.exe', 'Python 3.12.6'))

    def test_python_and_git_must_be_picked_from_their_own_install_folder(self):
        folder = Path(tempfile.mkdtemp(prefix='dani_own_'))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / 'Python312/Lib').mkdir(parents=True)
        (folder / 'Python312/Lib/os.py').write_text('')
        (folder / 'Git/cmd').mkdir(parents=True)
        (folder / 'Git/mingw64').mkdir()
        (folder / 'loose').mkdir()
        self.assertTrue(K.own_folder('python', folder / 'Python312/python.exe'))
        self.assertTrue(K.own_folder('git', folder / 'Git/cmd/git.exe'))
        for name, path in (('python', folder / 'loose/python.exe'), ('git', folder / 'loose/git.exe'),
                           ('python', Path('C:/python.exe')), ('git', Path('C:/Windows/git.exe')),
                           ('python', Path('C:/Program Files/python.exe'))):
            with self.subTest(path=path):
                self.assertFalse(K.own_folder(name, path))   # the sandbox would read that whole folder
        with patch.object(K, 'under_profile', return_value=False):
            self.assertIn('own installation folder', K.problem('python', Path('C:/python.exe'), 'Python 3.12.6'))
            self.assertIn('installation folder', K.problem('git', Path('C:/git.exe'), 'git version 2.45.1'))

    def test_confirm_records_exact_path_version_and_hash(self):
        folder = Path(tempfile.mkdtemp(prefix='dani_setup_'))
        self.addCleanup(shutil.rmtree, folder, True)
        exe = folder / 'git.exe'
        exe.write_bytes(b'fake git')
        with patch.dict(K.APP, {'SETUP_FILE': folder / 'setup.json', 'load_setup': lambda: {}}), \
                patch.object(K, 'version_of', return_value='git version 2.45.1.windows.1'), patch.object(K, 'reload_app'), \
                patch.object(K, 'under_profile', return_value=False), \
                patch.object(K, 'own_folder', return_value=True):        # the temp folder is in the profile
            entry = K.confirm('git', str(exe))
            for bad in ('relative.exe', str(folder / 'missing.exe'), str(folder)):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    K.confirm('git', bad)
            with self.assertRaises(ValueError):
                K.confirm('unknown', str(exe))
        saved = json.loads((folder / 'setup.json').read_text())
        self.assertEqual(saved['tools']['git'], entry)
        self.assertEqual(entry['version'], 'git version 2.45.1.windows.1')
        self.assertEqual(len(entry['sha256']), 64)

    def test_unconfirmed_cli_stops_every_live_check(self):
        with patch.dict(C.APP['TOOLS'], {}, clear=True):
            for name in ('codex', 'claude'):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'CONNECT_' + name.upper() + '_NOT_CONFIRMED'):
                    C.APP['version'](name, {})

    def test_a_store_update_of_codex_is_confirmed_by_itself_and_nothing_else_is(self):
        # 2026-10-24: the Codex app updated itself mid-build; Windows removed the confirmed package folder.
        folder = Path(tempfile.mkdtemp(prefix='dani_update_'))
        self.addCleanup(shutil.rmtree, folder, True)
        apps = folder / 'WindowsApps'
        def package(name):
            exe = apps / name / 'app/resources/codex.exe'
            exe.parent.mkdir(parents=True)
            exe.write_bytes(name.encode())
            return exe
        old = apps / 'OpenAI.Codex_26.1013.4894.0_x64__2p2nqsd0c76g0/app/resources/codex.exe'   # already removed
        new = package('OpenAI.Codex_26.1021.2667.0_x64__2p2nqsd0c76g0')
        other = package('OpenAI.Codex_26.1021.2667.0_x64__someoneelse01')                   # another publisher
        self.assertTrue(K.same_installation('codex', old, new))
        self.assertFalse(K.same_installation('codex', old, other))
        self.assertFalse(K.same_installation('codex', old, folder / 'elsewhere/codex.exe'))
        self.assertFalse(K.same_installation('claude', folder / 'a/claude.exe', folder / 'b/claude.exe'))
        setup = folder / 'setup.json'
        setup.write_text(json.dumps({'tools': {'codex': {'path': str(old), 'version': 'codex-cli 0.163.0', 'sha256': 'x'}}}))
        load = lambda: json.loads(setup.read_text())
        versions = {str(new.resolve()): 'codex-cli 0.164.0', str(other.resolve()): 'codex-cli 0.164.0'}
        with patch.dict(K.APP, {'SETUP_FILE': setup, 'load_setup': load, 'TOOLS': load()['tools']}), \
                patch.object(K, 'version_of', side_effect=lambda path: versions.get(str(Path(path).resolve()))), \
                patch.object(K, 'candidates', return_value=[other.resolve(), new.resolve()]), \
                patch.object(K, 'reload_app', side_effect=lambda: K.APP.update(TOOLS=load()['tools'])):
            self.assertTrue(K.programs_changed())
            self.assertEqual(K.follow_updates(), ['codex'])
            self.assertFalse(K.programs_changed())
            self.assertEqual(K.follow_updates(), [])                     # nothing changed since
        saved = load()
        self.assertEqual((saved['tools']['codex']['path'], saved['tools']['codex']['version']), (str(new.resolve()), 'codex-cli 0.164.0'))
        self.assertEqual([(u['program'], u['from'], u['to']) for u in saved['updated_by_itself']],
                         [('codex', 'codex-cli 0.163.0', 'codex-cli 0.164.0')])
        setup.write_text(json.dumps({'tools': {'codex': {'path': str(old), 'version': 'codex-cli 0.163.0', 'sha256': 'x'}}}))
        with patch.dict(K.APP, {'SETUP_FILE': setup, 'load_setup': load, 'TOOLS': load()['tools']}), \
                patch.object(K, 'version_of', side_effect=lambda path: versions.get(str(Path(path).resolve()))), \
                patch.object(K, 'candidates', return_value=[other.resolve()]), patch.object(K, 'reload_app'):
            self.assertEqual(K.follow_updates(), [])                     # only another publisher's: Connect page
        self.assertEqual(load()['tools']['codex']['path'], str(old))

    def test_changed_cli_version_stops_until_confirmed_again(self):
        with patch.dict(C.APP['TOOLS'], {'codex': {'path': sys.executable, 'version': 'codex-cli 1.0'}}):
            with self.assertRaisesRegex(RuntimeError, 'UNEXPECTED_CODEX_VERSION'):
                C.APP['version']('codex', None)


class SignInTests(unittest.TestCase):
    def status(self, codex_text, claude_value):
        def quiet(argv, timeout=15, environment=None):
            return Done(codex_text) if argv[1] == 'login' else Done(json.dumps(claude_value))
        with patch.object(K, 'quiet', quiet), patch.dict(K.APP, {'CODEX': 'codex.exe', 'CLAUDE': 'claude.exe'}):
            return K.sign_in_status()

    def test_only_subscription_sign_ins_count_and_no_identity_is_kept(self):
        value = self.status('Logged in using ChatGPT', {'loggedIn': True, 'authMethod': 'claude.ai',
            'subscriptionType': 'max', 'email': 'person@example.invalid', 'orgName': 'Private Org'})
        self.assertTrue(value['codex']['ok'])
        self.assertEqual(value['claude'], {'signed_in': True, 'method': 'claude.ai', 'plan': 'max', 'ok': True})
        self.assertNotIn('example.invalid', json.dumps(value))
        self.assertNotIn('Private Org', json.dumps(value))
        value = self.status('Logged in using an API key - sk-SYNTHETIC', {'loggedIn': True, 'authMethod': 'console'})
        self.assertEqual((value['codex']['method'], value['codex']['ok']), ('api_key', False))
        self.assertFalse(value['claude']['ok'])
        self.assertNotIn('sk-SYNTHETIC', json.dumps(value))
        value = self.status('Not logged in', {'loggedIn': False})
        self.assertFalse(value['codex']['signed_in'] or value['claude']['signed_in'])

    def test_api_key_environment_overrides_are_reported(self):
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'synthetic', 'OPENAI_API_KEY': 'synthetic'}):
            value = self.status('Logged in using ChatGPT', {})
        self.assertIn('ANTHROPIC_API_KEY', value['environment_overrides'])
        self.assertIn('OPENAI_API_KEY', value['environment_overrides'])
        self.assertNotIn('synthetic', json.dumps(value))


class WorkFolderPermissionTests(unittest.TestCase):   # was a second WorkFolderTests, which hid it from discovery
    """Real Windows ACLs on a disposable folder; the real C:\\DaniAgentWork is never touched."""
    def setUp(self):
        self.parent = Path(tempfile.mkdtemp(prefix='dani_acl_'))
        self.addCleanup(shutil.rmtree, self.parent, True)

    def test_creates_a_folder_only_this_user_and_system_can_open(self):
        root = self.parent / 'DaniAgentWork'
        with patch.object(K, 'work_root', return_value=root):
            status = K.lock_work_root()
            self.assertEqual((status['private'], status['ready']), (True, True))
            p = subprocess.run(['icacls', str(root)], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertNotIn('(I)', p.stdout.split('\n')[0])   # nothing inherited from the parent
            self.assertTrue((root / 'runs').is_dir() and (root / 'products').is_dir())

    def test_an_existing_shared_folder_is_reported_not_changed(self):
        root = self.parent / 'DaniAgentWork'
        root.mkdir()   # inherits the parent's ACL (Administrators etc.)
        before = subprocess.run(['icacls', str(root)], capture_output=True, text=True).stdout
        with patch.object(K, 'work_root', return_value=root):
            self.assertFalse(K.work_root_status()['private'])
            with self.assertRaisesRegex(RuntimeError, 'WORK_FOLDER_NOT_PRIVATE'):
                K.lock_work_root()
        self.assertEqual(subprocess.run(['icacls', str(root)], capture_output=True, text=True).stdout, before)
        self.assertFalse((root / 'runs').exists())


class ReviewerSourceTests(unittest.TestCase):
    def test_only_a_recorded_real_profile_is_used(self):
        folder = Path(tempfile.mkdtemp(prefix='dani_profile_'))
        self.addCleanup(shutil.rmtree, folder, True)
        with patch.object(C, 'DATA', folder), patch.object(C, 'LEGACY_SOURCE', folder / 'absent'):
            with self.assertRaisesRegex(RuntimeError, 'CONNECT_CLAUDE_REVIEWER_CHECK_REQUIRED'):
                C.reviewer_source()
            for name in ('synthetic_x', '..\\x', 'a/b', ''):
                (folder / 'reviewer_profile.json').write_text(json.dumps({'run': name}))
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    C.reviewer_source()
            (folder / 'reviewer_profile.json').write_text(json.dumps({'run': '20990101T000000Z_abcdef12'}))
            self.assertEqual(C.reviewer_source(), C.RUNS / '20990101T000000Z_abcdef12')


class Engine:
    host_context, current = {'appcontainer': False, 'elevated': False}, None
    def progress(self):
        return {'phase': 'idle'}


class ConnectActionTests(unittest.TestCase):
    def setUp(self):
        self.folder = C.DATA / ('connect_test_' + uuid.uuid4().hex[:8])
        self.folder.mkdir()
        self.store = Store(self.folder / 'state.sqlite3')
        self.dash = D.Dashboard(self.store, Engine(), 'token')
        patcher = patch.object(D.Dashboard, 'connect_status', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def click_codex(self, run='R1'):
        with patch.object(M, 'auto_settings', return_value={**M.DEFAULTS, 'codex_model': 'gpt-x'}):
            self.dash.connect('hostcheck', {})
        check = self.store.claim(C.fingerprint())
        self.assertEqual(check['payload'], {'model': 'gpt-x', 'approved_on_connect_page': True, 'fingerprint': C.fingerprint(),
                                            'approval_source': 'DASHBOARD_HUMAN_APPROVAL'})
        self.store.finish(check['id'], check['run_id'], 'completed', {'verdict': 'PASS', 'run': run, 'model': 'gpt-x'})
        C.queue_diagnostic(self.store, {'run': run, 'model': 'gpt-x'})
        return next(t for t in self.store.tasks() if t['kind'] == 'codex_diagnostic' and t['payload']['run'] == run)

    def test_a_click_never_renews_an_invalidated_or_used_approval(self):
        diagnostic = self.click_codex()
        with patch.object(D, 'check_diagnostic_plan'):
            self.dash.after_connect()
            self.assertEqual(self.store.get(diagnostic['id'])['state'], 'queued')
            # Dani Agent's code changes before the session starts: claim() puts it back for approval.
            self.assertIsNone(self.store.claim('changed source fingerprint'))
            self.assertEqual(self.store.get(diagnostic['id'])['state'], 'awaiting_approval')
            self.dash.after_connect()
            self.assertEqual(self.store.get(diagnostic['id'])['state'], 'awaiting_approval')
            # A new check replaces it (cancelled), with a fresh approval of its own.
            with patch.object(M, 'auto_settings', return_value={**M.DEFAULTS, 'codex_model': 'gpt-x'}):
                self.dash.connect('hostcheck', {})
            self.assertEqual(self.store.get(diagnostic['id'])['state'], 'cancelled')
            self.assertEqual(self.store.tasks()[0]['kind'], 'codex_preflight')

    def test_a_click_on_older_source_is_not_used_for_the_current_source(self):
        diagnostic = self.click_codex()
        with patch.object(D, 'check_diagnostic_plan'), patch.object(D, 'fingerprint', return_value='source changed after the click'):
            self.dash.after_connect()
        self.assertEqual(self.store.get(diagnostic['id'])['state'], 'awaiting_approval')

    def test_a_codex_test_click_approves_exactly_the_one_diagnostic_it_creates(self):
        self.click_codex()
        other = self.store.add('codex_diagnostic', 'Not requested on the Connect page', {'run': 'R2'})
        with patch.object(D, 'check_diagnostic_plan') as checked:
            self.dash.after_connect()
        diagnostic = next(t for t in self.store.tasks() if t['kind'] == 'codex_diagnostic' and t['id'] != other)
        self.assertEqual(diagnostic['payload'], {'run': 'R1', 'model': 'gpt-x'})
        self.assertEqual(diagnostic['state'], 'queued')
        self.assertEqual(self.store.get(other)['state'], 'awaiting_approval')
        checked.assert_called_once()

    def setup_world(self, self_test, login_ok=True, overrides=()):
        tools = {name: {'path': sys.executable, 'version': name + ' 1', 'sha256': name} for name in K.TOOLS}
        login = {'codex': {'ok': login_ok}, 'claude': {'ok': login_ok}, 'environment_overrides': list(overrides)}
        return [patch.dict(C.APP['TOOLS'], tools), patch.object(K, 'work_root_status', return_value={'ready': True}),
                patch.object(K, 'self_test_current', return_value=self_test), patch.object(K, 'sign_in_status', return_value=login),
                patch.object(K, 'reviewer_profile_state', return_value='missing'), patch.object(D, 'integration_ready', return_value=False),
                patch.object(M, 'auto_settings', return_value={**M.DEFAULTS, 'codex_model': 'gpt-x'})]

    def run_setup(self, patches, times=2):
        for item in patches:
            item.start()
        try:
            for _ in range(times):
                self.dash.auto_at = 0.0
                self.dash.auto_setup()
        finally:
            for item in reversed(patches):
                item.stop()
        return sorted((t['kind'], t['state']) for t in self.store.tasks())

    def test_setup_checks_start_by_themselves_in_order_and_only_once(self):
        self.assertEqual(self.run_setup(self.setup_world(self_test=False)), [('offline_tests', 'queued')])   # no AI first
        self.dash.checks.clear()
        kinds = self.run_setup(self.setup_world(self_test=True))
        self.assertEqual(kinds, [('codex_preflight', 'queued'), ('offline_tests', 'queued'), ('reviewer_profile', 'queued')])
        sources = [e for t in self.store.tasks() if t['kind'] in ('codex_preflight', 'reviewer_profile')
                   for e in self.store.history(t['id']) if e['kind'] == 'approval_source']
        self.assertTrue(sources and all(json.loads(e['payload'])['source'] == 'AUTOMATIC_SETUP_CHECK_ENABLED_BY_USER' if isinstance(e['payload'], str)
                                        else e['payload']['source'] == 'AUTOMATIC_SETUP_CHECK_ENABLED_BY_USER' for e in sources))

    def test_setup_checks_wait_for_sign_in_and_a_clean_environment(self):
        for world in (self.setup_world(self_test=True, login_ok=False), self.setup_world(self_test=True, overrides=['ANTHROPIC_BASE_URL'])):
            self.dash.checks.clear()
            self.assertEqual(self.run_setup(world), [])

    def test_an_automatic_sandbox_check_keeps_its_source_in_the_audit_trail(self):
        self.run_setup(self.setup_world(self_test=True), times=1)
        check = next(t for t in self.store.tasks() if t['kind'] == 'codex_preflight')
        claimed = self.store.claim(C.fingerprint(), {'codex_preflight'})
        self.assertEqual(claimed['id'], check['id'])
        self.store.finish(check['id'], claimed['run_id'], 'completed', {'verdict': 'PASS', 'run': 'R9', 'model': 'gpt-x'})
        C.queue_diagnostic(self.store, {'run': 'R9', 'model': 'gpt-x'})
        with patch.object(D, 'check_diagnostic_plan'):
            self.dash.after_connect()
        diagnostic = next(t for t in self.store.tasks() if t['kind'] == 'codex_diagnostic')
        sources = [e['payload'] for e in self.store.history(diagnostic['id']) if e['kind'] == 'approval_source']
        sources = [json.loads(p) if isinstance(p, str) else p for p in sources]
        self.assertEqual([s['source'] for s in sources], ['AUTOMATIC_SETUP_CHECK_ENABLED_BY_USER'])

    def test_tried_checks_are_remembered_across_restarts(self):
        self.run_setup(self.setup_world(self_test=False), times=1)
        self.store.cancel_pending(self.store.tasks()[0]['id'])                  # it failed
        self.dash = D.Dashboard(self.store, Engine(), 'token')                  # Dani Agent restarted
        self.assertEqual(self.run_setup(self.setup_world(self_test=False)), [('offline_tests', 'cancelled')])

    def test_a_failed_check_is_not_retried_by_itself(self):
        self.run_setup(self.setup_world(self_test=False), times=1)
        task = self.store.tasks()[0]
        self.store.cancel_pending(task['id'])   # e.g. it failed
        self.assertEqual(self.run_setup(self.setup_world(self_test=False)), [('offline_tests', 'cancelled')])
        self.dash.connect('selftest', {})        # the Retry button
        self.assertIn(('offline_tests', 'queued'), sorted((t['kind'], t['state']) for t in self.store.tasks()))

    def test_a_stale_or_missing_reviewer_profile_is_checked_again_with_the_click(self):
        for state, expected in (('current', 0), ('stale', 1), ('missing', 1)):
            with self.subTest(state=state), patch.object(K, 'reviewer_profile_state', return_value=state):
                before = sum(t['kind'] == 'reviewer_profile' for t in self.store.tasks())
                self.dash.connect('reviewer', {})
                self.dash.connect('reviewer', {})   # never two pending checks
                added = [t for t in self.store.tasks() if t['kind'] == 'reviewer_profile'][:expected]
                self.assertEqual(sum(t['kind'] == 'reviewer_profile' for t in self.store.tasks()) - before, expected)
                for task in added:
                    self.assertEqual(task['state'], 'queued')
                    self.store.cancel_pending(task['id'])

    def test_test_and_choose_buttons_are_gone(self):
        for action in ('probe', 'choose', 'approve'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'NOT_FOUND'):
                self.dash.connect(action, {})

    def test_changing_the_folder_stops_previews_and_moves_through_connect(self):
        with patch.object(K, 'move_work_root') as move, patch.object(self.dash.previews, 'stop_all') as stop:
            self.dash.connect('folder', {'path': 'D:\\DaniAgent'})
        stop.assert_called_once()
        move.assert_called_once_with('D:\\DaniAgent', self.store)


class WorkFolderTests(unittest.TestCase):
    def test_unsafe_folders_are_refused_with_a_reason(self):
        home = str(Path.home() / 'DaniAgent')
        for value, reason in (('', 'TYPE_A_FOLDER_PATH'), ('relative', 'FULL_PATH'), ('\\\\server\\share\\x', 'FULL_PATH'),
                              ('C:\\', 'NOT_A_WHOLE_DRIVE'), (home, 'OUTSIDE_YOUR_USER_FOLDER'),
                              ('C:\\Windows\\DaniAgent', 'NOT_INSIDE'), ('C:\\Program Files\\DaniAgent', 'NOT_INSIDE'),
                              (str(ROOT / 'work'), 'OUTSIDE_YOUR_USER_FOLDER|NOT_INSIDE')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, reason):
                K.check_work_root(value)

    def test_a_new_folder_on_a_local_ntfs_drive_is_accepted_and_products_move_into_it(self):
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        old, new = base / 'old', base / 'new'
        self.addCleanup(shutil.rmtree, base, True)
        (old / 'products/p1/demo').mkdir(parents=True)
        (old / 'products/p1/demo/file.txt').write_text('kept')
        (old / 'runs/r1').mkdir(parents=True)
        self.assertEqual(K.check_work_root(str(new)), new.resolve())
        store = Store(base / 'state.sqlite3')
        with patch.object(K, 'work_root', return_value=old), patch.object(K, 'lock_work_root',
                side_effect=lambda root: [(root / n).mkdir(parents=True, exist_ok=True) for n in ('runs', 'products')]), \
                patch.dict(K.APP, {'SETUP_FILE': base / 'setup.json', 'load_setup': lambda: {'tools': {}}}), \
                patch.object(K, 'reload_app'), patch.object(K, 'work_root_status', return_value={'ready': True}), \
                patch.object(PR, 'PRODUCTS', PR.PRODUCTS):
            K.move_work_root(str(new), store)
        self.assertEqual((new / 'products/p1/demo/file.txt').read_text(), 'kept')
        self.assertTrue((new / 'runs/r1').is_dir())
        self.assertEqual(list((old / 'products').iterdir()), [])
        self.assertEqual(json.loads((base / 'setup.json').read_text())['work_root'], str(new.resolve()))

    def move_world(self, base):
        old = base / 'old'
        for name in ('a', 'b'):
            (old / 'products' / name).mkdir(parents=True)
            (old / 'products' / name / 'file.txt').write_text(name)
        return old, [patch.object(K, 'work_root', return_value=old), patch.object(K, 'lock_work_root',
                side_effect=lambda root: [(root / n).mkdir(parents=True, exist_ok=True) for n in ('runs', 'products')]),
                patch.dict(K.APP, {'SETUP_FILE': base / 'setup.json', 'load_setup': lambda: {}}), patch.object(K, 'reload_app'),
                patch.object(K, 'work_root_status', return_value={'ready': True}), patch.object(PR, 'PRODUCTS', PR.PRODUCTS)]

    def test_a_failed_move_puts_everything_back(self):
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        self.addCleanup(shutil.rmtree, base, True)
        old, world = self.move_world(base)
        real, calls = K.os.rename, []
        def flaky(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError('disk full')
            return real(source, target)
        for item in world:
            item.start()
        try:
            with patch.object(K.os, 'rename', flaky), self.assertRaises(OSError):
                K.move_work_root(str(base / 'new'), Store(base / 'state.sqlite3'))
        finally:
            for item in reversed(world):
                item.stop()
        self.assertEqual(sorted(p.name for p in (old / 'products').iterdir()), ['a', 'b'])   # nothing split
        self.assertFalse((base / 'setup.json').exists())

    def test_a_move_whose_new_setting_cannot_be_saved_is_undone(self):
        # CODEX_REVIEW_3 F6: the renames were done, then saving the new folder failed, and nothing put them back.
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        self.addCleanup(shutil.rmtree, base, True)
        old, world = self.move_world(base)
        real = K.atomic_json
        def flaky(path, value):
            if Path(path) == base / 'setup.json':
                raise OSError('disk full')
            return real(path, value)
        for item in world:
            item.start()
        try:
            with patch.object(K, 'atomic_json', flaky), self.assertRaises(OSError):
                K.move_work_root(str(base / 'new'), Store(base / 'state.sqlite3'))
        finally:
            for item in reversed(world):
                item.stop()
        self.assertEqual(sorted(p.name for p in (old / 'products').iterdir()), ['a', 'b'])   # back where they were
        self.assertFalse((base / 'work_root_move.json').exists())

    def test_a_reload_failure_after_publication_keeps_the_new_folder_and_recovery_journal(self):
        # Once setup.json points at the new folder, rolling files back would make that setting invalid.
        # Keep the published files and journal until runtime activation can finish, on either drive path.
        from contextlib import ExitStack
        for same_drive in (True, False):
            with self.subTest(same_drive=same_drive), tempfile.TemporaryDirectory(
                    prefix='move_publish_', dir=ROOT / 'prototype') as temporary:
                base = Path(temporary)
                old, world = self.move_world(base)
                new = base / 'new'
                setup = base / 'setup.json'
                setup.write_text(json.dumps({'work_root': str(old)}), encoding='utf-8')
                with ExitStack() as stack:
                    for item in world:
                        stack.enter_context(item)
                    stack.enter_context(patch.object(K, 'check_work_root', return_value=new))
                    stack.enter_context(patch.object(K, 'same_drive', return_value=same_drive))
                    stack.enter_context(patch.dict(K.APP, {'load_setup': lambda: json.loads(setup.read_text())}))
                    with patch.object(K, 'reload_app', side_effect=OSError('synthetic runtime reload failure')):
                        with self.assertRaisesRegex(OSError, 'synthetic runtime reload failure'):
                            K.move_work_root(str(new), Store(base / 'state.sqlite3'))
                    self.assertEqual(json.loads(setup.read_text())['work_root'], str(new))
                    self.assertEqual((new / 'products/a/file.txt').read_text(), 'a')
                    self.assertTrue((base / 'work_root_move.json').exists())
                    journal = (base / 'work_root_move.json').read_bytes()
                    with patch.object(K, 'reload_app', side_effect=OSError('synthetic recovery activation failure')):
                        with self.assertRaisesRegex(OSError, 'synthetic recovery activation failure'):
                            K.move_work_root(str(base / 'third'), Store(base / 'state.sqlite3'))
                    self.assertEqual((base / 'work_root_move.json').read_bytes(), journal)
                    self.assertFalse((base / 'third').exists())
                    with patch.object(K, 'reload_app') as reload:
                        self.assertEqual(K.recover_work_root_move(), 'FINISHED')
                        reload.assert_called_once()
                    self.assertFalse((base / 'work_root_move.json').exists())
                    self.assertFalse((old / 'products/a').exists())

    def test_an_interrupted_move_is_finished_or_undone_on_the_next_start(self):
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        self.addCleanup(lambda: K.remove_tree(base))
        old, new = base / 'old', base / 'new'
        def crashed(published, same_drive):
            K.remove_tree(base) if base.exists() else None
            (new / 'products/a').mkdir(parents=True)
            (new / 'products/a/file.txt').write_text('a')
            (old / 'products').mkdir(parents=True)
            if not same_drive:                                       # a copy: the original is still there
                (old / 'products/a').mkdir()
            (base / 'setup.json').write_text(json.dumps({'work_root': str(new if published else old)}))
            (base / 'work_root_move.json').write_text(json.dumps({'old': str(old), 'new': str(new), 'same_drive': same_drive,
                'moves': [[str(old / 'products/a'), str(new / 'products/a')]]}))
        cases = ((False, True, 'UNDONE', old), (True, True, 'FINISHED', new), (False, False, 'UNDONE', old), (True, False, 'FINISHED', new))
        for published, same_drive, outcome, home in cases:
            with self.subTest(published=published, same_drive=same_drive):
                crashed(published, same_drive)
                with patch.dict(K.APP, {'SETUP_FILE': base / 'setup.json',
                                        'load_setup': lambda: json.loads((base / 'setup.json').read_text())}), \
                        patch.object(K, 'reload_app'), patch.object(PR, 'PRODUCTS', PR.PRODUCTS):
                    self.assertEqual(K.recover_work_root_move(), outcome)
                    self.assertIsNone(K.recover_work_root_move())       # the journal is gone
                self.assertTrue((home / 'products/a').is_dir())
                other = new if home == old else old
                self.assertFalse((other / 'products/a').exists())     # never split between the two folders

    def test_a_failed_copy_cleanup_keeps_the_journal_until_recovery_finishes(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory(prefix='move_cleanup_', dir=ROOT / 'prototype') as temporary:
            base = Path(temporary)
            old, world = self.move_world(base)
            new, setup = base / 'new', base / 'setup.json'
            setup.write_text(json.dumps({'work_root': str(old)}), encoding='utf-8')
            real_copy, copies = K.shutil.copytree, []
            def failed_copy(source, target, **kwargs):
                copies.append(str(source))
                real_copy(source, target, **kwargs)
                if len(copies) == 2:
                    raise OSError('synthetic copy failure')
            with ExitStack() as stack:
                for item in world:
                    stack.enter_context(item)
                stack.enter_context(patch.object(K, 'check_work_root', return_value=new))
                stack.enter_context(patch.object(K, 'same_drive', return_value=False))
                stack.enter_context(patch.dict(K.APP, {'load_setup': lambda: json.loads(setup.read_text())}))
                with patch.object(K, 'remove_tree', side_effect=OSError('synthetic sharing violation')):
                    with patch.object(K.shutil, 'copytree', failed_copy), self.assertRaisesRegex(OSError, 'synthetic copy failure'):
                        K.move_work_root(str(new), Store(base / 'state.sqlite3'))
                    self.assertTrue((base / 'work_root_move.json').exists())
                    self.assertEqual(K.recover_work_root_move(), 'INCOMPLETE')
                self.assertEqual(json.loads(setup.read_text())['work_root'], str(old))
                self.assertEqual((old / 'products/a/file.txt').read_text(), 'a')
                self.assertEqual(K.recover_work_root_move(), 'UNDONE')
                self.assertFalse((base / 'work_root_move.json').exists())
                self.assertEqual(list((new / 'products').iterdir()), [])

    def test_a_move_to_another_drive_copies_checks_switches_then_deletes_read_only_git_files(self):
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        self.addCleanup(lambda: K.remove_tree(base))
        old, world = self.move_world(base)
        objects = old / 'products/a/.git/objects'
        objects.mkdir(parents=True)
        (objects / 'pack').write_text('git object')
        (objects / 'pack').chmod(0o444)   # Git's object files are read-only on Windows
        for item in world:
            item.start()
        try:
            with patch.object(K, 'same_drive', return_value=False):   # as if D:\\
                result = K.move_work_root(str(base / 'new'), Store(base / 'state.sqlite3'))
        finally:
            for item in reversed(world):
                item.stop()
        self.assertEqual((base / 'new/products/a/.git/objects/pack').read_text(), 'git object')
        self.assertEqual(sorted(p.name for p in (base / 'new/products').iterdir()), ['a', 'b'])
        self.assertEqual(list((old / 'products').iterdir()), [])           # old copies deleted, read-only ones too
        self.assertEqual(result['left_in_old_folder'], [])
        self.assertEqual(json.loads((base / 'setup.json').read_text())['work_root'], str((base / 'new').resolve()))

    def test_a_failed_copy_to_another_drive_leaves_the_originals_in_use(self):
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        self.addCleanup(lambda: K.remove_tree(base))
        old, world = self.move_world(base)
        real, calls = K.shutil.copytree, []
        def flaky(source, target, **kwargs):
            calls.append(source)
            if len(calls) == 2:
                real(source, target, **kwargs)
                raise OSError('disk full part-way')   # a partial copy exists
            return real(source, target, **kwargs)
        for item in world:
            item.start()
        try:
            with patch.object(K, 'same_drive', return_value=False), patch.object(K.shutil, 'copytree', flaky), \
                    self.assertRaises(OSError):
                K.move_work_root(str(base / 'new'), Store(base / 'state.sqlite3'))
        finally:
            for item in reversed(world):
                item.stop()
        self.assertEqual(sorted(p.name for p in (old / 'products').iterdir()), ['a', 'b'])   # untouched
        self.assertEqual(list((base / 'new/products').iterdir()), [])                       # copies removed
        self.assertFalse((base / 'setup.json').exists())                                    # still the old folder

    def test_a_name_clash_is_found_before_anything_moves(self):
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        self.addCleanup(shutil.rmtree, base, True)
        old, world = self.move_world(base)
        (base / 'new/products/b').mkdir(parents=True)
        for item in world:
            item.start()
        try:
            with patch.object(K, 'check_work_root', return_value=(base / 'new').resolve()), self.assertRaisesRegex(RuntimeError, 'ALREADY_HAS'):
                K.move_work_root(str(base / 'new'), Store(base / 'state.sqlite3'))
        finally:
            for item in reversed(world):
                item.stop()
        self.assertEqual(sorted(p.name for p in (old / 'products').iterdir()), ['a', 'b'])

    def test_the_folder_cannot_move_while_work_is_waiting(self):
        base = Path('C:/') / ('dani_move_test_' + uuid.uuid4().hex[:8])
        self.addCleanup(shutil.rmtree, base, True)
        base.mkdir()
        store = Store(base / 'state.sqlite3')
        store.add('offline_tests', 'Self-test', {})
        with patch.object(K, 'work_root', return_value=base / 'old'), self.assertRaisesRegex(ValueError, 'WAIT_UNTIL'):
            K.move_work_root(str(base / 'new'), store)


class EnvironmentSourceTests(unittest.TestCase):
    def test_the_message_says_where_each_variable_comes_from(self):
        with patch.dict(K.os.environ, {'ANTHROPIC_BASE_URL': 'http://proxy', 'OPENAI_API_KEY': 'k'}), \
                patch.object(K, 'windows_setting', side_effect=lambda name: name == 'OPENAI_API_KEY'), \
                patch.dict(K.APP, {'CODEX': '', 'CLAUDE': ''}):
            status = K.sign_in_status()
        self.assertEqual(status['environment_in_windows'], ['OPENAI_API_KEY'])
        self.assertIn('ANTHROPIC_BASE_URL', status['environment_inherited'])



class ReviewerProfileStateTests(unittest.TestCase):
    def test_a_new_claude_code_makes_the_profile_stale(self):
        source = TF.reviewer_source()
        K._profile_checks.clear()
        self.addCleanup(K._profile_checks.clear)
        with patch.object(C, 'reviewer_source', return_value=source):
            self.assertEqual(K.reviewer_profile_state(), 'current')
            # runpy's functions read their own module globals, which APP is a copy of.
            new = {'claude': {'path': sys.executable, 'version': 'new (Claude Code)', 'sha256': 'new'}}
            with patch.dict(K.APP, {'CLAUDE': sys.executable}), patch.dict(K.APP['TOOLS'], new), \
                    patch.dict(K.APP['verified_startup_gate'].__globals__, {'CLAUDE': sys.executable}):
                self.assertEqual(K.reviewer_profile_state(), 'stale')
        with patch.object(C, 'reviewer_source', side_effect=RuntimeError('CONNECT_CLAUDE_REVIEWER_CHECK_REQUIRED')):
            self.assertEqual(K.reviewer_profile_state(), 'missing')


class SharedGitTests(unittest.TestCase):
    def test_every_project_git_call_uses_the_confirmed_git(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv[0])
            return Done(b'')
        with patch.dict(C.APP, {'GIT': r'D:\\Tools\\Git\\cmd\\git.exe'}), patch('subprocess.run', run):
            P.git(Path(tempfile.gettempdir()), 'status')
            try:
                P.validate_config('[core]\n')
            except ValueError:
                pass
        self.assertEqual(set(calls), {r'D:\\Tools\\Git\\cmd\\git.exe'})

    def test_git_inside_the_user_profile_is_refused(self):
        self.assertIn('user folder', K.problem('git', Path.home() / 'AppData/Local/Programs/Git/cmd/git.exe', 'git version 2.45.1'))


class PlannerModelTests(unittest.TestCase):
    def test_planner_uses_the_approved_verified_claude_model_and_effort(self):
        run = TF.toy_run()
        settings = {**M.DEFAULTS, 'claude_model': 'opus', 'claude_effort': 'high'}
        with patch.object(M, 'verified_models', lambda role: {'codex': {'gpt-6-astra'}, 'claude': {'opus'}}[role]):
            argv = PR.planner_argv(run, settings)
        flags = dict(zip(argv, argv[1:]))
        self.assertEqual((flags['--model'], flags['--effort']), ('opus', 'high'))
        self.assertEqual(argv[:argv.index('--model')], C.APP['commands'](run)['claude'][:argv.index('--model')])
        with patch.object(M, 'verified_models', lambda role: {'codex': {'gpt-6-astra'}, 'claude': {'sonnet'}}[role]):
            with self.assertRaisesRegex(ValueError, 'MODEL_NOT_SUBSCRIPTION_VERIFIED'):
                PR.planner_argv(run, settings)   # evidence is re-checked when the build starts


if __name__ == '__main__':
    unittest.main()
