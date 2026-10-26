"""Concrete Phase 2 regression cases; synthetic files and no model sessions."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'prototype'))
from dani_agent import projects as P, workspaces as W, project_workflow as F, model_settings as M
from dani_agent import phase2_fixtures as FIX, project_apply as A, milestone_review as R
from dani_agent.coordinator import APP, human_decision
from dani_agent.state import Store, approval_scope
from test_phase2_projects import Agents
from dani_agent.file_transfer import update_exact



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class Phase2SecurityTests(unittest.TestCase):
    def test_native_file_update_preserves_identity_and_refuses_concurrent_access(self):
        folder = ROOT / 'data' / ('file_update_' + uuid.uuid4().hex[:8]);folder.mkdir()
        path = folder / 'source.txt';path.write_bytes(b'original')
        inode = path.stat().st_ino
        with path.open('rb'):
            with self.assertRaises(OSError):
                update_exact(path, b'original', b'updated')
        self.assertEqual(path.read_bytes(), b'original')
        update_exact(path, b'original', b'updated')
        self.assertEqual(path.read_bytes(), b'updated')
        self.assertEqual(path.stat().st_ino, inode)
        with self.assertRaisesRegex(RuntimeError, 'CONCURRENT_PROJECT_EDIT'):
            update_exact(path, b'stale', b'discard human changes')
        with self.assertRaises(OSError):
            update_exact(path, None, b'never replace concurrent new file')
        self.assertEqual(path.read_bytes(), b'updated')

    def test_native_failed_write_restores_original_and_owned_additions_can_roll_back(self):
        folder = ROOT / 'data' / ('file_restore_' + uuid.uuid4().hex[:8]);folder.mkdir()
        path = folder / 'source.txt';path.write_bytes(b'original')
        with patch('dani_agent.file_transfer.os.fsync', side_effect=[OSError('synthetic flush failure'), None]):
            with self.assertRaisesRegex(RuntimeError, 'ORIGINAL_RESTORED'):
                update_exact(path, b'original', b'partial result')
        self.assertEqual(path.read_bytes(), b'original')
        added = folder / 'added.txt'
        update_exact(added, None, b'new')
        update_exact(added, b'new', None)
        self.assertFalse(added.exists())

    def test_no_empty_failed_or_all_skipped_successful_test_claims(self):
        nonce='a'*32
        for record in ({'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0},
                {'tests': 2, 'failures': 1, 'errors': 0, 'skipped': 0},
                {'tests': 2, 'failures': 0, 'errors': 0, 'skipped': 2}):
            with self.assertRaises(RuntimeError):
                F.test_summary('python-unittest', 'DANI_TEST_COMPLETE=' + json.dumps({'nonce':nonce,'counts':record}), 0,nonce)
        with self.assertRaises(RuntimeError):
            F.test_summary('node-test', '# tests 0\n# pass 0\n# fail 0\n', 0)
        frame='DANI_TEST_COMPLETE='+json.dumps({'nonce':nonce,'counts':{'tests':2,'failures':0,'errors':0,'skipped':0}})
        self.assertEqual(F.test_summary('node-test',frame,0,nonce)['tests'],2)
        for output in (frame+frame,frame.replace(nonce,'b'*32),'DANI_TEST_RESULT={"tests":1}',frame+'\n'+frame):
            with self.assertRaises(RuntimeError):F.test_summary('python-unittest',output,0,nonce)

    def test_git_native_parser_rejects_hidden_keys_and_continuations(self):
        for text in ('[core] worktree=C:/DANI_SYNTHETIC_OUTSIDE\n',
                     '[core]\nrepositoryformatversion=0\n    worktree=C:/DANI_SYNTHETIC_OUTSIDE\n',
                     '[core] sshCommand=NEVER_EXECUTE\n',
                     '[include] path=C:/DANI_SYNTHETIC_NOT_READ\n',
                     '[core "hidden"]\nfilemode=false\n','[core\nmalformed'):
            with self.assertRaisesRegex(RuntimeError,'GIT_CONFIG_REQUIRES_REVIEW'):P.validate_config(text)
        P.validate_config('[core] filemode=false\n[user]\nname=synthetic\n[remote "origin"]\nurl=https://example.invalid/test\n')

    def test_schema_migration_preserves_existing_history_and_updates_projects(self):
        folder = ROOT / 'data' / ('migration_' + uuid.uuid4().hex[:8]);folder.mkdir()
        db = folder / 'state.sqlite3'
        old = Store(db)
        tid = old.add('offline_tests', 'Preserved historical task')
        with sqlite3.connect(db) as conn:
            conn.execute('DROP TABLE projects');conn.execute('DROP TABLE settings');conn.execute('PRAGMA user_version=1')
        current = Store(db)
        self.assertEqual(current.get(tid)['objective'], 'Preserved historical task')
        summary = {'path': str(folder), 'git': False, 'eligible': False}
        first = current.save_project(summary)
        self.assertEqual(current.save_project({**summary, 'name': 'Rescan'}), first)
        self.assertEqual(len(current.projects()), 1)
        current.save_setting('model_settings', M.DEFAULTS)
        self.assertEqual(current.setting('model_settings'), M.DEFAULTS)

    def test_model_budget_and_apply_changes_are_bound_to_approval(self):
        folder = ROOT / 'data' / ('approval2_' + uuid.uuid4().hex[:8]);folder.mkdir()
        store = Store(folder / 'state.sqlite3')
        settings = {**M.DEFAULTS, 'max_codex_sessions': 2, 'max_claude_sessions': 2, 'max_correction_cycles': 1}
        tid = store.add('project_development', 'Scoped task', {'settings': settings})
        self.assertEqual(approval_scope(store.get(tid))['limits']['codex'], 2)
        store.approve(tid, 'sources')
        with store.connection() as db:
            db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps({'settings': M.DEFAULTS}), tid))
        self.assertIsNone(store.claim('sources'))
        self.assertEqual(store.get(tid)['state'], 'awaiting_approval')
        review = store.add('milestone_review', 'Review source', {'snapshot': 'synthetic'})
        self.assertEqual(store.get(review)['state'], 'awaiting_approval')
        self.assertIsNone(store.claim('sources'))

    def test_sensitive_files_and_executable_agent_settings_are_not_imported(self):
        fixture = FIX.create()[0]
        source = Path(fixture['summary']['path'])
        (source / '.claude').mkdir()
        (source / '.claude/settings.json').write_text('{"hooks":{"SessionStart":[{"command":"NEVER_EXECUTE"}]}}')
        (source / 'AGENTS.md').write_text('UNTRUSTED_INSTRUCTIONS')
        P.git(source, 'add', '--all');P.git(source, 'commit', '-m', 'Synthetic untrusted config')
        summary = P.discover(source)
        run = W.prepare(summary, fixture['objective'], fixture['editable_paths'], fixture['test_id'], dict(M.DEFAULTS))
        self.assertFalse((run / 'repo/.claude').exists())
        self.assertFalse((run / 'repo/AGENTS.md').exists())
        self.assertTrue((source / '.claude/settings.json').exists())
        (source / '.env').write_text('SYNTHETIC_SECRET_NOT_REAL')
        P.git(source, 'add', '--force', '.env');P.git(source, 'commit', '-m', 'Synthetic sensitive path')
        self.assertFalse(P.discover(source)['eligible'])

    def test_hard_links_git_config_indirection_and_embedded_secret_signatures_block(self):
        fixture = FIX.create()[0]
        source = Path(fixture['summary']['path'])
        config = source / '.git/config'
        config.write_text(config.read_text() + '\n[filter "untrusted"]\n\tclean = NEVER_EXECUTE\n')
        with self.assertRaisesRegex(RuntimeError, 'GIT_CONFIG_REQUIRES_REVIEW'):
            P.discover(source)
        fixture = FIX.create()[0];source = Path(fixture['summary']['path'])
        (source / 'calculator.py').write_text("secret='" + 'sk-' + 'A' * 40 + "'\n")
        P.git(source, 'add', '--all');P.git(source, 'commit', '-m', 'Synthetic secret signature')
        with self.assertRaisesRegex(RuntimeError, 'EMBEDDED_SECRET_PATTERN'):
            W.prepare(P.discover(source), fixture['objective'], fixture['editable_paths'], fixture['test_id'], dict(M.DEFAULTS))

    def test_apply_refuses_uncommitted_changes_after_acceptance(self):
        fixture = FIX.create()[0]
        run = W.prepare(fixture['summary'], fixture['objective'], fixture['editable_paths'], fixture['test_id'], dict(M.DEFAULTS))
        report = F.execute(run, runner=Agents(run), simulated=True)
        folder = ROOT / 'data' / ('apply_changed_' + uuid.uuid4().hex[:8]);folder.mkdir()
        store = Store(folder / 'db.sqlite3');tid = store.add('project_development', 'Task', {'settings': M.DEFAULTS})
        store.approve(tid, 'test');task = store.claim('test');store.finish(tid, task['run_id'], 'awaiting_acceptance', report)
        (folder / 'jobs' / tid).mkdir(parents=True)
        with patch('dani_agent.coordinator.DATA', folder):human_decision(store, tid, True)
        original = Path(fixture['summary']['path']) / 'calculator.py'
        original.write_text('# Human edits must survive\n')
        with self.assertRaisesRegex(RuntimeError, 'ORIGINAL_CHANGED'):
            A.proposal(store, tid)
        self.assertEqual(original.read_text(), '# Human edits must survive\n')

    def test_milestone_review_uses_only_approved_read_only_batches_and_rejects_tools(self):
        for tool in (False, True):
            payload = R.prepare()
            run = APP['approved_run'](payload['run'])
            folder = run / 'artifacts/milestone_test';folder.mkdir()
            shown_files = set()
            def runner(spec, directory, **kwargs):
                self.assertEqual((kwargs['timeout'], kwargs['idle']), (None, R.SESSION_IDLE_SECONDS))   # no time limit
                self.assertEqual(spec['kind'], 'claude-review')
                evidence = json.loads(spec['prompt'].split('\nEvidence:\n')[1])
                shown = set(evidence.get('sources', ())) | set(evidence.get('unchanged_files_already_reviewed', ())) | set(evidence.get('diffs', ()))
                shown_files.update(shown)
                return {'exit_code': 0, 'initialization': {'tools': ['Bash'] if tool else [], 'mcp_servers_count': 0},
                    'prompt_sent': True, 'events': [{'type': 'result', 'subtype': 'success', 'result': json.dumps({
                        'candidate_sha': payload['candidate_sha'], 'verdict': 'pass', 'findings': []})}]}
            with patch.object(R.runpy, 'run_path', return_value={'verify_offline_tests': lambda: {'verdict': 'PASS', 'scope': 'SIMULATED'}}):
                if tool:
                    with self.assertRaisesRegex(RuntimeError, 'SECURITY_FAILED'):
                        R.execute({'payload': payload}, folder, runner=runner, simulated=True)
                else:
                    result = R.execute({'payload': payload}, folder, runner=runner, simulated=True)
                    self.assertEqual(result['scope'], 'SIMULATED_MILESTONE_REVIEW')
                    self.assertEqual(result['agent_sessions'], {'codex': 0, 'claude': payload.get('review_batch_count', 1)})
                    self.assertIn('dani_agent/project_workflow.py', shown_files)


if __name__ == '__main__':
    unittest.main()
