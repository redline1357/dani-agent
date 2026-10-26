"""Product builder: template, planning, step-by-step build and integration with fake agents; zero model calls."""
import hashlib
import json
import os
import shutil
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import products as PR, projects as P, workspaces as W, model_settings as M
from dani_agent.state import Store, approval_scope

PLAN = {'steps': [
    {'title': 'Cart logic', 'objective': 'Add a cart module with add/remove/total.', 'difficulty': 'normal',
     'files': ['public/lib/cart.mjs', 'public/lib/cart.test.mjs']},
    {'title': 'Cart API', 'objective': 'Expose GET /api/cart from server.mjs.', 'difficulty': 'hard', 'files': ['server.mjs', 'server.test.mjs']}]}



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class Agents:
    """Fake planner, developer, test runner and reviewer."""
    def __init__(self, plan=PLAN, on_codex=None, notes=0, polish_fails=False, note_text='consider handling empty input.'):
        self.plan, self.on_codex, self.notes, self.polish_fails, self.note_text = plan, on_codex, notes, polish_fails, note_text
        self.codex = self.claude = 0
        self.prompts, self.objectives = [], []

    def __call__(self, spec, folder, **options):
        if spec['kind'] == 'claude-review':
            self.claude += 1
            if spec['prompt'].startswith('Plan how to build'):
                self.prompts.append(spec['prompt'])
                plan = self.plan.pop(0) if isinstance(self.plan, list) else self.plan   # a list: one plan per try
                text = json.dumps(plan)
            else:
                evidence = json.loads(spec['prompt'].split('\nEvidence:\n')[1])
                polishing = 'Polish step.' in evidence.get('task', '')
                files = sorted(evidence.get('sources', {}))
                findings = [] if polishing or not files else [{'severity': 'low', 'file': files[0], 'line': None,
                    'explanation': 'Note ' + str(i) + ': ' + self.note_text} for i in range(self.notes)]
                verdict = 'changes_requested' if polishing and self.polish_fails else 'pass'
                if verdict != 'pass':
                    findings = [{'severity': 'medium', 'file': files[0], 'line': None, 'explanation': 'The polish broke something.'}]
                text = json.dumps({'candidate_sha': evidence['candidate_sha'], 'verdict': verdict, 'findings': findings})
            return {'exit_code': 0, 'prompt_sent': True, 'initialization': {'tools': [], 'mcp_servers_count': 0},
                    'events': [{'type': 'result', 'subtype': 'success', 'result': text}]}
        if spec['kind'] == 'codex':
            self.codex += 1
            run = Path(spec['cwd']).parent
            self.objectives.append(W.load(run)['objective'])
            names = W.load(run)['editable_paths']
            for name in names:
                (run / 'repo' / name).write_text('// built by fake codex ' + str(self.codex) + '\n')
            if self.on_codex:
                self.on_codex(run)
            options['watch']()
            return {'exit_code': 0, 'events': [{'type': 'turn.completed'}, {'type': 'final', 'text': json.dumps(
                {'status': 'implemented', 'changed_files': names,
                 'summary': 'Note 1: fixed - handled empty input' if 'Polish step.' in self.objectives[-1] else 'fake'})}]}
        if spec['kind'] == 'plain':
            return {'exit_code': 0, 'stdout': 'DANI_TEST_COMPLETE=' + json.dumps({'nonce': spec['test_completion_nonce'],
                'counts': {'tests': 3, 'failures': 0, 'errors': 0, 'skipped': 0}}) + '\n'}
        raise AssertionError(spec['kind'])


class ProductCase(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'data' / ('products_test_' + uuid.uuid4().hex[:8])
        self.root.mkdir()
        patcher = patch.object(PR, 'PRODUCTS', self.root)   # keep test products out of the real dashboard
        patcher.start()
        self.addCleanup(patcher.stop)


class ProductTests(ProductCase):
    def test_create_makes_a_clean_eligible_starter_repository(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        app = Path(product['app'])
        self.assertEqual(app, self.root / 'demo-shop')                            # products/<name>: only the code
        self.assertTrue((self.root / '.dani' / product['id'] / 'product.json').is_file())   # the records, hidden
        summary = P.discover(app)
        self.assertTrue(summary['eligible'])
        self.assertFalse(summary['dirty'])
        node = next(o for o in summary['test_options'] if o['id'] == 'node-test')
        self.assertEqual(node['baseline_test_files'], 1)
        self.assertEqual(product['state'], 'awaiting_approval')
        self.assertEqual(PR.load(product['id'])['name'], 'Demo Shop')

    def test_invalid_input_and_budget_are_refused(self):
        for name, description in (('', 'long enough text'), ('ok', 'short'), ('bad\nname', 'long enough text')):
            with self.subTest(name=name), self.assertRaises(ValueError):
                PR.create(name, description)
        for steps in (0, 9, '3'):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                PR.budget(steps)

    def test_build_plans_then_builds_and_commits_each_step(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        fake = Agents()
        result = PR.build(product['id'], task_id='t1', runner=fake, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        built = PR.load(product['id'])
        self.assertEqual(built['state'], 'ready')
        self.assertEqual([s['state'] for s in built['steps']], ['done', 'done'])
        app = Path(built['app'])
        self.assertEqual(len(P.git(app, 'log', '--oneline').splitlines()), 3)
        self.assertIn('fake codex', (app / 'public/lib/cart.mjs').read_text())
        self.assertEqual(P.git(app, 'status', '--porcelain'), '')
        self.assertEqual((fake.codex, fake.claude), (2, 3))      # one planning review + one review per step
        self.assertEqual(result['agent_sessions'], {'codex': 2, 'claude': 3})
        self.assertIn('Product description:\nA small shop', fake.prompts[0])
        self.assertIn('as FEW small, independently testable steps as it needs', fake.prompts[0])
        self.assertEqual(PR.create('Default', 'A product with the default step ceiling.')['budget']['max_steps'], PR.MAX_STEPS)

    def test_unsafe_or_untested_plans_stop_before_any_developer_session(self):
        for plan, reason in (
                ({'steps': [{'title': 'x', 'objective': 'y', 'difficulty': 'simple', 'files': ['package.json', 'a.test.mjs']}]}, 'PRODUCT_PLAN_UNSAFE_FILE'),
                ({'steps': [{'title': 'x', 'objective': 'y', 'difficulty': 'simple', 'files': ['public/a.js']}]}, 'PRODUCT_PLAN_STEP_WITHOUT_TEST_FILE'),
                ({'steps': [PLAN['steps'][0]] * 4}, 'PRODUCT_PLAN_STEP_COUNT_INVALID')):
            with self.subTest(reason=reason):
                product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
                fake = Agents(plan)
                result = PR.build(product['id'], runner=fake, simulated=True)
                self.assertTrue(result['reason'].startswith(reason), result['reason'])
                self.assertEqual(fake.codex, 0)
                self.assertEqual(PR.load(product['id'])['state'], 'stopped')
                self.assertEqual(fake.claude, PR.PLAN_ATTEMPTS)                 # the same refused plan, twice
                self.assertIn('Your previous plan was refused by Dani Agent\'s checks (' + reason, fake.prompts[1])

    def test_a_refused_plan_goes_back_to_the_planner_once(self):
        # 2026-10-24: StockPilot's rebuild plan was refused and the whole product stopped.
        bad = {'steps': [{'title': 'x', 'objective': 'y', 'difficulty': 'simple', 'files': ['public/a.js']}]}
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        fake = Agents([bad, PLAN])
        result = PR.build(product['id'], runner=fake, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        self.assertIn('PRODUCT_PLAN_STEP_WITHOUT_TEST_FILE', fake.prompts[1])
        self.assertEqual(result['agent_sessions']['claude'], 2 + len(PLAN['steps']))

    def test_a_documentation_only_step_after_tested_steps_needs_no_test_file(self):
        summary = {'files': []}
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=4)
        readme = {'title': 'README', 'objective': 'Write the README.', 'difficulty': 'simple', 'files': ['README.md']}
        with patch.object(W, 'select_scope', side_effect=lambda summary, files: files):
            self.assertEqual(len(PR.validate_plan(product, summary, {'steps': [PLAN['steps'][0], dict(readme)]})), 2)
            with self.assertRaisesRegex(RuntimeError, 'PRODUCT_PLAN_STEP_WITHOUT_TEST_FILE'):   # no tests exist yet
                PR.validate_plan(product, summary, {'steps': [dict(readme)]})
            with self.assertRaisesRegex(RuntimeError, 'PRODUCT_PLAN_STEP_WITHOUT_TEST_FILE'):   # code still needs a test
                PR.validate_plan(product, summary, {'steps': [PLAN['steps'][0], dict(readme, files=['README.md', 'public/a.js'])]})

    def test_product_edited_outside_dani_agent_stops_the_build(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        app = Path(product['app'])
        def human_edit(run):
            (app / 'README.md').write_text('edited by a human while step 1 was running\n')
        result = PR.build(product['id'], runner=Agents(on_codex=human_edit), simulated=True)
        # The workflow's own original-project guard fires first; integrate() is the last line of defence.
        self.assertIn(result['reason'], ('PRODUCT_STEP_1_ORIGINAL_PROJECT_CHANGED_STOPPED_WITHOUT_OVERWRITE',
                                         'PRODUCT_CHANGED_OUTSIDE_DANI_AGENT_STOPPED'))
        self.assertIn('edited by a human', (app / 'README.md').read_text())
        self.assertEqual(len(P.git(app, 'log', '--oneline').splitlines()), 1)

    def test_a_step_that_fails_is_tried_again_until_it_passes(self):
        # 2026-10-24, the owner: "product never stop with any reason".
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        failures = []
        def flaky(run):
            if len(failures) < 2:
                failures.append(run)
                raise RuntimeError('SYNTHETIC_DEVELOPER_FAILURE')
        fake = Agents(on_codex=flaky)
        with patch.object(PR, 'SIMULATED_ATTEMPTS', 5):
            result = PR.build(product['id'], runner=fake, simulated=True)
        built = PR.load(product['id'])
        self.assertEqual((result['verdict'], built['state']), ('PASS', 'ready'), result.get('reason'))
        self.assertEqual([s['state'] for s in built['steps']], ['done', 'done'])
        self.assertEqual([a['reason'] for a in built['steps'][0]['attempts']], ['SYNTHETIC_DEVELOPER_FAILURE'] * 2)
        self.assertIn('An earlier attempt at this step did not pass (SYNTHETIC_DEVELOPER_FAILURE)', fake.objectives[1])
        self.assertNotIn('An earlier attempt', fake.objectives[0])
        self.assertEqual([PR.escalate('normal', n) for n in range(4)], ['normal', 'normal', 'hard', 'hard'])
        self.assertEqual(PR.retry_wait(1), 0)
        self.assertEqual(PR.retry_wait(50), 3600)                           # a lasting problem: hourly, never a stop

    def test_edits_made_outside_dani_agent_are_kept_and_the_build_goes_on(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        app = Path(product['app'])
        edited = []
        def human_edit(run):
            if not edited:
                edited.append(True)
                (app / 'README.md').write_text('edited by a human while step 1 was running\n')
        with patch.object(PR, 'SIMULATED_ATTEMPTS', 3):
            result = PR.build(product['id'], runner=Agents(on_codex=human_edit), simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result.get('reason'))
        self.assertIn('edited by a human', (app / 'README.md').read_text())   # never overwritten
        self.assertIn('Changes made outside Dani Agent, kept as they were', P.git(app, 'log', '--format=%s'))

    def test_an_automatic_resume_continues_the_plan_and_keeps_finished_steps(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        calls = []
        def stop_in_step_two(run):
            calls.append(run)
            if len(calls) == 2:
                raise RuntimeError('SYNTHETIC_WORKER_DIED')
        PR.build(product['id'], runner=Agents(on_codex=stop_in_step_two), simulated=True)
        stopped = PR.load(product['id'])
        self.assertEqual([s['state'] for s in stopped['steps']], ['done', 'failed'])
        stopped.update(state='approved', resume_plan=True)
        PR.save(stopped)
        again = Agents()
        result = PR.build(product['id'], runner=again, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result.get('reason'))
        self.assertEqual(again.prompts, [])                                 # no new plan
        self.assertEqual(again.codex, 1)                                    # only the unfinished step
        self.assertEqual([s['state'] for s in PR.load(product['id'])['steps']], ['done', 'done'])

    def test_f12_a_name_is_one_line_and_persian_names_still_work(self):
        for bad in ('Mug Shop', 'Mug Shop', 'Mug\u0085Shop', 'Mug\nShop'):
            with self.subTest(name=repr(bad)), self.assertRaisesRegex(ValueError, 'PRODUCT_NAME'):
                PR.create(bad, 'A small shop that sells mugs online.', max_steps=3)
        persian = PR.create('فروشگاه‌من', 'A small shop that sells mugs online.', max_steps=3)
        self.assertIn('‌', persian['name'])                             # the zero-width non-joiner is kept

    def test_f8_the_review_evidence_is_measured_and_the_change_itself_never_cut(self):
        diff = 'd' * 300000
        handoff = {'task': 't', 'diff': diff, 'test_evidence': {'exit_code': 1, 'output': 'o' * 200000},
                   'previous_findings': {'test_evidence': {'output': 'x' * 100000},
                                         'review': {'findings': [{'severity': 'medium', 'explanation': 'e' * 1500}] * 20}}}
        F = PR.F
        F.fit_review_base(handoff)
        self.assertEqual(handoff['diff'], diff)
        self.assertLessEqual(len(json.dumps(handoff)), F.REVIEW_CONTEXT_CHARS)
        self.assertIn('shortened by Dani Agent', handoff['test_evidence']['output'])
        with self.assertRaisesRegex(RuntimeError, 'PROJECT_CHANGE_TOO_LARGE_TO_REVIEW'):
            F.fit_review_base({'task': 't', 'diff': 'd' * (F.REVIEW_HARD_CHARS + 1)})

    def test_f2_a_build_that_pauses_records_when_to_try_again(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        with patch.object(PR.F, 'execute', side_effect=PR.RetryLater('QUOTA_EXHAUSTED', 900)):
            result = PR.build(product['id'], runner=Agents(), simulated=True)
        paused = PR.load(product['id'])
        self.assertEqual((paused['state'], paused['reason']), ('stopped', 'PRODUCT_STEP_1_QUOTA_EXHAUSTED'))
        self.assertGreater(PR.datetime.datetime.fromisoformat(paused['resume_after']), PR.datetime.datetime.now(PR.datetime.UTC))
        self.assertEqual(result['reason'], 'PRODUCT_STEP_1_QUOTA_EXHAUSTED')

    def test_interrupted_polish_returns_only_unfinished_notes_for_automatic_resume(self):
        product = {'build_mode': 'polish', 'state': 'building', 'steps': [
            {'state': 'done', 'review': {'findings': [
                {'severity': 'low', 'file': 'public/cart.mjs', 'line': None, 'explanation': 'Handle empty input.'},
                {'severity': 'low', 'file': 'public/cart.mjs', 'line': None, 'explanation': 'Explain rounding.'}]}}
        ], 'resume_after': '2999-01-01T00:00:00+00:00'}
        keys = [PR.note_key(note) for note in PR.open_notes(product)]
        product['auto_polish_tried'] = list(keys)  # pending was removed when the worker started
        product['steps'].extend([
            {'state': 'done', 'polish': True, 'handled_notes': [keys[0]]},
            {'state': 'retrying', 'polish': True}])
        PR.end_request(product, 'PRODUCT_STEP_3_QUOTA_EXHAUSTED')
        self.assertEqual(product['state'], 'ready')  # the previous finished revision stays available
        self.assertEqual(product['auto_polish_tried'], [keys[0]])
        self.assertEqual([PR.note_key(note) for note in PR.open_notes(product)], [keys[1]])
        self.assertEqual(product['resume_after'], '2999-01-01T00:00:00+00:00')

    def test_returning_interrupted_polish_notes_keeps_the_human_cancel_flag(self):
        product = {'build_mode': 'polish', 'state': 'building', 'cancelled_by_user': True,
                   'steps': [{'state': 'done', 'review': {'findings': [
                       {'severity': 'low', 'file': 'public/cart.mjs', 'explanation': 'Handle empty input.'}]}}]}
        product['auto_polish_tried'] = [PR.note_key(note) for note in PR.open_notes(product)]
        PR.end_request(product, 'CANCELLED_BY_USER')
        self.assertEqual(product['auto_polish_tried'], [])
        self.assertTrue(product['cancelled_by_user'])  # dashboard will not schedule these returned notes
        self.assertNotIn('polish_failures', product)
        self.assertNotIn('resume_after', product)

    def test_one_approval_is_bound_to_the_product_budget(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=4)
        store = Store(self.root / 'state.sqlite3')
        task_id = PR.request_build(store, product['id'])
        self.assertEqual(PR.request_build(store, product['id']), task_id)   # idempotent while pending
        task = store.get(task_id)
        self.assertEqual(task['state'], 'awaiting_approval')
        limits = approval_scope(task)['limits']
        settings = product['budget']['settings']
        self.assertEqual(limits['codex'], 4 * settings['max_codex_sessions'])
        self.assertEqual(limits['claude'], 1 + 4 * settings['max_claude_sessions'])
        self.assertGreater(PR.ceiling(product['budget']), 4 * M.workflow_ceiling(settings))


class StepBaselineTests(ProductCase):
    """2026-10-21 'test1': the step preflight ran the step's still-empty new node:test file and stopped."""

    def test_baseline_tests_exclude_the_steps_empty_new_test_files(self):
        product = PR.create('Hi', 'Ask for a name, then greet that person.', max_steps=3)
        summary = P.discover(Path(product['app']))
        run = W.prepare(summary, 'Greeting', ['public/lib/greet.mjs', 'public/lib/greet.test.mjs'], 'node-test',
                        dict(M.DEFAULTS))
        self.assertEqual((run / 'repo/public/lib/greet.test.mjs').read_text(), '')
        baseline, full = W.commands(run, baseline=True)['tests'], W.commands(run)['tests']
        self.assertIn('./server.test.mjs', baseline)
        self.assertNotIn('./public/lib/greet.test.mjs', baseline)
        self.assertIn('./public/lib/greet.test.mjs', full)

    def test_a_step_stopped_before_its_agents_is_marked_failed(self):
        product = PR.create('Hi', 'Ask for a name, then greet that person.', max_steps=3)
        with patch.object(W, 'prepare', side_effect=RuntimeError('SYNTHETIC_PREFLIGHT_STOP')):
            result = PR.build(product['id'], runner=Agents(), simulated=True)
        self.assertEqual(result['reason'], 'PRODUCT_STEP_1_SYNTHETIC_PREFLIGHT_STOP')
        steps = PR.load(product['id'])['steps']
        self.assertEqual([s['state'] for s in steps], ['failed', 'pending'])
        self.assertEqual(steps[0]['reason'], 'SYNTHETIC_PREFLIGHT_STOP')


class BlockedDeveloperTests(ProductCase):
    """2026-10-21 'test2': Codex stopped because its command tool could not start; show why, in plain terms."""

    def blocked_agents(self, summary):
        fake = Agents()
        original = fake.__call__
        def call(spec, folder, **options):
            if spec['kind'] == 'codex':
                fake.codex += 1
                return {'exit_code': 0, 'events': [{'type': 'turn.completed'}, {'type': 'final', 'text': json.dumps(
                    {'status': 'blocked', 'changed_files': [], 'summary': summary})}]}
            return original(spec, folder, **options)
        return call

    def test_command_tool_failure_is_classified_and_explained(self):
        product = PR.create('Hi', 'Ask for a name, then greet that person.', max_steps=3)
        summary = 'The command runner failed before reading files: it could not resolve its PowerShell executable.'
        result = PR.build(product['id'], runner=self.blocked_agents(summary), simulated=True)
        self.assertEqual(result['reason'], 'PRODUCT_STEP_1_PROJECT_DEVELOPER_BLOCKED_COMMAND_TOOL_UNAVAILABLE')
        step = PR.load(product['id'])['steps'][0]
        self.assertEqual((step['state'], step['developer_summary']), ('failed', summary))

    def test_other_blocks_keep_the_developer_explanation(self):
        product = PR.create('Hi', 'Ask for a name, then greet that person.', max_steps=3)
        result = PR.build(product['id'], runner=self.blocked_agents('The task needs a database I may not add.'), simulated=True)
        self.assertEqual(result['reason'], 'PRODUCT_STEP_1_PROJECT_DEVELOPER_BLOCKED')
        self.assertIn('database', PR.load(product['id'])['steps'][0]['developer_summary'])


class IntegrationSafetyTests(ProductCase):
    """CODEX_REVIEW.md 2026-10-21 findings 1-3 and 6."""

    def candidate(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        app = Path(product['app'])
        run = self.root / ('run_' + uuid.uuid4().hex[:6])
        files = {'public/lib/new.mjs': b'export const x = 1;\n', 'README.md': b'# candidate readme\n'}
        for name, content in files.items():
            (run / 'repo' / name).parent.mkdir(parents=True, exist_ok=True)
            (run / 'repo' / name).write_bytes(content)
        report = {'changed_files': list(files), 'candidate_sha': 'c' * 40,
                  'candidate_inventory': {n: hashlib.sha256(c).hexdigest() for n, c in files.items()}}
        plan = {'project': {'original_sha': P.git(app, 'rev-parse', 'HEAD')}}
        return app, run, report, plan, (app / 'README.md').read_bytes()

    def assert_untouched(self, app, readme, head):
        self.assertEqual((app / 'README.md').read_bytes(), readme)
        self.assertFalse((app / 'public/lib').exists())              # nor the folder it created
        self.assertEqual((P.git(app, 'rev-parse', 'HEAD'), P.git(app, 'status', '--porcelain')), (head, ''))
        self.assertFalse(PR.journal_path(app).exists())

    def test_a_failed_write_part_way_restores_the_product(self):
        app, run, report, plan, readme = self.candidate()
        original, calls = Path.write_bytes, []
        def flaky(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('disk full')
            return original(path, data)
        with patch.object(Path, 'write_bytes', flaky), self.assertRaises(OSError):
            PR.integrate(app, run, report, plan, 'Step 1')
        self.assert_untouched(app, readme, plan['project']['original_sha'])

    def test_a_worker_killed_part_way_is_recovered_by_the_next_build(self):
        app, run, report, plan, readme = self.candidate()
        original, calls = Path.write_bytes, []
        def killed(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt   # stands in for the job object ending the worker
            return original(path, data)
        with patch.object(Path, 'write_bytes', killed), patch.object(PR, 'recover_integration'), \
                self.assertRaises(KeyboardInterrupt):
            PR.integrate(app, run, report, plan, 'Step 1')
        self.assertTrue(PR.journal_path(app).exists())
        self.assertTrue((app / 'public/lib/new.mjs').exists())        # left half-written...
        PR.recover_integration(app)                                    # ...until build() starts again
        self.assert_untouched(app, readme, plan['project']['original_sha'])

    def test_a_kill_between_staging_and_replacing_leaves_no_half_written_file(self):
        app, run, report, plan, readme = self.candidate()
        original, calls = PR.os.replace, []
        def killed(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise KeyboardInterrupt   # the worker ends with README's candidate staged, not yet in place
            return original(source, target)
        with patch.object(PR.os, 'replace', killed), patch.object(PR, 'recover_integration'), \
                self.assertRaises(KeyboardInterrupt):
            PR.integrate(app, run, report, plan, 'Step 1')
        self.assertEqual((app / 'README.md').read_bytes(), readme)            # whole old file, never partial
        self.assertTrue(PR.staging(app / 'README.md').exists())
        self.assertIn('original_sha', json.loads(PR.journal_path(app).read_text()))   # complete journal
        PR.recover_integration(app)
        self.assert_untouched(app, readme, plan['project']['original_sha'])   # staging file removed too

    def test_a_human_edit_during_integration_is_kept_not_overwritten(self):
        app, run, report, plan, readme = self.candidate()
        original = PR.blob
        def edit_then_blob(*args):
            (app / 'README.md').write_bytes(b'# human edit\n')   # after the clean-status check
            return original(*args)
        with patch.object(PR, 'blob', edit_then_blob), self.assertRaises(RuntimeError) as caught:
            PR.integrate(app, run, report, plan, 'Step 1')
        self.assertEqual(str(caught.exception), 'PRODUCT_CHANGED_OUTSIDE_DANI_AGENT_STOPPED')
        self.assertEqual((app / 'README.md').read_bytes(), b'# human edit\n')
        self.assertFalse((app / 'public/lib/new.mjs').exists())
        self.assertEqual(P.git(app, 'rev-parse', 'HEAD'), plan['project']['original_sha'])

    def test_a_clean_integration_commits_and_leaves_no_journal(self):
        app, run, report, plan, _ = self.candidate()
        head = PR.integrate(app, run, report, plan, 'Step 1')
        self.assertNotEqual(head, plan['project']['original_sha'])
        self.assertEqual(P.git(app, 'status', '--porcelain'), '')
        self.assertFalse(PR.journal_path(app).exists())

    def test_reconcile_never_overwrites_a_newer_ready_product(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        store = Store(self.root / 'state.sqlite3')
        task_id = PR.request_build(store, product['id'])
        stale = PR.load(product['id'])
        stale.update(state='building', steps=[{'title': 'x', 'files': [], 'state': 'running'}])
        ready = dict(stale, state='ready', revision='r' * 40, steps=[{'title': 'x', 'files': [], 'state': 'done'}])
        PR.save(ready)                                  # the worker's last save...
        store.cancel_pending(task_id)                   # ...then the task reaches a final state
        with patch.object(PR, 'products', return_value=[stale]):   # read by reconcile before both
            PR.reconcile(store)
        self.assertEqual((PR.load(product['id'])['state'], PR.load(product['id'])['revision']), ('ready', 'r' * 40))

    def test_an_approval_outdated_by_an_update_pauses_the_product_for_automatic_resume(self):
        # CODEX_REVIEW_3 F3: it used to wait for another click. The stale approval is never renewed.
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        store = Store(self.root / 'state.sqlite3')
        task_id = PR.request_build(store, product['id'])
        approved = PR.load(product['id'])
        approved['state'] = 'approved'                  # task went back to awaiting_approval (code changed)
        PR.save(approved)
        PR.reconcile(store)
        paused = PR.load(product['id'])
        self.assertEqual((paused['state'], paused['reason']), ('stopped', 'APPROVAL_OUTDATED_BY_DANI_AGENT_UPDATE'))
        self.assertEqual(store.get(task_id)['state'], 'cancelled')

    def test_new_files_use_the_existing_folder_casing(self):
        summary = {'files': [{'path': 'public/index.html'}]}
        self.assertEqual(W.select_scope(summary, ['Public/new.mjs', 'PUBLIC/Lib/a.mjs', 'public/lib/b.mjs']),
                         ['public/new.mjs', 'public/Lib/a.mjs', 'public/Lib/b.mjs'])
        with self.assertRaises(ValueError):
            W.select_scope(summary, ['public/INDEX.html'])   # an existing file keeps its exact name


class FolderAndModelTests(ProductCase):
    def test_the_code_folder_is_named_after_the_product(self):
        for name, folder in (('Mug Shop!', 'mug-shop'), ('CON', 'con-app'), ('###', 'product')):
            with self.subTest(name=name):
                product = PR.create(name, 'A small shop that sells mugs online.', max_steps=3)
                self.assertEqual(PR.app_dir(product), PR.PRODUCTS / folder)
                self.assertEqual(Path(product['app']).name, folder)

    def legacy(self, product, code_name):
        """Turn a product into the layout before 2026-10-22: products/<id>/{product.json, <code_name>}."""
        old = self.root / product['id']
        old.mkdir()
        os.rename(PR.app_dir(product), old / code_name)
        record = PR.load(product['id'])
        del record['folder']
        (old / 'product.json').write_text(json.dumps(record))
        (old / 'preview.log').write_text('log')
        shutil.rmtree(PR.folder(product['id']))
        return old

    def test_products_in_the_old_layout_move_to_products_slash_name(self):
        for code_name in ('old-shop', 'app'):
            with self.subTest(code_name=code_name):
                product = PR.create('Old Shop', 'A small shop that sells mugs online.', max_steps=3)
                old = self.legacy(product, code_name)
                self.assertNotIn(product['id'], [p['id'] for p in PR.products()])   # not read from code-level folders
                PR.migrate_layout()
                moved = PR.load(product['id'])
                self.assertEqual(PR.app_dir(moved), self.root / moved['folder'])
                self.assertTrue(P.discover(PR.app_dir(moved))['eligible'])
                self.assertEqual((PR.folder(product['id']) / 'preview.log').read_text(), 'log')
                self.assertFalse(old.exists())
                shutil.rmtree(PR.app_dir(moved), onerror=lambda f, p, _: (os.chmod(p, 0o666), f(p)))
                shutil.rmtree(PR.folder(product['id']))

    def test_an_interrupted_move_resumes_on_the_next_start(self):
        for failing in ('code', 'records'):
            with self.subTest(failing=failing):
                product = PR.create('Old Shop', 'A small shop that sells mugs online.', max_steps=3)
                old = self.legacy(product, 'old-shop')
                real, calls = os.rename, []
                def flaky(source, target):
                    calls.append(Path(source).name)
                    if (failing == 'code' and Path(source).name == 'old-shop') or (failing == 'records' and Path(source).name == 'preview.log'):
                        raise PermissionError('locked by antivirus')
                    return real(source, target)
                with patch.object(PR.os, 'rename', flaky):
                    PR.migrate_layout()                                         # interrupted part-way
                self.assertIn(product['id'], [p['id'] for p in PR.products()])  # never lost from the dashboard
                PR.migrate_layout()                                             # the next start finishes it
                moved = PR.load(product['id'])
                self.assertTrue(P.discover(PR.app_dir(moved))['eligible'])
                self.assertEqual((PR.folder(product['id']) / 'preview.log').read_text(), 'log')
                self.assertFalse(old.exists())
                shutil.rmtree(PR.app_dir(moved), onerror=lambda f, p, _: (os.chmod(p, 0o666), f(p)))
                shutil.rmtree(PR.folder(product['id']))

    def test_a_new_style_code_folder_is_never_taken_apart(self):
        product = PR.create('mugshopstore', 'A small shop that sells mugs online.', max_steps=3)   # a 12-character name
        app = PR.app_dir(product)
        (app / 'app').mkdir()
        (app / 'product.json').write_text(json.dumps(dict(product, id='mugshopstore')))   # generated code
        PR.migrate_layout()
        self.assertTrue((app / 'product.json').is_file() and (app / 'app').is_dir())
        self.assertFalse((PR.folder('mugshopstore')).exists())

    def test_quota_running_out_during_a_polish_keeps_the_product_ready(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=2)
        PR.build(product['id'], runner=Agents(notes=1), simulated=True)
        store = Store(self.root / 'state.sqlite3')
        PR.request_build(store, product['id'], mode='polish')
        with patch.object(PR.F, 'execute', side_effect=RuntimeError('QUOTA_EXHAUSTED')):
            result = PR.build(product['id'], runner=Agents(), simulated=True, mode='polish')
        after = PR.load(product['id'])
        # A real build waits for the quota (never counted as a failed attempt); a simulated one ends after its attempt.
        self.assertEqual((result['verdict'], result['reason'], after['state'], after['reason']),
                         ('PASS', 'PRODUCT_POLISH_STOPPED', 'ready', None))
        self.assertIn('QUOTA_EXHAUSTED', after['polish_reason'])

    def test_same_names_get_their_own_folders_and_a_code_folder_is_never_a_record(self):
        first = PR.create('Mug Shop', 'A small shop that sells mugs online.', max_steps=3)
        second = PR.create('Mug Shop', 'A small shop that sells mugs online.', max_steps=3)
        self.assertEqual((first['folder'], second['folder']), ('mug-shop', 'mug-shop-2'))
        (PR.app_dir(first) / 'product.json').write_text(json.dumps(dict(first, id='aaaaaaaaaaaa', name='spoof')))
        self.assertEqual(sorted(p['name'] for p in PR.products()), ['Mug Shop', 'Mug Shop'])
        tampered = dict(first, folder='../outside')
        with self.assertRaisesRegex(RuntimeError, 'FOLDER_NAME_INVALID'):
            PR.app_dir(tampered)

    def test_live_status_names_the_model_and_effort_at_work(self):
        # The planner uses the approved Claude effort, not the reviewer profile's fixed one (low).
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3,
                            settings={**M.DEFAULTS, 'mode': 'manual', 'claude_effort': 'high'})
        seen, argvs = [], []
        agents = Agents()
        def runner(spec, folder, **kwargs):
            argvs.append(spec['argv'])
            return agents(spec, folder, **kwargs)
        PR.build(product['id'], runner=runner, simulated=True, status=lambda phase, **k: seen.append(dict(k, phase=phase)))
        settings = product['budget']['settings']
        planning = next(s for s in seen if s['phase'] == 'product_planning' and s.get('model'))
        self.assertEqual((planning['agent'], planning['model'], planning['effort']), ('Claude', 'sonnet', 'high'))
        self.assertEqual(argvs[0][argvs[0].index('--effort') + 1], 'high')
        roles = {s['role']: (s['agent'], s['model'], s['effort']) for s in seen if s['phase'] == 'product_step' and 'role' in s}
        self.assertEqual(roles['codex'], ('Codex', settings['codex_model'], settings['codex_effort']))
        self.assertEqual(roles['claude'], ('Claude', settings['claude_model'], settings['claude_effort']))
        self.assertEqual(roles['tests'], (None, None, None))

    def test_the_job_stall_check_sees_every_session_of_a_build(self):
        # Review 7476198e: the status never named the run, so a Codex session working for over 30 minutes looked
        # like a stuck job (only status.json was watched) and the whole build would have been stopped.
        from dani_agent import coordinator as C
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        seen = []
        PR.build(product['id'], runner=Agents(), simulated=True, status=lambda phase, **k: seen.append(dict(k, phase=phase)))
        working = [s for s in seen if s.get('role') or (s['phase'] == 'product_planning' and s.get('model'))]
        self.assertTrue(working)
        self.assertTrue(all(s.get('run') for s in working), [s for s in working if not s.get('run')])
        steps = {row['run'] for row in PR.load(product['id'])['steps'] if row.get('run')}
        self.assertTrue(steps and steps <= {s['run'] for s in working})
        job = self.root / 'job'
        job.mkdir()
        step = next(s for s in working if s.get('role') == 'codex')
        (job / 'status.json').write_text(json.dumps(step))
        journal = Path(step['run']) / 'artifacts' / 'later.progress.jsonl'
        journal.write_text(json.dumps({'output_bytes': 10, 'phase': 'running'}) + '\n')
        before = C.job_activity(job)
        with journal.open('a') as more:                     # a session still producing output, long after the status
            more.write(json.dumps({'output_bytes': 5000, 'phase': 'running'}) + '\n')
        self.assertNotEqual(C.job_activity(job), before)

    def test_a_fault_in_dani_agent_is_labelled_and_keeps_its_traceback(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        agents = Agents()
        def runner(spec, folder, **kwargs):
            if spec.get('kind') == 'codex':
                b'\xb7'.decode('utf-8')                     # an unexpected exception inside Dani Agent's code
            return agents(spec, folder, **kwargs)
        result = PR.build(product['id'], runner=runner, simulated=True)
        self.assertEqual(result['reason'], 'PRODUCT_STEP_1_INTERNAL_ERROR_UnicodeDecodeError')
        self.assertIn('UnicodeDecodeError', PR.load(product['id'])['steps'][0]['traceback'])
        self.assertEqual(PR.load(product['id'])['reason'], 'PRODUCT_STEP_1_INTERNAL_ERROR_UnicodeDecodeError')
        from dani_agent import project_workflow as F
        for exc, expected in ((RuntimeError('PROJECT_TESTS_FAILED'), 'PROJECT_TESTS_FAILED'),
                              (ValueError('CLEAN_SUPPORTED_PROJECT_AND_TASK_REQUIRED'), 'CLEAN_SUPPORTED_PROJECT_AND_TASK_REQUIRED'),
                              (RuntimeError('PRODUCT_STEP_3_INTERNAL_ERROR_KeyError'), 'PRODUCT_STEP_3_INTERNAL_ERROR_KeyError'),
                              (RuntimeError('UnicodeDecodeError'), 'INTERNAL_ERROR_UnicodeDecodeError'),   # passed on by process.run
                              (KeyError('x'), 'INTERNAL_ERROR_KeyError'), (RuntimeError(''), 'INTERNAL_ERROR_RuntimeError')):
            with self.subTest(exc=exc):
                self.assertEqual(F.stop_reason(exc), expected)

    def test_a_hung_job_is_not_kept_alive_by_journals_that_are_only_rewritten(self):
        # Review 7d50d9b4: the wrapper rewrites its own journal about twice a second while it lives. Counting file
        # times made a hung worker look busy forever; only recorded progress counts.
        from dani_agent import coordinator as C
        job = self.root / 'job'
        job.mkdir()
        (job / 'status.json').write_text(json.dumps({'phase': 'product_step'}))
        wrapper = job / 'worker.progress.jsonl'
        wrapper.write_text(json.dumps({'output_bytes': 0, 'phase': 'running'}) + '\n')
        before = C.job_activity(job)
        wrapper.write_text(json.dumps({'output_bytes': 0, 'phase': 'running'}) + '\n')   # rewritten, no progress
        os.utime(wrapper, (time.time() + 60, time.time() + 60))
        self.assertEqual(C.job_activity(job), before)
        (job / 'offline_tests.log').write_text('ok\n')                                   # a log that grows is progress
        self.assertNotEqual(C.job_activity(job), before)

    def test_each_step_gets_its_model_and_effort_from_its_difficulty(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)   # automatic by default
        self.assertEqual(product['budget']['settings']['mode'], 'auto')
        seen, argvs, agents = [], [], Agents()
        def runner(spec, folder, **kwargs):
            argvs.append(spec['argv'])
            return agents(spec, folder, **kwargs)
        strongest = {'codex': 'gpt-strong', 'claude': 'opus'}
        with patch.object(M, 'usable', lambda role: [strongest[role], M.DEFAULTS[role + '_model']]), \
                patch.object(M, 'verified_models', lambda role: {strongest[role], M.DEFAULTS[role + '_model']}):
            result = PR.build(product['id'], runner=runner, simulated=True, status=lambda phase, **k: seen.append(dict(k, phase=phase)))
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        planning = next(s for s in seen if s['phase'] == 'product_planning' and s.get('model'))
        self.assertEqual((planning['model'], planning['effort']), ('sonnet', 'medium'))   # planning: normal
        steps = PR.load(product['id'])['steps']
        self.assertEqual([s['difficulty'] for s in steps], ['normal', 'hard'])
        self.assertEqual(steps[0]['models'], {'codex': {'model': M.DEFAULTS['codex_model'], 'effort': 'medium'},
                                              'claude': {'model': 'sonnet', 'effort': 'medium'}})
        self.assertEqual(steps[1]['models'], {'codex': {'model': 'gpt-strong', 'effort': 'high'},
                                              'claude': {'model': 'opus', 'effort': 'high'}})
        codex = [a for a in argvs if '--output-schema' in a]
        self.assertIn('model_reasoning_effort="high"', ' '.join(codex[-1]))   # the hard step really ran at high

    def test_a_refused_planner_model_is_recorded_and_explained(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        def refusing(spec, folder, **kwargs):
            return {'exit_code': 1, 'error': 'The model sonnet does not exist or you do not have access to it.',
                    'prompt_sent': True, 'initialization': {'tools': [], 'mcp_servers_count': 0}, 'events': []}
        with patch.object(M, 'record_probe') as recorded:
            result = PR.build(product['id'], runner=refusing, simulated=True)
        self.assertEqual(result['reason'], 'MODEL_NOT_IN_YOUR_PLAN_CLAUDE')
        # Recorded each time it is refused; a real build's next attempt then picks another model (here it is patched).
        self.assertEqual(recorded.call_count, PR.PLAN_ATTEMPTS)
        self.assertEqual(recorded.call_args[0][:3], ('claude', 'sonnet', False))

    def test_a_plan_without_a_valid_difficulty_is_refused(self):
        product = PR.create('Demo', 'A small shop that sells mugs online.', max_steps=3)
        plan = {'steps': [dict(PLAN['steps'][0], difficulty='extreme')]}
        result = PR.build(product['id'], runner=Agents(plan), simulated=True)
        self.assertIn(result['reason'], ('PRODUCT_PLAN_STEP_DIFFICULTY_INVALID', 'PRODUCT_PLAN_INVALID_JSON'))


class NoTimeLimitBuildTests(ProductCase):
    """2026-10-23 StockPilot: no product stops because of a time limit; a stuck session is restarted by itself."""

    def test_sessions_run_without_a_time_limit(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        seen, agents = [], Agents()
        def runner(spec, folder, **options):
            seen.append((spec['kind'], options.get('timeout'), options.get('idle')))
            return agents(spec, folder, **options)
        self.assertEqual(PR.build(product['id'], runner=runner, simulated=True)['verdict'], 'PASS')
        self.assertTrue(seen and all(timeout is None and idle for _, timeout, idle in seen))

    def test_a_stuck_codex_session_is_restarted_by_itself(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        agents, stalls = Agents(), []
        def runner(spec, folder, **options):
            if spec['kind'] == 'codex' and not stalls:
                stalls.append(1)
                raise RuntimeError('STALLED')
            return agents(spec, folder, **options)
        result = PR.build(product['id'], runner=runner, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        self.assertEqual(PR.load(product['id'])['steps'][0]['sessions']['codex'], 2)   # the stuck one, then the restart

    def test_hanging_product_tests_become_a_correction_not_a_stop(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        agents, hangs = Agents(), []
        def runner(spec, folder, **options):
            if spec['kind'] == 'plain' and spec.get('test_completion_nonce') and not hangs:
                hangs.append(1)
                raise RuntimeError('STALLED')
            return agents(spec, folder, **options)
        result = PR.build(product['id'], runner=runner, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        first = PR.load(product['id'])['steps'][0]
        self.assertEqual(first['sessions']['codex'], 2)                   # Codex was asked to fix the hang


class PolishTests(ProductCase):
    """2026-10-22: Codex acts on the review notes it agrees with, in a final polish step."""

    def test_failures_before_polish_attempts_keep_growing_pauses_across_workers(self):
        # Both failures happen before keep_trying() can record an attempt. They must leave the notes resumable,
        # but never cause another automatic task on each five-second dashboard round.
        for failing in ('recover_integration', 'safe_target'):
            with self.subTest(failing=failing):
                product = PR.create('Polish Recovery', 'A product whose review notes need polishing.', max_steps=2)
                product.update(state='ready', steps=[
                    {'state': 'done', 'review': {'findings': [
                        {'severity': 'low', 'file': 'server.mjs', 'explanation': 'Handle empty input.'}]}},
                    {'state': 'done', 'polish': True, 'handled_notes': []}])
                PR.save(product)
                original_steps = product['steps']
                keys = [PR.note_key(n) for n in PR.open_notes(product)]
                store = Store(self.root / (failing + '.sqlite3'))
                fake = Agents()
                for failures, expected_wait in enumerate((30, 120, 300), 1):
                    # These records are reloaded just as a fresh dashboard and worker would load them.
                    record = PR.load(product['id'])
                    record.update(auto_polish_tried=keys, auto_polish_pending=keys)
                    PR.save(record)
                    PR.request_build(store, product['id'], mode='polish')
                    started = PR.datetime.datetime.now(PR.datetime.UTC)
                    with patch.object(PR, failing, side_effect=RuntimeError('SYNTHETIC_POLISH_SETUP_FAILED')):
                        result = PR.build(product['id'], runner=fake, simulated=True, mode='polish')
                    ended = PR.datetime.datetime.now(PR.datetime.UTC)
                    saved = PR.load(product['id'])
                    self.assertEqual((result['verdict'], result['reason']), ('FAIL', 'SYNTHETIC_POLISH_SETUP_FAILED'))
                    self.assertEqual((saved['state'], saved['polish_failures']), ('ready', failures))
                    resume = PR.datetime.datetime.fromisoformat(saved['resume_after'])
                    self.assertGreaterEqual(resume, started + PR.datetime.timedelta(seconds=expected_wait))
                    self.assertLessEqual(resume, ended + PR.datetime.timedelta(seconds=expected_wait))
                    self.assertEqual(saved['auto_polish_tried'], [])
                    self.assertEqual([PR.note_key(n) for n in PR.open_notes(saved)], keys)
                    self.assertEqual(saved['steps'], original_steps)
                    self.assertNotIn('polish_attempts', saved)
                self.assertEqual((fake.codex, fake.claude), (0, 0))
                self.assertEqual(P.git(PR.app_dir(saved), 'rev-parse', 'HEAD'), product['revision'])

    def test_outer_polish_backoff_keeps_quota_minimum_and_caps_at_one_hour(self):
        for reason, failures, expected_wait in (('QUOTA_EXHAUSTED', 0, PR.QUOTA_WAIT),
                                                 ('SYNTHETIC_WORKER_FAILED', 99, 3600)):
            with self.subTest(reason=reason):
                product = {'build_mode': 'polish', 'state': 'building', 'polish_failures': failures,
                    'resume_after': '2000-01-01T00:00:00+00:00', 'steps': [
                        {'state': 'done', 'review': {'findings': [
                            {'severity': 'low', 'file': 'server.mjs', 'explanation': 'Handle empty input.'}]}}]}
                started = PR.datetime.datetime.now(PR.datetime.UTC)
                PR.end_request(product, reason)
                ended = PR.datetime.datetime.now(PR.datetime.UTC)
                resume = PR.datetime.datetime.fromisoformat(product['resume_after'])
                self.assertGreaterEqual(resume, started + PR.datetime.timedelta(seconds=expected_wait))
                self.assertLessEqual(resume, ended + PR.datetime.timedelta(seconds=expected_wait))
                self.assertEqual(product['polish_failures'], failures + 1)

    def interrupted_polish(self):
        product = PR.create('Resume Polish', 'A small shop that sells mugs online.', max_steps=2)
        PR.build(product['id'], runner=Agents(notes=1), simulated=True)
        store = Store(self.root / 'state.sqlite3')
        PR.request_build(store, product['id'], mode='polish')
        def yield_after_failure(kind, detail):
            if kind == 'product_retry':
                raise PR.RetryLater(detail['reason'], 900)
        result = PR.build(product['id'], runner=Agents(polish_fails=True), simulated=True,
                          mode='polish', event=yield_after_failure)
        self.assertEqual(result['verdict'], 'FAIL')
        saved = PR.load(product['id'])
        self.assertEqual(saved['steps'][-1]['state'], 'retrying')
        self.assertEqual(len(saved['steps'][-1]['attempts']), 1)
        self.assertEqual(saved['polish_failures'], 1)
        return saved, store, yield_after_failure

    @patch.object(PR, 'SIMULATED_ATTEMPTS', 3)
    def test_yielded_polish_resumes_same_pass_and_finishes_without_retrying_rows(self):
        product, store, _ = self.interrupted_polish()
        history = product['steps'][-1]['attempts']
        PR.request_build(store, product['id'], mode='polish')
        result = PR.build(product['id'], runner=Agents(), simulated=True, mode='polish')
        built = PR.load(product['id'])
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        self.assertEqual([row['state'] for row in built['steps']], ['done', 'done', 'done'])
        self.assertEqual(built['steps'][-1]['attempts'], history)
        self.assertEqual(PR.open_notes(built), [])
        self.assertNotIn('polish_failures', built)

    @patch.object(PR, 'SIMULATED_ATTEMPTS', 3)
    def test_yielded_polish_does_not_reset_its_bounded_failure_count(self):
        product, store, yield_after_failure = self.interrupted_polish()
        fake = Agents(polish_fails=True)
        for _ in range(PR.SIMULATED_ATTEMPTS + 1):
            if not PR.open_notes(PR.load(product['id'])):
                break
            PR.request_build(store, product['id'], mode='polish')
            result = PR.build(product['id'], runner=fake, simulated=True, mode='polish', event=yield_after_failure)
        built = PR.load(product['id'])
        self.assertEqual((result['verdict'], result['reason']), ('PASS', 'PRODUCT_POLISH_STOPPED'))
        self.assertEqual([row['state'] for row in built['steps']], ['done', 'done'])
        self.assertEqual(PR.open_notes(built), [])
        self.assertEqual(len(built['polish_attempts']), 1)
        self.assertEqual(len(built['polish_attempts'][0]['attempts']), PR.SIMULATED_ATTEMPTS - 1)
        self.assertNotIn('polish_failures', built)

    def test_review_notes_get_a_polish_step_where_codex_decides(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        fake = Agents(notes=2)
        result = PR.build(product['id'], runner=fake, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        built = PR.load(product['id'])
        self.assertEqual(built['state'], 'ready')
        polish = built['steps'][-1]
        self.assertEqual((polish['title'], polish['state'], polish['polish']), (PR.POLISH_TITLE, 'done', True))
        self.assertEqual(len(polish['notes']), 4)                          # 2 notes on each of 2 steps
        self.assertIn('Note 1: fixed', polish['developer_summary'])        # Codex's own decision per note
        self.assertIn('For EACH note decide yourself whether you agree', fake.objectives[-1])
        self.assertIn('consider handling empty input', fake.objectives[-1])
        self.assertEqual(len(polish['handled_notes']), 4)                  # each note tracked on its own
        self.assertEqual(PR.open_notes(built), [])
        self.assertEqual(len(P.git(Path(built['app']), 'log', '--oneline').splitlines()), 4)   # template + 2 steps + polish

    def test_notes_that_do_not_fit_stay_open_for_the_apps_polish_button(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        fake = Agents(notes=6, note_text='x' * 1000)                       # 12 long notes: not all fit in one task
        PR.build(product['id'], runner=fake, simulated=True)
        built = PR.load(product['id'])
        polish = built['steps'][-1]
        shown = len(polish['handled_notes'])
        self.assertTrue(0 < shown < 12)
        self.assertEqual(len(polish['notes']), shown)
        self.assertEqual(fake.objectives[-1].count('\nNote '), shown)       # exactly the notes Codex was shown
        self.assertEqual(len(PR.open_notes(built)), 12 - shown)             # the rest wait for Polish in the app
        self.assertLessEqual(len(fake.objectives[-1]), 4000)

    def test_polish_passes_repeat_until_every_note_is_handled(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=8)   # room for more passes
        fake = Agents(notes=6, note_text='x' * 1000)                                            # 12 long notes
        result = PR.build(product['id'], runner=fake, simulated=True)
        built = PR.load(product['id'])
        passes = [s for s in built['steps'] if s.get('polish')]
        self.assertEqual((result['verdict'], built['state']), ('PASS', 'ready'))
        self.assertGreater(len(passes), 1)
        handled = [key for p in passes for key in p['handled_notes']]
        self.assertEqual((len(handled), len(set(handled))), (12, 12))         # every note exactly once
        self.assertEqual(PR.open_notes(built), [])
        self.assertLessEqual(len(built['steps']), 8)                           # never beyond the approved ceiling

    def test_a_note_that_can_never_be_polished_is_set_aside(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        PR.build(product['id'], runner=Agents(), simulated=True)
        record = PR.load(product['id'])
        record['steps'][0]['review'] = {'verdict': 'pass', 'findings': [
            {'severity': 'low', 'file': 'public/logo.png', 'line': None, 'explanation': 'Compress the logo.'},
            {'severity': 'low', 'file': 'public/lib/cart.mjs', 'line': None, 'explanation': 'Round totals.'}]}
        PR.save(record)
        store = Store(self.root / 'state.sqlite3')
        PR.request_build(store, product['id'], mode='polish')
        fake = Agents()
        PR.build(product['id'], runner=fake, simulated=True, mode='polish')
        built = PR.load(product['id'])
        self.assertIn('Round totals', fake.objectives[-1])
        self.assertNotIn('Compress the logo', fake.objectives[-1])
        self.assertEqual(PR.open_notes(built), [])                          # no Polish button that can never help

    def test_no_notes_no_polish(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        PR.build(product['id'], runner=Agents(), simulated=True)
        self.assertEqual([s['title'] for s in PR.load(product['id'])['steps']], ['Cart logic', 'Cart API'])

    def test_polish_stays_inside_the_approved_step_ceiling(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=2)   # the plan uses both
        PR.build(product['id'], runner=Agents(notes=1), simulated=True)
        built = PR.load(product['id'])
        self.assertEqual(len(built['steps']), 2)
        self.assertEqual(len(PR.open_notes(built)), 2)                     # left for a separately approved polish

    def test_a_failed_polish_keeps_the_finished_product_and_sets_its_notes_aside(self):
        # 2026-10-24: every step of a finished product is done; a polish that cannot pass is not a failed step.
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=3)
        result = PR.build(product['id'], runner=Agents(notes=1, polish_fails=True), simulated=True)
        built = PR.load(product['id'])
        self.assertEqual((result['verdict'], built['state']), ('PASS', 'ready'))
        self.assertEqual([s['state'] for s in built['steps']], ['done', 'done'])
        self.assertEqual(built['polish_attempts'][0]['state'], 'set_aside')
        self.assertTrue(built['polish_reason'])
        self.assertEqual(PR.open_notes(built), [])                          # shown as set aside, not retried forever
        self.assertEqual(len(built['notes_set_aside']), 2)
        self.assertEqual(len(P.git(Path(built['app']), 'log', '--oneline').splitlines()), 3)   # nothing from the polish

    def test_a_finished_product_can_be_polished_on_request(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=2)
        PR.build(product['id'], runner=Agents(notes=1), simulated=True)
        store = Store(self.root / 'state.sqlite3')
        task_id = PR.request_build(store, product['id'], mode='polish')
        self.assertEqual(store.get(task_id)['payload']['mode'], 'polish')   # part of what the approval binds
        self.assertEqual(PR.load(product['id'])['build_mode'], 'polish')
        result = PR.build(product['id'], runner=Agents(), simulated=True, mode='polish')
        self.assertEqual((result['verdict'], result['reason']), ('PASS', 'PRODUCT_POLISHED'))
        built = PR.load(product['id'])
        self.assertEqual((built['state'], built['steps'][-1]['title']), ('ready', PR.POLISH_TITLE))
        with self.assertRaisesRegex(ValueError, 'NOTHING_TO_POLISH'):
            PR.request_build(store, product['id'], mode='polish')

    def test_a_polish_that_was_stopped_leaves_the_product_ready(self):
        product = PR.create('Demo Shop', 'A small shop that sells mugs online.', max_steps=2)
        PR.build(product['id'], runner=Agents(notes=1), simulated=True)
        store = Store(self.root / 'state.sqlite3')
        task_id = PR.request_build(store, product['id'], mode='polish')
        record = PR.load(product['id'])
        record['state'] = 'building'
        PR.save(record)
        store.cancel_pending(task_id)                  # e.g. cancelled or killed
        PR.reconcile(store)
        stopped = PR.load(product['id'])
        self.assertEqual((stopped['state'], stopped['reason']), ('ready', None))
        self.assertTrue(stopped['polish_reason'])


class PythonProductTests(ProductCase):
    PY_PLAN = {'steps': [{'title': 'Budget logic', 'objective': 'Add monthly totals.', 'difficulty': 'simple', 'files': ['budget.py', 'test_budget.py']},
                         {'title': 'CLI', 'objective': 'Wire the totals into main.py.', 'difficulty': 'normal', 'files': ['main.py', 'test_core.py']}]}

    def test_python_product_starts_clean_with_unittest(self):
        product = PR.create('Budget Tool', 'Tracks monthly spending from a CSV file.', product_kind='python')
        self.assertEqual(product['kind'], 'python')
        app = Path(product['app'])
        self.assertTrue((app / 'main.py').is_file() and not (app / 'package.json').exists())
        summary = P.discover(app)
        self.assertTrue(summary['eligible'])
        option = next(o for o in summary['test_options'] if o['id'] == 'python-unittest')
        self.assertEqual(option['baseline_test_files'], 1)

    def test_python_product_builds_with_the_unittest_driver(self):
        product = PR.create('Budget Tool', 'Tracks monthly spending from a CSV file.', product_kind='python')
        fake = Agents(self.PY_PLAN)
        result = PR.build(product['id'], runner=fake, simulated=True)
        self.assertEqual(result['verdict'], 'PASS', result['reason'])
        self.assertIn('Python app / script', fake.prompts[0])
        self.assertIn('budget.py', P.git(Path(product['app']), 'log', '--name-only', '--format='))
        run = Path(PR.load(product['id'])['steps'][0]['run'])
        self.assertEqual(W.load(run)['test']['id'], 'python-unittest')

    def test_python_plans_need_top_level_tests_and_python_files(self):
        for files, reason in ((['budget.py', 'tests/test_budget.py'], 'PRODUCT_PLAN_PYTHON_TESTS_MUST_BE_TOP_LEVEL'),
                              (['public/app.mjs', 'test_budget.py'], 'PRODUCT_PLAN_FILE_TYPE_NOT_ALLOWED_FOR_PRODUCT')):
            with self.subTest(reason=reason):
                product = PR.create('Budget Tool', 'Tracks monthly spending from a CSV file.', product_kind='python')
                fake = Agents({'steps': [{'title': 'x', 'objective': 'y', 'difficulty': 'simple', 'files': files}]})
                self.assertEqual(PR.build(product['id'], runner=fake, simulated=True)['reason'], reason)
                self.assertEqual(fake.codex, 0)

    def test_unknown_product_type_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'UNKNOWN_PRODUCT_TYPE'):
            PR.create('X', 'A product of an unsupported type.', product_kind='desktop')


if __name__ == '__main__':
    unittest.main()
