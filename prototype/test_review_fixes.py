"""Regressions for the 2026-10-20 independent review fixes; disposable Git fixtures, zero model calls."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dani_agent import projects as P, workspaces as W, project_workflow as F, model_settings as M
from dani_agent.presentation import acceptance_text
from dani_agent.state import POLICIES
from test_phase2_projects import Agents


def repository(files, config_extra=None, worktree_config=None):
    folder = ROOT / 'data' / ('review_fix_' + uuid.uuid4().hex[:8])
    folder.mkdir()
    for name, text in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding='utf-8')
    P.git(folder, 'init', '--template=')
    P.git(folder, 'add', '--all')
    P.git(folder, 'commit', '-m', 'baseline')
    if config_extra:
        with (folder / '.git/config').open('a', encoding='utf-8') as stream:
            stream.write(config_extra)
    if worktree_config is not None:
        (folder / '.git/config.worktree').write_text(worktree_config, encoding='utf-8')
    return folder



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class GitConfigTests(unittest.TestCase):
    def test_worktree_extension_is_accepted(self):
        # Real repositories that ever used `git worktree` carry this flag (bolighub, sharehub_website).
        folder = repository({'a.py': 'pass\n'}, '[extensions]\n\tworktreeConfig = true\n')
        self.assertTrue(P.discover(folder)['git'])

    def test_config_worktree_file_uses_the_same_allowlist(self):
        folder = repository({'a.py': 'pass\n'}, '[extensions]\n\tworktreeConfig = true\n',
                            '[core]\n\tfsmonitor = C:/evil.exe\n')
        with self.assertRaises(RuntimeError) as caught:
            P.discover(folder)
        self.assertEqual(str(caught.exception), 'PROJECT_GIT_CONFIG_REQUIRES_REVIEW')
        safe = repository({'a.py': 'pass\n'}, '[extensions]\n\tworktreeConfig = true\n', '[core]\n\tsymlinks = false\n')
        self.assertTrue(P.discover(safe)['git'])

    def test_other_unknown_sections_still_rejected(self):
        folder = repository({'a.py': 'pass\n'}, '[credential]\n\thelper = store\n')
        with self.assertRaises(RuntimeError):
            P.discover(folder)


    def test_dubious_ownership_is_reported_clearly_without_stderr(self):
        failed = subprocess.CompletedProcess([], 128, b'', b"fatal: detected dubious ownership in repository at 'C:/secret/path'")
        with patch('subprocess.run', return_value=failed), self.assertRaises(RuntimeError) as caught:
            P.git(ROOT, 'rev-parse', 'HEAD')
        self.assertEqual(str(caught.exception), 'PROJECT_GIT_OWNED_BY_ANOTHER_ACCOUNT_FIX_OWNERSHIP')


class SecretTemplateTests(unittest.TestCase):
    def test_env_example_is_excluded_but_does_not_block(self):
        summary = P.discover(repository({'.env.example': 'API_KEY=\n', 'test_a.py': 'import unittest\n'}))
        self.assertIn('.env.example', summary['excluded'])
        self.assertTrue(summary['eligible'])

    def test_real_env_and_keys_still_block(self):
        for name in ('.env', '.env.local', 'certs/server.key', 'config/.env.production'):
            with self.subTest(name=name):
                summary = P.discover(repository({name: 'x\n', 'test_a.py': 'import unittest\n'}))
                self.assertFalse(summary['eligible'])
                self.assertIn(name, summary['excluded'])


class EligibilityTests(unittest.TestCase):
    def test_static_site_without_supported_driver_is_not_eligible(self):
        summary = P.discover(repository({'index.html': '<p>hi</p>\n'}))
        self.assertFalse(summary['eligible'])
        self.assertTrue(any('No supported test driver' in w for w in summary['warnings']))

    def test_baseline_test_counts_per_driver(self):
        summary = P.discover(repository({'package.json': '{"name":"x","type":"module"}', 'lib.mjs': 'export const x=1;\n',
            'lib.test.mjs': 'import "node:test";\n', 'test-legacy.js': '// plain script\n'}))
        node = next(o for o in summary['test_options'] if o['id'] == 'node-test')
        self.assertEqual(node['baseline_test_files'], 1)   # test-legacy.js is not a node:test file for Dani
        self.assertTrue(summary['eligible'])


class FirstTestsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = P.discover(repository({'package.json': '{"name":"x","type":"module"}',
                                             'lib.mjs': 'export function add(a,b){return a-b;}\n'}))

    def test_no_tests_requires_a_test_file_in_scope(self):
        self.assertEqual(next(o for o in self.summary['test_options'] if o['id'] == 'node-test')['baseline_test_files'], 0)
        with self.assertRaises(ValueError) as caught:
            W.prepare(self.summary, 'Fix add', ['lib.mjs'], 'node-test', dict(M.DEFAULTS))
        self.assertEqual(str(caught.exception), 'PROJECT_HAS_NO_TESTS_ADD_A_TEST_FILE_TO_EDITABLE_PATHS')

    def test_task_added_tests_are_run_reviewed_and_disclosed(self):
        run = W.prepare(self.summary, 'Fix add and add tests', ['lib.mjs', 'lib.test.mjs'], 'node-test', dict(M.DEFAULTS))
        plan = W.load(run)
        self.assertEqual((plan['baseline_test_files'], plan['tests_added_by_task']), (0, ['lib.test.mjs']))
        self.assertIn('./lib.test.mjs', W.commands(run)['tests'])
        report = F.execute(run, runner=Agents(run), simulated=True)
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['tests_written_by_developer'], ['lib.test.mjs'])
        handoff = json.loads((run / 'artifacts/cycle_0/handoff.json').read_text())
        self.assertEqual(handoff['tests_written_by_developer'], ['lib.test.mjs'])
        text = acceptance_text({'state': 'awaiting_acceptance', 'result': report})
        self.assertIn('Tests written by the developer agent', text)


class WorkRootAndShellTests(unittest.TestCase):
    """2026-10-21: workspaces under the sandbox-denied profile left Codex's PowerShell stranded at C:\\."""
    APP = W.APP

    def test_codex_never_gets_another_programs_runtime_on_its_path(self):
        """2026-10-22 Pocket Ledger: the Codex desktop app's bundled PowerShell sat first on PATH; Codex wrapped every
        shell command in it, the sandbox denies the user profile, and each command failed."""
        run = ROOT / 'data' / ('codex_env_' + uuid.uuid4().hex[:8])
        (run / 'artifacts').mkdir(parents=True)
        (run / 'control/empty_git').mkdir(parents=True)
        self.addCleanup(__import__('shutil').rmtree, run, True)
        home = str(Path.home())
        frozen = dict(self.APP['OLD_GATE']['child_env'](), PATH=';'.join([
            home + r'\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell',
            home + r'\.codex\tmp\arg0-windows\codex-arg0abc', home, '',
            r'C:\Windows\System32', r'C:\Windows\System32\WindowsPowerShell\v1.0', r'C:\Program Files\Git\cmd']))
        frozen = {k: v for k, v in frozen.items() if k.lower() in ('systemroot', 'windir', 'path', 'userprofile', 'temp', 'tmp',
                                                                    'localappdata', 'comspec', 'pathext')}
        (run / 'artifacts/verified_startup_environment.json').write_text(json.dumps(frozen))
        codex = self.APP['codex_env'](run)['PATH'].split(';')
        self.assertEqual(codex, [r'C:\Windows\System32', r'C:\Windows\System32\WindowsPowerShell\v1.0', r'C:\Program Files\Git\cmd'])
        self.assertIn('codex-runtimes', self.APP['env'](run)['PATH'])   # Claude's verified environment is unchanged

    def test_project_runs_live_in_private_work_root_and_are_approved(self):
        summary = P.discover(repository({'test_a.py': 'import unittest\n', 'a.py': 'x = 1\n'}))
        run = W.prepare(summary, 'Task', ['a.py'], 'python-unittest', dict(M.DEFAULTS))
        self.assertEqual(run.parent, self.APP['WORK_RUNS'].resolve())
        self.assertEqual(self.APP['approved_run'](run), run)
        with self.assertRaises(ValueError):
            self.APP['approved_run'](ROOT / 'data')

    def test_run_location_is_never_arbitrary_or_implicitly_created(self):
        prepare = self.APP['prepare']
        with self.assertRaisesRegex(ValueError, 'UNAPPROVED_RUN_LOCATION'):
            prepare(template='clamp-v1', parent=ROOT / 'data')
        missing = Path(r'C:\DaniAgentWork') / ('absent_' + uuid.uuid4().hex) / 'runs'
        with patch.dict(prepare.__globals__, {'WORK_RUNS': missing}):
            with self.assertRaisesRegex(RuntimeError, 'PRIVATE_WORK_ROOT_MISSING'):
                prepare(template='clamp-v1', parent=missing)
        self.assertFalse(missing.parent.exists())

    def test_codex_shell_path_includes_powershell(self):
        toy = self.APP['commands'](TF.toy_run())['codex']
        config = __import__('tomllib').loads('\n'.join(toy[2:toy.index('exec'):2]))
        self.assertIn(r'C:\Windows\System32\WindowsPowerShell\v1.0', config['shell_environment_policy']['set']['PATH'].split(';'))
        summary = P.discover(repository({'test_a.py': 'import unittest\n', 'a.py': 'x = 1\n'}))
        run = W.prepare(summary, 'Task', ['a.py'], 'python-unittest', dict(M.DEFAULTS))
        dev = W.commands(run)['codex']
        self.assertTrue(any(a.startswith('shell_environment_policy.set=') and 'WindowsPowerShell' in a for a in dev))

    def test_shell_probe_requires_powershell_in_workspace(self):
        summary = P.discover(repository({'test_a.py': 'import unittest\n', 'a.py': 'x = 1\n'}))
        run = W.prepare(summary, 'Task', ['a.py'], 'python-unittest', dict(M.DEFAULTS))
        plan, seen = W.load(run), []
        def runner(stdout):
            def call(spec, folder, **kwargs):
                seen.append(spec)
                return {'exit_code': 0, 'stdout': stdout}
            return call
        result = F.shell_probe(run, plan, runner('DANI_SHELL_OK\n'), run / 'artifacts', lambda: None)
        self.assertTrue(result['verified'])
        spec = seen[-1]
        self.assertEqual(spec['argv'][spec['argv'].index('--') + 1], 'powershell.exe')   # resolved like Codex does
        self.assertEqual(spec['env']['PATH'], self.APP['SHELL_PATH'])
        self.assertIn(str(run / 'repo'), spec['argv'][-1])
        with self.assertRaisesRegex(RuntimeError, 'PROJECT_AGENT_SHELL_UNUSABLE_IN_WORKSPACE'):
            F.shell_probe(run, plan, runner('DANI_SHELL_WRONG_LOCATION\n'), run / 'artifacts', lambda: None)


class NoTimeLimitTests(unittest.TestCase):
    """2026-10-23 StockPilot: a hard step at high effort was cut off by the 240-second limit. Product work has no time
    limit now; only a session with no sign of work for the idle period is stopped (STALLED)."""
    PROCESS = W.APP['PROCESS']

    def folder(self):
        folder = ROOT / 'data' / ('no_limit_' + uuid.uuid4().hex[:8])
        folder.mkdir()
        self.addCleanup(__import__('shutil').rmtree, folder, True)
        return folder

    def test_a_session_that_keeps_working_is_never_stopped(self):
        folder = self.folder()
        code = 'import time\nfor i in range(20):\n    print("working", i, flush=True)\n    time.sleep(0.4)\n'
        out = self.PROCESS['run']({'argv': [sys.executable, '-I', '-B', '-c', code], 'cwd': str(folder),
            'env': {'SystemRoot': r'C:\Windows'}, 'kind': 'plain'}, folder, timeout=None, idle=5)   # runs 8 s, idle 5 s
        self.assertEqual(out['exit_code'], 0)
        self.assertIn('working 19', out['stdout'])

    def test_a_session_with_no_sign_of_work_is_stopped_as_stuck(self):
        folder = self.folder()
        with self.assertRaisesRegex(RuntimeError, 'STALLED'):
            self.PROCESS['run']({'argv': [sys.executable, '-I', '-B', '-c', 'import time; time.sleep(60)'], 'cwd': str(folder),
                'env': {'SystemRoot': r'C:\Windows'}, 'kind': 'plain'}, folder, timeout=None, idle=5)
        containment = json.loads(next(folder.glob('*.containment.json')).read_text())
        self.assertEqual(containment['termination_reason'], 'STALLED')
        self.assertTrue(containment['job_empty_after_termination'])
        self.assertLess(containment['elapsed_seconds'], 30)


class SelfTestNoTimeLimitTests(unittest.TestCase):
    """2026-10-23: the self-test was cut off by a fixed 25-minute limit; it now stops only when it prints nothing."""

    def test_the_self_test_runs_while_it_prints_and_stops_when_it_hangs(self):
        from dani_agent import coordinator as C
        folder = ROOT / 'data' / ('suite_' + uuid.uuid4().hex[:8])
        folder.mkdir()
        self.addCleanup(__import__('shutil').rmtree, folder, True)
        busy = 'import time\nfor i in range(16):\n    print(".", end="", flush=True)\n    time.sleep(0.5)\n'
        self.assertEqual(C.run_while_active([sys.executable, '-I', '-B', '-c', busy], folder / 'a.log', 5), 0)   # 8 s, idle 5 s
        before = C.job_activity(folder)
        # A stuck run is stopped with everything it started (the self-test must never leave processes behind).
        stuck = ('import subprocess, sys, time; subprocess.Popen([sys.executable, "-I", "-B", "-c", '
                 '"import time; time.sleep(120)"]); print("child", flush=True); time.sleep(120)')
        with self.assertRaisesRegex(RuntimeError, 'OFFLINE_TESTS_STALLED'):
            C.run_while_active([sys.executable, '-I', '-B', '-c', stuck], folder / 'b.log', 5)
        left = subprocess.run(['powershell', '-NoProfile', '-Command',
            '(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*time.sleep(120)*" '
            '-and $_.ProcessId -ne $PID }).Count'], capture_output=True, text=True, timeout=120).stdout.strip()
        self.assertEqual(left, '0', 'the stuck run left processes behind')
        self.assertNotEqual(C.job_activity(folder), before)             # a log that grew counts as progress


class CodexReview3Tests(unittest.TestCase):
    """Findings of CODEX_REVIEW_3.md (2026-10-25)."""
    PROCESS = W.APP['PROCESS']

    def folder(self):
        folder = ROOT / 'data' / ('review3_' + uuid.uuid4().hex[:8])
        folder.mkdir()
        self.addCleanup(__import__('shutil').rmtree, folder, True)
        return folder

    def startup(self, code, **settings):
        folder = self.folder()
        return self.PROCESS['run']({'argv': [sys.executable, '-I', '-B', '-c', code], 'cwd': str(folder),
            'env': {'SystemRoot': r'C:\Windows'}, 'kind': 'claude-inventory', 'startup_idle_seconds': 1.5, **settings},
            folder, timeout=None, idle=30)

    def test_f7_a_claude_cli_that_is_visibly_starting_is_given_time_for_its_inventory(self):
        busy = ('import json, time\nfor i in range(8):\n    print(json.dumps({"type": "system", "subtype": "status"}), flush=True)\n'
                '    time.sleep(0.5)\nprint(json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}), flush=True)\n'
                'time.sleep(5)\n')
        out = self.startup(busy)                                  # 4 s of startup output, a 1.5 s silence window
        self.assertEqual(out['initialization'], {'tools': [], 'mcp_servers_count': 0}, out.get('error'))
        self.assertFalse(out['prompt_sent'])                      # still: no prompt before the inventory
        silent = self.startup('import time; time.sleep(20)')
        self.assertEqual(silent['error'], 'NO_PREPROMPT_TOOL_INVENTORY')
        self.assertFalse(silent['prompt_sent'])

    def test_noisy_startup_without_inventory_still_stops_and_cleans_up(self):
        code = 'import sys,time\nwhile True:\n print("starting", file=sys.stderr, flush=True)\n time.sleep(.1)\n'
        out = self.startup(code, startup_max_seconds=2)
        self.assertEqual(out['error'], 'NO_PREPROMPT_TOOL_INVENTORY')
        self.assertFalse(out['prompt_sent'])
        self.assertTrue(out['containment']['job_empty_after_termination'])

    def test_f2_long_pauses_quota_and_program_updates_end_the_job_instead_of_sleeping_in_it(self):
        from dani_agent import products as PR
        self.assertEqual(PR.next_wait(1, 'PROJECT_TESTS_FAILED', False), 0)       # short pauses stay in the job
        self.assertEqual(PR.next_wait(3, 'PROJECT_TESTS_FAILED', False), 120)
        for failures, reason, changed, expected in ((4, 'PROJECT_TESTS_FAILED', False, ('PROJECT_TESTS_FAILED', 300)),
                                                    (1, 'QUOTA_EXHAUSTED', False, ('QUOTA_EXHAUSTED', PR.QUOTA_WAIT)),
                                                    (1, 'X', True, ('PROGRAM_UPDATED_DURING_BUILD', 0))):
            with self.subTest(reason=reason), self.assertRaises(PR.RetryLater) as raised:
                PR.next_wait(failures, reason, changed)
            self.assertEqual((raised.exception.reason, raised.exception.seconds), expected)

    def test_f9_no_test_class_hides_another_one(self):
        import ast
        for path in sorted(Path(__file__).parent.glob('test_*.py')):
            names = [n.name for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n, ast.ClassDef)]
            duplicates = sorted({n for n in names if names.count(n) > 1})
            self.assertEqual(duplicates, [], path.name + ': a second class with the same name hides the first from discovery')


class ContextFitsTests(unittest.TestCase):
    """2026-10-24: StockPilot step 4 stopped with PROJECT_REVIEW_CONTEXT_LIMIT once the product grew; the review
    and correction context now fit themselves to the space instead of stopping the product."""

    def test_the_review_reads_the_step_files_first_and_names_what_did_not_fit(self):
        run = ROOT / 'data' / ('context_' + uuid.uuid4().hex[:8])
        (run / 'repo/lib').mkdir(parents=True)
        self.addCleanup(__import__('shutil').rmtree, run, True)
        (run / 'repo/app.js').write_text('a' * 100000, encoding='utf-8')
        (run / 'repo/lib/big.test.mjs').write_text('t' * 100000, encoding='utf-8')
        (run / 'repo/lib/small.test.mjs').write_text('ok', encoding='utf-8')
        (run / 'repo/lib/data.test.bin').write_bytes(b'\0binary')
        plan = {'editable_paths': ['app.js'], 'project': {'files': [{'path': 'lib/big.test.mjs'}, {'path': 'lib/small.test.mjs'},
                                                                    {'path': 'lib/data.test.bin'}]}}
        base = {'diff': 'd' * (F.REVIEW_CONTEXT_CHARS - 150000)}
        sources, omitted = F.review_sources(run, plan, base)
        self.assertEqual(sorted(sources), ['app.js', 'lib/small.test.mjs'])
        self.assertEqual(sorted(omitted), ['lib/big.test.mjs', 'lib/data.test.bin'])
        self.assertLessEqual(len(json.dumps({**base, 'sources': sources})), F.REVIEW_CONTEXT_CHARS + 100)

    def test_long_correction_evidence_is_shortened_to_fit_the_command_line(self):
        argv = ['codex.exe', 'exec', 'p' * 15000]
        findings = [{'severity': severity, 'file': 'app.js', 'line': 1, 'explanation': severity + ' ' + 'x' * 1500}
                    for severity in ['low'] * 10 + ['high'] * 5 + ['medium'] * 5]
        feedback = {'candidate_sha': 'abc', 'test_evidence': {'exit_code': 1, 'output': 'start ' + 'o' * 12000 + ' end'},
                    'review': {'verdict': 'changes_requested', 'findings': findings}}
        text = F.fit_feedback(argv, feedback)
        W.check_command_size(argv[:-1] + [argv[-1] + text])                 # fits: no exception
        value = json.loads(text[len(F.FEEDBACK_INTRO):])
        self.assertIn('shortened by Dani Agent', value['test_evidence']['output'])
        self.assertTrue(value['test_evidence']['output'].startswith('start') and value['test_evidence']['output'].endswith('end'))
        kept = [f['severity'] for f in value['review']['findings']]
        self.assertEqual(kept[:5], ['high'] * 5)                             # the most severe are kept first
        self.assertEqual(len(feedback['review']['findings']), 20)            # the saved evidence is unchanged


class SandboxTempTests(unittest.TestCase):
    """2026-10-21: MXC swaps TEMP for an unwritable per-launch folder; all pilot project tests failed in setup."""

    def prepared(self, files, editable, test_id):
        summary = P.discover(repository(files))
        return W.prepare(summary, 'Task', editable, test_id, dict(M.DEFAULTS))

    def test_node_tests_preload_runtime_temp_before_test_files(self):
        run = self.prepared({'package.json': '{"type":"module"}', 'a.mjs': 'export const a = 1;\n'},
                            ['a.mjs', 'a.test.mjs'], 'node-test')
        argv = W.commands(run)['tests']
        preload = next(a for a in argv if a.startswith('--import=data:text/javascript,'))
        self.assertLess(argv.index(preload), argv.index('--test'))
        source = __import__('urllib.parse').parse.unquote(preload.split(',', 1)[1])
        self.assertIn('process.env.TEMP = ' + json.dumps(str(run / 'runtime')), source)
        self.assertIn('process.env.TMP = ' + json.dumps(str(run / 'runtime')), source)

    def test_python_runner_redirects_tempfile_to_runtime(self):
        import os, tempfile
        from dani_agent import test_runner
        root = ROOT / 'data' / ('runner_' + uuid.uuid4().hex[:8]) / 'repo'
        (root.parent / 'runtime').mkdir(parents=True)
        with patch.dict(os.environ):
            try:
                test_runner.use_runtime_temp(root)
                self.assertEqual(Path(tempfile.gettempdir()), root.parent / 'runtime')
            finally:
                tempfile.tempdir = None

    def test_preflight_temp_probe_uses_the_real_mechanism_and_fails_closed(self):
        cases = ((({'test_a.py': 'import unittest\n', 'a.py': 'x = 1\n'}, ['a.py'], 'python-unittest'), 'use_runtime_temp'),
                 (({'package.json': '{"type":"module"}', 'a.mjs': 'export const a = 1;\n'}, ['a.mjs', 'a.test.mjs'], 'node-test'),
                  '--import=data:text/javascript,'))
        for args, marker in cases:
            run = self.prepared(*args)
            plan, seen = W.load(run), []
            def runner(stdout):
                def call(spec, folder, **kwargs):
                    seen.append(spec)
                    return {'exit_code': 0, 'stdout': stdout}
                return call
            self.assertTrue(F.temp_probe(run, plan, runner('DANI_TEMP_OK\n'), run / 'artifacts', lambda: None)['verified'])
            self.assertTrue(any(marker in a for a in seen[-1]['argv']))
            self.assertIn('projecttest', seen[-1]['argv'])
            with self.assertRaisesRegex(RuntimeError, 'PROJECT_TEST_TEMP_FOLDER_UNUSABLE_IN_SANDBOX'):
                F.temp_probe(run, plan, runner('EPERM\n'), run / 'artifacts', lambda: None)


class LimitTests(unittest.TestCase):
    def test_session_cap_raised_but_still_bounded(self):
        self.assertEqual(M.validate({**M.DEFAULTS, 'seconds_per_session': 300})['seconds_per_session'], 300)
        with self.assertRaises(ValueError):
            M.validate({**M.DEFAULTS, 'seconds_per_session': 301})

    def test_outer_deadline_covers_every_approved_session_and_test(self):
        settings = {**M.DEFAULTS, 'seconds_per_session': 300}
        floor = 6 * 300 + 3 * M.TEST_SECONDS
        self.assertGreater(M.workflow_ceiling(settings), floor)
        self.assertLess(M.workflow_ceiling(settings), floor + 600)

    def test_milestone_review_budget_matches_policy(self):
        self.assertIsNone(POLICIES['milestone_review']['seconds'])
        self.assertEqual(POLICIES['milestone_review']['claude'], 3)

    def test_milestone_review_covers_isolation_core_within_limit(self):
        from dani_agent import milestone_review as R
        sources = R.review_sources()
        for name in ('prototype/orchestrator.py', 'prototype/process.py', 'prototype/diagnostics.py',
                     'phase0_security/final/process_checks.py', 'dani_agent/projects.py', *R.CORE):
            self.assertIn(name, sources)
        batches = R.evidence_batches(sources, R.baseline())   # what the next review would read
        self.assertLessEqual(len(batches), 3)
        for shown in batches:
            self.assertLessEqual(len(json.dumps(shown, sort_keys=True)), R.MAX_BUNDLE_CHARS)


class IncrementalReviewTests(unittest.TestCase):
    """2026-10-22: the full sources outgrew one review; read the changes since the last passed review."""

    def setUp(self):
        from dani_agent import milestone_review as R
        self.R = R
        self.core = {name: 'core ' + name for name in R.CORE}
        self.old = {'dani_agent/a.py': 'a = 1\n', 'dani_agent/b.py': 'b = 1\n', 'prototype/test_a.py': 't = 1\n',
                    'dani_agent/gone.py': 'x\n', **self.core}
        self.base = {'sources': self.old, 'candidate_sha': R.digest(self.old)}

    def test_only_changes_are_sent_and_unchanged_files_are_vouched_for(self):
        new = dict(self.old, **{'dani_agent/b.py': 'b = 2\n', 'prototype/test_a.py': 't = 2\n', 'dani_agent/new.py': 'n = 1\n'})
        del new['dani_agent/gone.py']
        shown = self.R.evidence(new, self.base)
        self.assertEqual(shown['mode'], 'changes_since_last_passed_review')
        self.assertEqual(shown['baseline_candidate_sha'], self.base['candidate_sha'])
        self.assertEqual(sorted(shown['diffs']), ['dani_agent/b.py', 'dani_agent/new.py', 'prototype/test_a.py'])
        self.assertIn('-b = 1', shown['diffs']['dani_agent/b.py'])
        self.assertIn('+b = 2', shown['diffs']['dani_agent/b.py'])
        self.assertEqual(shown['unchanged_files_already_reviewed'], sorted(['dani_agent/a.py', *self.core]))
        self.assertEqual(shown['removed_files'], ['dani_agent/gone.py'])
        self.assertEqual(shown['security_core'], self.core)                       # always whole
        self.assertEqual(sorted(shown['changed_files']), ['dani_agent/b.py', 'dani_agent/new.py'])   # tests: diff only
        self.assertNotIn('sources', shown)
        runner = self.R.evidence(dict(self.old, **{'dani_agent/test_runner.py': 'production code\n'}), self.base)
        self.assertIn('dani_agent/test_runner.py', runner['changed_files'])   # only prototype/test_* are tests

    def test_the_offline_test_fixture_is_reviewed_and_approval_bound(self):
        self.assertIn('prototype/fixture_runs.py', self.R.review_sources())
        self.assertIn('prototype/test_delivery_guards.py', self.R.review_sources())
        from dani_agent import coordinator as C
        before, real = C.fingerprint(), Path.read_bytes
        def changed(path):
            data = real(path)
            return data + b'\n# changed' if path.name == 'fixture_runs.py' else data
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(C.fingerprint(), before)

    def test_without_a_passed_review_the_full_sources_are_sent(self):
        self.assertEqual(self.R.evidence(self.old, None), {'mode': 'full_sources', 'sources': self.old})
        with patch.object(self.R, 'MAX_BUNDLE_CHARS', 10), self.assertRaisesRegex(RuntimeError, 'CONTEXT_LIMIT'):
            self.R.evidence(self.old, None)

    def test_full_text_is_dropped_largest_first_but_diffs_never(self):
        new = dict(self.old, **{'dani_agent/a.py': 'a = 2\n' + 'x' * 4000, 'dani_agent/b.py': 'b = 2\n'})
        whole = len(__import__('json').dumps(self.R.evidence(new, self.base), sort_keys=True))
        with patch.object(self.R, 'MAX_BUNDLE_CHARS', whole - 1000):
            shown = self.R.evidence(new, self.base)
        self.assertEqual(sorted(shown['changed_files']), ['dani_agent/b.py'])   # the large one went first
        self.assertEqual(sorted(shown['diffs']), ['dani_agent/a.py', 'dani_agent/b.py'])
        with patch.object(self.R, 'MAX_BUNDLE_CHARS', 100), self.assertRaisesRegex(RuntimeError, 'CONTEXT_LIMIT'):
            self.R.evidence(new, self.base)

    def test_the_baseline_is_the_newest_passed_untampered_review(self):
        root = ROOT / 'data' / ('baseline_test_' + uuid.uuid4().hex[:8])
        def review(name, sources, verdict='PASS', approved=True, tamper=False):
            artifacts = root / name / 'artifacts'
            artifacts.mkdir(parents=True)
            sha = self.R.digest(sources)
            (artifacts / 'milestone_plan.json').write_text(json.dumps({'sources': dict(sources, extra='x') if tamper else sources,
                'candidate_sha': sha, 'fingerprint': 'f'}))
            (root / name / 'result.json').write_text(json.dumps({'scope': 'LIVE_MILESTONE_REVIEW', 'verdict': verdict,
                'candidate_sha': sha, 'review': {'verdict': 'pass' if verdict == 'PASS' else 'changes_requested', 'findings': []}}))
            if approved:
                (artifacts / 'local_live_approval.json').write_text('{}')
        review('20261020T000000Z_old', {'v': '1'})
        review('20261021T000000Z_good', {'v': '2'})
        review('20261022T000000Z_failed', {'v': '3'}, verdict='FAIL')
        review('20261022T010000Z_unapproved', {'v': '4'}, approved=False)
        review('20261022T020000Z_tampered', {'v': '5'}, tamper=True)
        with patch.object(self.R, 'RUNS', root):
            self.assertEqual(self.R.baseline()['sources'], {'v': '2'})

    def test_later_pass_cannot_erase_material_findings_on_identical_sources(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT / 'prototype') as temporary:
            sources = {'app.py': 'same code'}
            plan = {'sources': sources, 'candidate_sha': self.R.digest(sources), 'fingerprint': 'f'}
            for name, verdict, findings in [('001', 'FAIL', [{'severity': 'medium'}]), ('002', 'PASS', [])]:
                run = Path(temporary) / name
                (run / 'artifacts').mkdir(parents=True)
                (run / 'artifacts/milestone_plan.json').write_text(json.dumps(plan))
                (run / 'artifacts/local_live_approval.json').write_text('{}')
                (run / 'result.json').write_text(json.dumps({'scope': 'LIVE_MILESTONE_REVIEW',
                    'verdict': verdict, 'candidate_sha': plan['candidate_sha'],
                    'review': {'verdict': 'pass' if verdict == 'PASS' else 'changes_requested', 'findings': findings}}))
            with patch.object(self.R, 'RUNS', Path(temporary)), patch.object(self.R, 'fingerprint', return_value='f'), \
                    patch.object(self.R, 'review_sources', return_value=sources):
                self.assertFalse(self.R.passed(Path(temporary) / '002', plan))
                self.assertFalse(self.R.verified_current())
                self.assertIsNone(self.R.baseline())
                self.assertFalse(self.R.material_rejection(self.R.digest({'app.py': 'corrected code'})))


if __name__ == '__main__':
    unittest.main()
