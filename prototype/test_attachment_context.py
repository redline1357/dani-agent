"""Attachment handoff and approval regressions with fake agents and synthetic owned files only."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import products as PR, workspaces as W, project_workflow as F, model_settings as M


class AttachmentContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='attachment_context_', dir=ROOT / 'prototype')
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to((ROOT / 'prototype').resolve()))
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.stack = ExitStack()
        self.settings = dict(M.DEFAULTS)
        self.text = 'Reference start\n' + ('Synthetic requirement خرید\n' * 500) + '\nFINAL_REFERENCE_REQUIREMENT'
        raw = self.text.encode('utf-8')
        self.refs = [{'id': 'a' * 32, 'name': 'requirements.txt', 'size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'text_sha256': hashlib.sha256(raw).hexdigest(),
            'characters': len(self.text)}]
        self.owner = None
        self.calls = []
        self.context_error = None

        def references(root, ids):
            if ids != [self.refs[0]['id']]:
                raise ValueError('ATTACHMENT_ID_INVALID')
            return json.loads(json.dumps(self.refs))
        def bind(root, refs, owner):
            self.assertFalse((PR.folder(owner) / 'product.json').exists())
            self.owner = owner
        def context(root, refs, owner=None):
            if self.context_error:
                raise RuntimeError(self.context_error)
            if owner != self.owner or refs != self.refs:
                raise RuntimeError('ATTACHMENT_OWNER_OR_METADATA_MISMATCH')
            return self.text
        self.stack.enter_context(patch.object(PR, 'PRODUCTS', self.root / 'products'))
        self.stack.enter_context(patch.object(PR.A, 'references', side_effect=references))
        self.stack.enter_context(patch.object(PR.A, 'bind', side_effect=bind))
        self.stack.enter_context(patch.object(PR.A, 'context', side_effect=context))
        self.stack.enter_context(patch.object(PR, 'hide'))
        self.stack.enter_context(patch.object(M, 'validate', side_effect=lambda value: dict(value)))
        self.stack.enter_context(patch.object(M, 'auto_settings', return_value=self.settings))
        self.stack.enter_context(patch.object(M, 'for_step', return_value=self.settings))
        self.stack.enter_context(patch.object(PR.P, 'git', side_effect=self.git))
        self.stack.enter_context(patch.object(PR.P, 'discover', side_effect=self.discover))
        self.stack.enter_context(patch.object(PR.P, 'read_blobs', side_effect=lambda path, rows:
            {row['path']: (path / row['path']).read_bytes() for row in rows}))
        self.stack.enter_context(patch.object(PR.C, 'reviewer_source', return_value=Path('synthetic-profile')))
        self.stack.enter_context(patch.object(PR.runpy, 'run_path', return_value={'prepare': self.prepare_run}))
        self.stack.enter_context(patch.object(PR, 'recover_integration'))
        self.stack.enter_context(patch.object(PR, 'integrate', return_value='c' * 40))
        self.stack.enter_context(patch.object(W, 'fingerprint', return_value='synthetic-source'))
        self.stack.enter_context(patch.object(F, 'fingerprint', return_value='synthetic-source'))
        self.stack.enter_context(patch.dict(PR.APP, {
            'WORK_RUNS': self.runs, 'approved_run': lambda run: Path(run),
            'commands': lambda run: {'codex': ['synthetic-codex', 'exec', '--model', 'fake', 'prompt'],
                'claude': ['synthetic-claude', '--model', 'fake', '--effort', 'low']},
            'CODEX': 'synthetic-codex', 'PYTHON': sys.executable, 'SHELL_PATH': '',
            'overrides': lambda values: [], 'env': lambda run: {}, 'codex_env': lambda run: {},
        }))

    def tearDown(self):
        self.stack.close()
        self.assertTrue(self.root.is_relative_to((ROOT / 'prototype').resolve()))
        for path in self.root.rglob('*'):
            if path.is_file():
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.temporary.cleanup()

    def prepare_run(self, source, parent=None):
        run = self.runs / uuid.uuid4().hex
        for name in ('repo', 'control/empty_git', 'review_snapshot', 'artifacts', '.claude'):
            (run / name).mkdir(parents=True, exist_ok=True)
        (run / '.mcp.json').write_text('{}')
        (run / '.claude/settings.json').write_text('{}')
        return run

    def discover(self, value):
        path = Path(value)
        files = [{'path': p.relative_to(path).as_posix()} for p in sorted(path.rglob('*'))
                 if p.is_file() and '.git' not in p.relative_to(path).parts]
        digest = hashlib.sha256(json.dumps({row['path']: (path / row['path']).read_text(encoding='utf-8')
                                           for row in files}, sort_keys=True).encode()).hexdigest()
        return {'path': str(path), 'name': path.name, 'eligible': True, 'digest': digest,
            'files': files, 'excluded': [], 'original_sha': 'a' * 40,
            'test_options': [{'id': 'python-unittest', 'baseline_test_files': 1}]}

    def git(self, path, *args, **kwargs):
        path = Path(path)
        if args[0] == 'init':
            (path / '.git').mkdir(exist_ok=True)
        elif args[:2] == ('worktree', 'add'):
            target = Path(args[-2])
            shutil.copytree(path, target, ignore=shutil.ignore_patterns('.git'))
            (target / '.git').write_text('synthetic worktree marker')
        elif args[0] == 'rev-parse':
            return ('b' if path.name == 'repo' else 'a') * 40
        elif args[0] == 'diff':
            return 'main.py\ntest_main.py\n' if '--name-only' in args else '+synthetic implementation\n'
        return b'' if kwargs.get('binary') else ''

    def product(self, attached=True, description='Build a synthetic local text utility.'):
        return PR.create('Synthetic references', description, max_steps=1, settings=self.settings,
            product_kind='python', **({'attachments': [self.refs[0]['id']]} if attached else {}))

    def output(self, value):
        return {'exit_code': 0, 'prompt_sent': True, 'initialization': {'tools': [], 'mcp_servers_count': 0},
            'events': [{'type': 'result', 'subtype': 'success', 'result': json.dumps(value)}]}

    def agent(self, spec, folder, **options):
        self.calls.append(spec)
        if spec['kind'] == 'claude-review' and spec['prompt'].startswith('Plan how to build'):
            return self.output({'steps': [{'title': 'Implement', 'objective': 'Implement the synthetic utility.',
                'difficulty': 'simple', 'files': ['main.py', 'test_main.py']}]})
        if spec['kind'] == 'codex':
            run = Path(spec['cwd']).parent
            for name in ('main.py', 'test_main.py'):
                (run / 'repo' / name).write_text('# synthetic fake-agent output\n', encoding='utf-8')
            options['watch']()
            return {'exit_code': 0, 'events': [{'type': 'turn.completed'}, {'type': 'final', 'text': json.dumps({
                'status': 'implemented', 'changed_files': ['main.py', 'test_main.py'], 'summary': 'synthetic'})}]}
        if spec['kind'] == 'plain':
            return {'exit_code': 0, 'stdout': 'DANI_TEST_COMPLETE=' + json.dumps({'nonce': spec['test_completion_nonce'],
                'counts': {'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 0}}) + '\n'}
        handoff = json.loads(spec['prompt'].split('\nEvidence:\n', 1)[1])
        return self.output({'candidate_sha': handoff['candidate_sha'], 'verdict': 'pass', 'findings': []})

    def test_invalid_references_are_rejected_before_product_files_or_git(self):
        with self.assertRaisesRegex(ValueError, 'ATTACHMENT_ID_INVALID'):
            PR.create('Invalid attachment', 'Build a synthetic utility.', attachments=['bad'])
        self.assertFalse(PR.PRODUCTS.exists())
        PR.P.git.assert_not_called()

    def test_valid_attachments_are_bound_before_publish_and_can_supply_description(self):
        for description in ('', 'brief'):
            with self.subTest(description=description):
                product = self.product(description=description)
                self.assertEqual(product['attachments'], self.refs)
                self.assertEqual(self.owner, product['id'])
                self.assertIn('attached reference documents', product['description'])
                if description:
                    self.assertIn(description, product['description'])
                self.assertNotIn('FINAL_REFERENCE_REQUIREMENT', json.dumps(product))
        with self.assertRaisesRegex(ValueError, 'DESCRIBE_THE_PRODUCT'):
            self.product(attached=False, description='')

    def test_request_build_binds_exact_metadata_and_legacy_payload_remains_unchanged(self):
        for attached in (True, False):
            with self.subTest(attached=attached):
                product = self.product(attached=attached)
                store = Mock()
                store.add.return_value = 'task'
                PR.request_build(store, product['id'])
                payload = store.add.call_args.args[2]
                expected = {'product_id', 'budget', 'limits'} | ({'attachments_sha256'} if attached else set())
                self.assertEqual(set(payload), expected)
                self.assertEqual(payload.get('attachments_sha256'), PR.attachments_digest(product))
                store.get.return_value = {'id': 'task', 'state': 'queued', 'payload': payload}
                self.assertEqual(PR.request_build(store, product['id']), 'task')
                if attached:
                    store.get.return_value['payload'] = {**payload, 'attachments_sha256': 'changed'}
                    with self.assertRaisesRegex(RuntimeError, 'ATTACHMENT_APPROVAL_MISMATCH'):
                        PR.request_build(store, product['id'])

    def test_attachment_approval_and_missing_or_changed_files_fail_before_models(self):
        product = self.product()
        for digest, failure in [(None, None), ('wrong', None),
                (PR.attachments_digest(product), 'ATTACHMENT_MISSING'),
                (PR.attachments_digest(product), 'ATTACHMENT_HASH_MISMATCH'),
                (PR.attachments_digest(product), 'ATTACHMENT_OWNER_MISMATCH')]:
            with self.subTest(digest=digest, failure=failure):
                self.context_error = failure
                runner = Mock()
                with self.assertRaisesRegex(RuntimeError, 'ATTACHMENT_'):
                    PR.build(product['id'], runner=runner, simulated=True, approved_attachments_sha256=digest)
                runner.assert_not_called()
                self.assertEqual(PR.load(product['id'])['state'], 'awaiting_approval')
        self.assertIn('ATTACHMENT_', PR.NOT_RETRIED)

    def test_full_reference_reaches_planner_read_only_developer_file_and_reviewer(self):
        product = self.product()
        result = PR.build(product['id'], runner=self.agent, simulated=True,
            approved_attachments_sha256=PR.attachments_digest(product))
        self.assertEqual(result['verdict'], 'PASS', result.get('reason'))
        planner, developer, _, reviewer = self.calls
        self.assertIn(json.dumps(self.text, ensure_ascii=False), planner['prompt'])
        self.assertIn('untrusted', planner['prompt'])
        self.assertGreater(len(self.text), 4000)
        run = Path(developer['cwd']).parent
        plan = W.load(run)
        self.assertEqual(W.reference_context(run, plan), self.text)
        reference = run / W.REFERENCE_CONTEXT_PATH
        self.assertEqual(reference.read_bytes(), self.text.encode('utf-8'))
        self.assertEqual(plan['reference_context']['sha256'], hashlib.sha256(reference.read_bytes()).hexdigest())
        self.assertFalse(reference.stat().st_mode & stat.S_IWRITE)
        self.assertIn(json.dumps(str(reference)), developer['argv'][-1])
        self.assertNotIn('FINAL_REFERENCE_REQUIREMENT', developer['argv'][-1])
        W.check_command_size(developer['argv'])
        handoff = json.loads(reviewer['prompt'].split('\nEvidence:\n', 1)[1])
        self.assertEqual(handoff['reference_context'], self.text)
        self.assertEqual(json.loads((run / 'artifacts/cycle_0/handoff.json').read_text())['reference_context'], self.text)
        profile = W.profile(run, True)['permissions.projectdev.filesystem']
        self.assertEqual(profile[str(run)], 'read')
        self.assertNotIn(str(reference), profile)  # Inherits existing read access; no broader permission added.
        self.assertNotIn(W.REFERENCE_CONTEXT_PATH, plan['editable_paths'])

    def test_legacy_build_has_no_reference_file_or_context_and_uses_old_call_contract(self):
        product = self.product(attached=False)
        self.assertNotIn('attachments', product)
        result = PR.build(product['id'], runner=self.agent, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result.get('reason'))
        self.assertNotIn('Attached reference', self.calls[0]['prompt'])
        developer = next(call for call in self.calls if call['kind'] == 'codex')
        run = Path(developer['cwd']).parent
        self.assertNotIn('reference_context', W.load(run))
        self.assertFalse((run / W.REFERENCE_CONTEXT_PATH).exists())
        reviewer = self.calls[-1]
        self.assertNotIn('reference_context', json.loads(reviewer['prompt'].split('\nEvidence:\n', 1)[1]))

    def test_missing_or_tampered_workspace_reference_fails_before_any_model_call(self):
        product = self.product()
        summary = self.discover(product['app'])
        for mode in ('missing', 'changed', 'path', 'size', 'hash'):
            with self.subTest(mode=mode):
                run = W.prepare(summary, 'Implement synthetic task.', ['main.py', 'test_main.py'],
                    'python-unittest', self.settings, reference_context=self.text)
                target = run / W.REFERENCE_CONTEXT_PATH
                target.chmod(stat.S_IREAD | stat.S_IWRITE)
                if mode == 'missing': target.unlink()
                elif mode == 'changed': target.write_text('changed')
                else:
                    plan_path = run / 'project_control/plan.json'
                    plan = json.loads(plan_path.read_text())
                    plan['reference_context'][{'path':'path', 'size':'characters', 'hash':'sha256'}[mode]] = {
                        'path':'../../outside.txt', 'size':999999, 'hash':'0'*64}[mode]
                    plan_path.write_text(json.dumps(plan))
                runner = Mock()
                with self.assertRaisesRegex(RuntimeError, 'ATTACHMENT_REFERENCE_CONTEXT_INVALID'):
                    F.execute(run, runner=runner, simulated=True)
                runner.assert_not_called()

    def test_reference_cannot_change_during_developer_or_read_only_test_session(self):
        for role in ('codex', 'plain'):
            with self.subTest(role=role):
                product = self.product()
                def tamper(spec, folder, **options):
                    if spec['kind'] == role:
                        reference = folder.parents[1] / W.REFERENCE_CONTEXT_PATH
                        reference.chmod(stat.S_IREAD | stat.S_IWRITE)
                        reference.write_text('tampered synthetic reference')
                        options['watch']()
                    return self.agent(spec, folder, **options)
                result = PR.build(product['id'], runner=tamper, simulated=True,
                    approved_attachments_sha256=PR.attachments_digest(product))
                self.assertEqual(result['verdict'], 'FAIL')
                self.assertIn('ATTACHMENT_REFERENCE_CONTEXT_INVALID', result['reason'])

    def test_large_context_is_kept_whole_when_reviewer_budget_requires_reduction(self):
        handoff = {'reference_context': self.text, 'diff': 'diff', 'test_evidence': {'output':'x'*12000}}
        with patch.object(F, 'REVIEW_CONTEXT_CHARS', 2000), patch.object(F, 'REVIEW_HARD_CHARS', 200000):
            F.fit_review_base(handoff)
        self.assertEqual(handoff['reference_context'], self.text)
        with patch.object(F, 'REVIEW_HARD_CHARS', 100):
            with self.assertRaisesRegex(RuntimeError, 'PROJECT_CHANGE_TOO_LARGE'):
                F.review_prompt(handoff, {})
        self.assertEqual(handoff['reference_context'], self.text)
        with self.assertRaisesRegex(ValueError, 'ATTACHMENT_REFERENCE_CONTEXT_LIMIT'):
            W.prepare({}, 'task', [], 'python-unittest', {}, reference_context='x'*50001)

    def test_reference_failure_between_initial_validation_and_planning_spends_no_session(self):
        product = self.product()
        runner = Mock()
        with patch.object(PR.A, 'context', side_effect=[self.text, PR.A.AttachmentError('ATTACHMENT_MISSING')]):
            result = PR.build(product['id'], runner=runner, simulated=True,
                approved_attachments_sha256=PR.attachments_digest(product))
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertEqual(result['reason'], 'ATTACHMENT_MISSING')
        self.assertEqual(result['agent_sessions'], {'codex': 0, 'claude': 0})
        runner.assert_not_called()

    def test_exact_fifty_thousand_character_reference_is_whole_and_off_command_line(self):
        product = self.product()
        text = 'خ' * 49996 + 'TAIL'
        run = W.prepare(self.discover(product['app']), 'Implement the utility.', ['main.py', 'test_main.py'],
            'python-unittest', self.settings, reference_context=text)
        self.assertEqual(W.reference_context(run, W.load(run)), text)
        command = W.commands(run)['codex']
        self.assertNotIn('TAIL', command[-1])
        W.check_command_size(command)
        handoff = {'reference_context': text, 'diff': '', 'sources': {}}
        F.fit_review_base(handoff)
        prompt = F.review_prompt(handoff, {})
        self.assertEqual(json.loads(prompt.split('\nEvidence:\n', 1)[1])['reference_context'], text)


if __name__ == '__main__':
    unittest.main()
