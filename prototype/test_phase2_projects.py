"""Disposable real Git worktrees and fault-injected providers; zero model calls."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import projects as P, workspaces as W, project_workflow as F, model_settings as M
from dani_agent import phase2_fixtures as FIX, project_apply as APPLY
from dani_agent.coordinator import APP, human_decision
from dani_agent.state import Store, approval_scope



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class Agents:
    def __init__(self, run, fault=None):
        self.run, self.fault = run, fault
        self.dev = self.review = 0
        self.prompts = []

    def __call__(self, spec, folder, **options):
        if spec['kind'] == 'codex':
            self.dev += 1
            self.prompts.append(spec['argv'][-1])
            if self.fault == 'quota':
                return {'exit_code': 1, 'diagnostic_codes': ['QUOTA_EXHAUSTED']}
            name = W.load(self.run)['editable_paths'][0]
            text = 'def add(a, b):\n    return a + b\n' if name.endswith('.py') else 'export function add(a,b) { return a + b; }\n'
            text += ('#' if name.endswith('.py') else '//') + ' candidate ' + str(self.dev) + '\n'
            if self.fault == 'ansi' and self.dev == 1:   # Windows PowerShell 5.1's Set-Content: ANSI, not UTF-8
                (self.run / 'repo' / name).write_bytes((text + '// StockPilot · Inventory\n').encode('cp1252'))
            else:
                (self.run / 'repo' / name).write_text(text, encoding='utf-8')
            if self.fault == 'protected':
                (self.run / 'repo/AGENTS.md').write_text('untrusted')
            options['watch']()
            if self.fault == 'no_final':
                return {'exit_code':0,'events':[{'type':'turn.completed'}]}
            return {'exit_code': 0, 'events': [{'type': 'turn.completed'}, {'type': 'final', 'text': json.dumps({
                'status': 'implemented', 'changed_files': [name], 'summary': 'Synthetic project provider'})}]}
        if spec['kind'] == 'plain':
            if self.fault == 'forged_test':
                return {'exit_code':0,'stdout':'DANI_TEST_RESULT={"tests":1,"failures":0,"errors":0,"skipped":0}\n'}
            return {'exit_code': 1 if self.fault == 'tests' else 0,
                'stdout': 'SIMULATED ONLY\nDANI_TEST_COMPLETE='+json.dumps({'nonce':spec['test_completion_nonce'],
                    'counts':{'tests':1,'failures':0,'errors':0,'skipped':0}})+'\n'}
        if spec['kind'] == 'claude-review':
            data = json.loads(spec['prompt'].split('\nEvidence:\n')[1])
            self.review += 1
            changes = self.review == 1 or self.fault == 'limit'
            findings = [{'severity': 'medium', 'file': next(iter(data['sources'])), 'line': 1,
                'explanation': 'SYNTHETIC_ACTIONABLE_REVIEW'}] if changes else []
            if self.fault == 'advice' and not changes:
                findings = [{'severity': 'low', 'file': next(iter(data['sources'])), 'line': 1, 'explanation': 'Nonblocking advice'}]
            if self.fault == 'high_pass' and not changes:
                findings = [{'severity': 'high', 'file': next(iter(data['sources'])), 'line': 1, 'explanation': 'Material contradiction'}]
            value = {'candidate_sha': 'wrong' if self.fault == 'sha' else data['candidate_sha'],
                'verdict': 'changes_requested' if changes else 'pass', 'findings': findings}
            return {'exit_code': 0, 'initialization': {'tools': ['Bash'] if self.fault == 'tool' else [], 'mcp_servers_count': 0},
                'prompt_sent': True, 'events': [{'type': 'result', 'subtype': 'success', 'result':
                    '```json\n'+json.dumps(value)+'\n```' if self.fault == 'fenced' else json.dumps(value)}]}
        raise AssertionError(spec['kind'])


class Phase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = FIX.create()

    def run_project(self, fault=None, index=0):
        fixture = self.fixtures[index]
        run = W.prepare(fixture['summary'], fixture['objective'], fixture['editable_paths'], fixture['test_id'], dict(M.DEFAULTS))
        fake = Agents(run, fault)
        events = []
        report = F.execute(run, runner=fake, simulated=True, event=lambda k, v: events.append((k, v)))
        return run, fake, report, events

    def test_python_next_react_typescript_and_test_detection(self):
        python, node = [f['summary'] for f in self.fixtures]
        self.assertTrue(python['eligible'])
        self.assertIn('Python', python['languages'])
        self.assertIn('Next.js', node['frameworks'])
        self.assertIn('React', node['frameworks'])
        self.assertIn('TypeScript', node['languages'])
        self.assertEqual(next(t for t in node['test_options'] if t['id'] == 'npm-test')['script'], 'node --test arithmetic.test.mjs')

    def test_real_worktree_private_git_original_and_approved_scope(self):
        fixture = self.fixtures[0]
        before = APP['inventory'](Path(fixture['summary']['path']))
        run, fake, report, events = self.run_project('advice')
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['scope'], 'SIMULATED_PROJECT_DEVELOPMENT')
        self.assertEqual(report['agent_sessions'], {'codex': 2, 'claude': 2})
        self.assertEqual(report['correction_cycles'], 1)
        self.assertTrue((run / 'repo/.git').is_file())
        common = Path(P.git(run / 'repo', 'rev-parse', '--git-common-dir')).resolve()
        self.assertTrue(common.is_relative_to(run))
        self.assertTrue(report['branch'].startswith('codex/'))
        self.assertIn('SYNTHETIC_ACTIONABLE_REVIEW', fake.prompts[1])
        self.assertEqual(sum(k == 'feedback_forwarded' for k, _ in events), 1)
        self.assertEqual(APP['inventory'](Path(fixture['summary']['path'])), before)
        self.assertEqual(report['candidate_sha'], report['cycles'][-1]['review']['candidate_sha'])
        self.assertEqual(report['cycles'][-1]['review']['findings'][0]['severity'], 'low')

    def test_a_file_saved_as_ansi_is_corrected_by_codex_not_a_stop(self):
        # StockPilot step 3 (2026-10-24): index.html saved as ANSI stopped the whole build with UnicodeDecodeError.
        run, fake, report, _ = self.run_project('ansi', index=1)
        self.assertEqual(report['verdict'], 'PASS', report.get('reason'))
        first = report['cycles'][0]['tests']
        self.assertEqual(first['exit_code'], 1)
        self.assertIn('not UTF-8 text', first['output'])
        self.assertIn('not UTF-8 text', fake.prompts[1])          # Codex is told what to fix
        self.assertIn('Save every file as UTF-8', fake.prompts[0])  # and how to avoid it in the first place

    def test_node_project_uses_same_automatic_handoff_without_shell_on_host(self):
        run, fake, report, _ = self.run_project(index=1)
        self.assertEqual(report['verdict'], 'PASS')
        argv = W.commands(run)['tests']
        self.assertIn(str(W.NODE), argv)
        self.assertIn('--test', argv)
        self.assertIn('--preserve-symlinks-main',argv)
        self.assertIn('./arithmetic.test.mjs',argv)
        self.assertNotIn('install', argv)
        self.assertEqual(report['changed_files'], ['arithmetic.mjs'])

    def test_fenced_review_handoff_and_missing_developer_final(self):
        self.assertEqual(self.run_project('fenced')[2]['verdict'],'PASS')
        result=self.run_project('no_final')[2]
        self.assertEqual(result['reason'],'DEVELOPER_FINAL_MISSING')
        self.assertEqual(result['agent_sessions'],{'codex':1,'claude':0})

    def test_forged_test_success_stops_before_any_reviewer(self):
        result=self.run_project('forged_test')[2]
        self.assertEqual(result['reason'],'PROJECT_TEST_UNIQUE_COMPLETION_REQUIRED')
        self.assertEqual(result['agent_sessions'],{'codex':1,'claude':0})

    def test_blocked_findings_unexpected_tools_quota_protected_files_and_sha(self):
        for fault, reason in (('protected', 'PROJECT_PROTECTED_OR_OUT_OF_SCOPE_CHANGE'),
                ('tool', 'PROJECT_REVIEWER_SECURITY_FAILED'), ('sha', 'PROJECT_REVIEW_REVISION_OR_FINDINGS_INVALID'),
                ('quota', 'QUOTA_EXHAUSTED'), ('high_pass', 'REVIEW_BLOCKED_OR_INCONSISTENT_NEEDS_HUMAN')):
            with self.subTest(fault=fault):
                _, fake, report, _ = self.run_project(fault)
                self.assertEqual(report['reason'], reason)
                self.assertNotEqual(report['verdict'], 'PASS')
                self.assertLessEqual(fake.dev, 3)
                if fault == 'quota':
                    self.assertEqual(fake.review, 0)

    def test_two_correction_cap_and_failed_test_handoff(self):
        for fault, reason in (('limit', 'CORRECTION_LIMIT_REACHED'), ('tests', 'PROJECT_TESTS_FAILED_CORRECTION_LIMIT')):
            with self.subTest(fault=fault):
                _, fake, report, _ = self.run_project(fault)
                self.assertEqual(report['reason'], reason)
                self.assertEqual(fake.dev, 3)
                self.assertEqual(fake.review, 0 if fault == 'tests' else 3)

    def test_dirty_project_never_cleaned_or_initialized(self):
        folder = ROOT / 'data' / ('dirty_' + uuid.uuid4().hex[:8])
        folder.mkdir()
        no_git = P.discover(folder)
        self.assertFalse(no_git['git'])
        self.assertFalse((folder / '.git').exists())
        (folder / 'a.py').write_text('pass\n')
        P.git(folder, 'init', '--template=')
        P.git(folder, 'add', '--all')
        P.git(folder, 'commit', '-m', 'baseline')
        (folder / 'a.py').write_text('# uncommitted\n')
        summary = P.discover(folder)
        self.assertTrue(summary['dirty'])
        self.assertFalse(summary['eligible'])
        self.assertEqual((folder / 'a.py').read_text(), '# uncommitted\n')

    def test_path_traversal_reserved_paths_and_protected_configuration(self):
        for path in ('../x.py', '/x.py', 'C:/x.py', 'x\\a.py', 'NUL.py', 'src/../x.py', 'a.py:stream', 'src/COM1.txt'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                P.relative(path)
        for path in ('.env', '.codex/config.toml', 'AGENTS.md', '.mcp.json', 'package.json', 'pyproject.toml'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                W.select_scope(self.fixtures[0]['summary'], [path])

    def test_model_and_session_settings_fail_closed(self):
        for change in ({'codex_model': 'unverified'}, {'claude_model': 'opus'}, {'seconds_per_session': M.MAX_SECONDS_PER_SESSION + 1},
                {'max_correction_cycles': 3}, {'max_codex_sessions': 1}, {'max_claude_sessions': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                M.validate({**M.DEFAULTS, **change})
        self.assertEqual(M.availability(M.DEFAULTS)['status'], 'PREVIOUSLY_SUBSCRIPTION_VERIFIED')

    def test_preflight_profiles_deny_original_sensitive_paths_and_network(self):
        fixture = self.fixtures[0]
        run = W.prepare(fixture['summary'], fixture['objective'], fixture['editable_paths'], fixture['test_id'], dict(M.DEFAULTS))
        seen = []
        def runner(spec, folder, **kwargs):
            seen.append(spec)
            return {'exit_code': 0, 'stdout': 'DANI_PROJECT_SECURITY_OK'}
        result = F.preflight(run, runner=runner, simulated=True)
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(len(seen), 3)
        for writable in (True, False):
            name = 'projectdev' if writable else 'projecttest'
            flags = W.profile(run, writable)
            self.assertFalse(flags['permissions.' + name + '.network.enabled'])
            fs = flags['permissions.' + name + '.filesystem']
            self.assertEqual(fs[str(run / 'repo')], 'read')
            self.assertEqual(fs.get(str(run / 'repo/calculator.py'), 'read'), 'write' if writable else 'read')
            self.assertEqual(fs[fixture['summary']['path']], 'deny')
            self.assertEqual(fs[str(run / 'sensitive_probe')], 'deny')
            self.assertEqual(fs[str(run / 'repo/AGENTS.md')], 'deny')
        with self.assertRaisesRegex(RuntimeError, 'CURRENT_NATIVE_PROJECT_PREFLIGHT_REQUIRED'):
            F.check_ready(run)  # Simulated gates never authorize live execution.

    def test_exact_revision_acceptance_then_separate_apply_approval(self):
        # Use a private fixture instance because this test deliberately applies changes.
        fixture = FIX.create()[0]
        run = W.prepare(fixture['summary'], fixture['objective'], fixture['editable_paths'], fixture['test_id'], dict(M.DEFAULTS))
        report = F.execute(run, runner=Agents(run), simulated=True)
        folder = ROOT / 'data' / ('apply_test_' + uuid.uuid4().hex[:8]);folder.mkdir()
        store = Store(folder / 'state.sqlite3')
        payload = {'run': str(run), 'settings': dict(M.DEFAULTS)}
        tid = store.add('project_development', fixture['objective'], payload)
        store.approve(tid, 'test');task = store.claim('test')
        store.finish(tid, task['run_id'], 'awaiting_acceptance', report)
        with self.assertRaises(ValueError):
            APPLY.proposal(store, tid)
        (folder / 'jobs' / tid).mkdir(parents=True)
        with patch('dani_agent.coordinator.DATA', folder):
            human_decision(store, tid, True)
        proposal = APPLY.proposal(store, tid)
        apply_id = store.add('project_apply', 'Apply approved revision', proposal)
        self.assertIsNone(store.claim('test'))  # Accept is NOT merge permission.
        store.approve(apply_id, 'test')
        claimed = store.claim('test')
        self.assertEqual(claimed['kind'], 'project_apply')
        result_dir = folder / 'apply'; result_dir.mkdir()
        outcome = APPLY.apply(store, proposal, result_dir)
        self.assertEqual(outcome['verdict'], 'PASS')
        self.assertIn('a + b', (Path(fixture['summary']['path']) / 'calculator.py').read_text())
        self.assertEqual(P.git(Path(fixture['summary']['path']), 'rev-parse', 'HEAD'), fixture['summary']['original_sha'])
        with self.assertRaisesRegex(RuntimeError, 'ORIGINAL_CHANGED'):
            APPLY.proposal(store, tid)

    def test_rollback_rejection_preserves_source_and_evidence(self):
        run, _, report, _ = self.run_project()
        folder = ROOT / 'data' / ('reject_' + uuid.uuid4().hex[:8]);folder.mkdir()
        store = Store(folder / 'db.sqlite3')
        tid = store.add('project_development', 'Reject candidate', {'settings': dict(M.DEFAULTS)})
        store.approve(tid, 'test');task = store.claim('test')
        store.finish(tid, task['run_id'], 'awaiting_acceptance', report)
        (folder / 'jobs' / tid).mkdir(parents=True)
        with patch('dani_agent.coordinator.DATA', folder):
            human_decision(store, tid, False)
        self.assertEqual(store.get(tid)['state'], 'rejected')
        self.assertTrue((run / 'repo').is_dir())
        self.assertEqual(P.discover(self.fixtures[0]['summary']['path'])['digest'], self.fixtures[0]['summary']['digest'])


if __name__ == '__main__':
    unittest.main()
