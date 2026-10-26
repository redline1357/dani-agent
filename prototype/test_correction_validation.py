"""Offline proof of bounded handoff rules, never execution of generated code on host."""
import ast
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import collaboration as W, coordinator as C, correction_fixture as F
from dani_agent.presentation import acceptance_text
from dani_agent.state import POLICIES



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class FixtureAgents:
    def __init__(self, run, fault=None):
        self.run, self.fault = run, fault
        self.developers = self.reviews = 0
        self.prompts, self.review_inputs, self.review_prompts, self.calls = [], [], [], []

    def __call__(self, spec, directory, **options):
        kind = spec['kind']
        self.calls.append(kind)
        if kind == 'claude-inventory':
            return {'initialization': {'tools': [], 'mcp_servers_count': 0}, 'prompt_sent': False}
        if kind == 'codex':
            self.prompts.append(spec['argv'][-1])
            index = self.developers
            self.developers += 1
            if index == 0 and self.fault != 'early_fix':
                source = F.SOURCE.replace('    return', '    """Bound a value using the current behavior."""\n    return')
            else:
                source = 'def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError\n    return min(max(value, lower), upper)\n'
            (self.run / 'repo/toy_math.py').write_text(source, encoding='utf-8')
            options['watch']()
            return {'exit_code': 0, 'events': [{'type': 'turn.completed'}, {'type': 'final', 'text': json.dumps({
                'status': 'implemented', 'changed_files': ['toy_math.py'], 'summary': 'Synthetic provider protocol'})}]}
        if kind == 'plain':
            if 'acceptance_test.py' in ' '.join(spec['argv']):
                bad = self.developers == 1 or self.fault == 'unfixed'
                return {'exit_code': 1 if bad else 0, 'stdout': 'SIMULATED\nRan 5 tests\n' +
                    ('FAILED (failures=1)\nAssertionError: ValueError not raised\n' if bad else 'OK\n')}
            return {'exit_code': 1 if self.fault == 'weak_failure' else 0, 'stdout': 'SIMULATED\nRan 8 tests\nOK\n'}
        if kind == 'claude-review':
            self.review_prompts.append(spec['prompt'])
            data = json.loads(spec['prompt'].split('\nEvidence:\n')[1])
            self.review_inputs.append(data)
            changes = self.reviews == 0 and self.fault != 'missed_bug' or self.fault == 'unresolved_review'
            if self.reviews == 1 and self.fault == 'coverage_context':
                evidence = data.get('acceptance_evidence', {})
                changes = (evidence.get('exit_code') != 0 or evidence.get('candidate_sha') != data['candidate_sha']
                    or data.get('acceptance_test_source') != F.ACCEPTANCE_TEST)
            self.reviews += 1
            value = {'candidate_sha': data['candidate_sha'], 'verdict': 'changes_requested' if changes else 'pass',
                'findings': [{'severity': 'medium', 'file': 'toy_math.py', 'line': 3,
                    'explanation': 'Reversed lower/upper bounds must raise ValueError; the fixed tests miss it.'}] if changes else []}
            if self.fault == 'out_of_scope_label':
                value['findings'].append({'severity': 'medium', 'file': 'tests (fixed_tests)', 'line': None,
                    'explanation': 'The eight tests omit the reversed-bounds requirement.'})
            if self.reviews == 2 and self.fault in ('advisory_pass', 'pass_medium', 'pass_high', 'blocked_low', 'changes_requested_low'):
                # Reduced regression of run 20261018T235140Z_1908c994: Claude
                # approved the guard but kept two LOW, non-blocking observations.
                value['findings'] = [
                    {'severity': 'low', 'file': 'test_toy_math.py', 'line': None,
                        'explanation': 'The fixed eight tests omit reversed bounds; the candidate guard correctly satisfies the contract. Recommend a harness test.'},
                    {'severity': 'low', 'file': 'toy_math.py', 'line': 1,
                        'explanation': 'Types are not validated, as documented; enforcement is not required by the contract and this is acceptable.'}]
                if self.fault in ('pass_medium', 'pass_high'):
                    value['findings'][0]['severity'] = self.fault.removeprefix('pass_')
                elif self.fault in ('blocked_low', 'changes_requested_low'):
                    value['verdict'] = self.fault.removesuffix('_low')
            return {'exit_code': 0, 'initialization': {'tools': [], 'mcp_servers_count': 0}, 'prompt_sent': True,
                'events': [{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': json.dumps(value)}]}
        raise AssertionError(kind)


class CorrectionValidationTests(unittest.TestCase):
    def run_fixture(self, fault=None):
        run = C.APP['prepare'](template=F.TEMPLATE)
        task = {'kind': 'correction_validation', 'state': 'running', 'objective': F.OBJECTIVE,
            'payload': {'template': F.TEMPLATE, 'project_name': 'Synthetic correction proof'}}
        runner = FixtureAgents(run, fault)
        events = []
        result = W.execute(run, task, runner=runner, simulated=True, event=lambda name, value: events.append((name, value)))
        return run, runner, result, events

    def test_trusted_seed_passes_all_eight_weak_tests_but_violates_complete_contract(self):
        # Only coordinator-authored literal code is executed here, never agent output.
        module = types.ModuleType('toy_math')
        exec(F.SOURCE, module.__dict__)
        namespace = {}
        with patch.dict(sys.modules, {'toy_math': module}):
            exec(F.WEAK_TESTS, namespace)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace['ClampTests'])
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertEqual(result.testsRun, 8)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(module.clamp(5, 10, 0), 0)  # Must raise under full contract.
        self.assertFalse(any('reversed' in node.name for node in ast.walk(ast.parse(F.WEAK_TESTS)) if isinstance(node, ast.FunctionDef)))

    def test_one_review_driven_fix_with_independent_before_after_oracle(self):
        run, fake, report, events = self.run_fixture()
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['scope'], 'SIMULATED_CORRECTION_VALIDATION')
        self.assertTrue(report['correction_verified'])
        self.assertEqual(report['agent_sessions'], {'codex': 2, 'claude': 2})
        self.assertEqual(report['correction_cycles'], 1)
        first, last = report['cycles']
        self.assertEqual(first['tests']['exit_code'], 0)
        self.assertTrue(first['oracle_before']['seed_reproduced'])
        self.assertEqual(last['oracle_after']['exit_code'], 0)
        self.assertNotEqual(first['candidate_sha'], last['candidate_sha'])
        self.assertEqual(first['changed_files'], ['toy_math.py'])
        self.assertEqual(last['review']['candidate_sha'], report['candidate_sha'])
        self.assertEqual(fake.review_inputs[0]['fixed_tests'], F.WEAK_TESTS)
        self.assertNotIn('acceptance_test.py', json.dumps(fake.review_inputs[0]))
        self.assertIsNone(fake.review_inputs[0]['previous_findings'])
        self.assertIn('Reversed lower/upper', fake.prompts[1])
        self.assertNotIn('Preserve the existing function body', fake.prompts[1])
        self.assertTrue((run / 'artifacts/cycle_1/feedback.json').is_file())
        self.assertEqual(sum(name == 'feedback_forwarded' for name, _ in events), 1)

    def test_missing_independent_finding_does_not_manufacture_a_correction(self):
        _, fake, report, _ = self.run_fixture('missed_bug')
        self.assertEqual(report['reason'], 'SEEDED_BUG_NOT_IDENTIFIED_BY_INDEPENDENT_REVIEW')
        self.assertEqual(fake.developers, 1)
        self.assertEqual(fake.reviews, 1)

    def test_review_contract_rejects_descriptive_labels_and_names_fixed_test_file(self):
        _, fake, _, _ = self.run_fixture()
        for prompt, handoff in zip(fake.review_prompts, fake.review_inputs):
            schema = json.loads(prompt.split('Return ONLY JSON matching this schema: ')[1].split('\nEvidence:\n')[0])
            self.assertEqual(handoff['fixed_tests_file'], 'test_toy_math.py')
            review = {'candidate_sha': handoff['candidate_sha'], 'verdict': 'changes_requested',
                'findings': [{'severity': 'medium', 'file': 'test_toy_math.py', 'line': None,
                    'explanation': 'Missing reversed-bounds test.'}]}
            C.APP['validate'](review, schema)
            review['findings'][0]['file'] = 'tests (fixed_tests)'
            with self.assertRaisesRegex(ValueError, 'SCHEMA_ENUM'):
                C.APP['validate'](review, schema)
        # No mutation of the historical profile/control schema or CLI flags.
        self.assertNotIn('enum', C.APP['REVIEW_SCHEMA']['properties']['findings']['items']['properties']['file'])

    def test_final_review_receives_actual_combined_coverage_before_deciding(self):
        _, fake, report, events = self.run_fixture('coverage_context')
        self.assertEqual(report['verdict'], 'PASS')
        first, final = fake.review_inputs
        self.assertNotIn('acceptance_test_source', first)
        self.assertNotIn('acceptance_evidence', first)
        self.assertEqual(final['fixed_tests'], F.WEAK_TESTS)
        self.assertEqual(final['acceptance_test_source'], F.ACCEPTANCE_TEST)
        self.assertEqual(final['acceptance_evidence'], report['cycles'][1]['oracle_after'])
        self.assertEqual(final['acceptance_evidence']['candidate_sha'], final['candidate_sha'])
        self.assertEqual(final['acceptance_evidence']['exit_code'], 0)
        oracle_index = next(i for i, (name, value) in enumerate(events)
            if name == 'acceptance_oracle_completed' and value['cycle'] == 1)
        review_index = next(i for i, (name, value) in enumerate(events)
            if name == 'phase_started' and value == {'role': 'claude', 'cycle': 1})
        self.assertLess(oracle_index, review_index)

    def test_actual_invalid_test_label_stops_without_normalizing_or_retrying(self):
        _, fake, report, events = self.run_fixture('out_of_scope_label')
        self.assertEqual(report['reason'], 'REVIEW_FINDING_OUTSIDE_SCOPE')
        self.assertEqual(report['verdict'], 'FAIL')
        self.assertEqual(report['agent_sessions'], {'codex': 1, 'claude': 1})
        self.assertFalse(any(name == 'feedback_forwarded' for name, _ in events))
        self.assertEqual(fake.developers, 1)

    def test_explicit_pass_with_low_advice_completes_and_preserves_all_findings(self):
        run, fake, report, _ = self.run_fixture('advisory_pass')
        self.assertEqual(report['verdict'], 'PASS')
        self.assertTrue(report['correction_verified'])
        self.assertEqual(report['agent_sessions'], {'codex': 2, 'claude': 2})
        self.assertEqual(W.result_state(report), 'awaiting_acceptance')
        final = report['cycles'][-1]
        self.assertEqual(final['review']['candidate_sha'], report['candidate_sha'])
        self.assertEqual(final['oracle_after']['exit_code'], 0)
        self.assertEqual(len(final['review']['findings']), 2)
        saved = json.loads((run / 'artifacts/cycle_1/reviewer.json').read_text())
        self.assertEqual(saved, final['review'])
        screen = acceptance_text({'state': 'awaiting_acceptance', 'result': report})
        markdown = (run / 'REPORT.md').read_text(encoding='utf-8')
        for finding in saved['findings']:
            self.assertIn(finding['explanation'], screen)
            self.assertIn(finding['explanation'], markdown)
        self.assertIn('low findings may accompany pass', fake.review_prompts[-1])

    def test_pass_cannot_waive_medium_or_high_findings(self):
        for fault in ('pass_medium', 'pass_high'):
            with self.subTest(fault=fault):
                _, _, report, _ = self.run_fixture(fault)
                self.assertEqual(report['verdict'], 'FAIL')
                self.assertFalse(report['correction_verified'])
                self.assertEqual(report['reason'], 'REVIEW_BLOCKED_OR_INCONSISTENT_NEEDS_HUMAN')

    def test_low_findings_do_not_override_blocked_or_changes_requested(self):
        for fault, reason in (('blocked_low', 'REVIEW_BLOCKED_OR_INCONSISTENT_NEEDS_HUMAN'),
                ('changes_requested_low', 'CORRECTION_LIMIT_REACHED')):
            with self.subTest(fault=fault):
                _, _, report, _ = self.run_fixture(fault)
                self.assertEqual(report['verdict'], 'FAIL')
                self.assertFalse(report['correction_verified'])
                self.assertEqual(report['reason'], reason)

    def test_initial_developer_cannot_remove_seed_before_independent_review(self):
        _, fake, report, _ = self.run_fixture('early_fix')
        self.assertEqual(report['reason'], 'INITIAL_FIXTURE_BODY_CHANGED_EXPECTED_DOCSTRING_ONLY')
        self.assertEqual(fake.reviews, 0)

    def test_unexpected_initial_weak_test_failure_cannot_bypass_first_review(self):
        _, fake, report, _ = self.run_fixture('weak_failure')
        self.assertEqual(report['reason'], 'WEAK_FIXTURE_TESTS_UNEXPECTEDLY_FAILED')
        self.assertEqual(fake.reviews, 0)

    def test_failed_final_oracle_or_unresolved_review_never_becomes_pass(self):
        for fault in ('unfixed', 'unresolved_review'):
            with self.subTest(fault=fault):
                _, fake, report, _ = self.run_fixture(fault)
                self.assertEqual(report['verdict'], 'FAIL')
                self.assertEqual(fake.developers, 2)
                self.assertEqual(fake.reviews, 1 if fault == 'unfixed' else 2)

    def test_template_has_a_separate_smaller_approval_budget(self):
        self.assertEqual(POLICIES['correction_validation']['codex'], 2)
        self.assertEqual(POLICIES['correction_validation']['claude'], 2)
        self.assertEqual(POLICIES['correction_validation']['max_correction_cycles'], 1)
        self.assertEqual(POLICIES['collaboration']['max_correction_cycles'], 2)

    def test_acceptance_screen_contains_revision_files_tests_findings_and_status(self):
        _, _, report, _ = self.run_fixture()
        text = acceptance_text({'state': 'awaiting_acceptance', 'result': report})
        for value in ('AWAITING_ACCEPTANCE', report['candidate_sha'], 'toy_math.py', 'Ran 8 tests',
                'Reversed lower/upper', 'after fix: PASS', 'before fix: FAIL (seed reproduced)', 'not recorded'):
            self.assertIn(value, text)

    def test_fixture_transfer_keeps_executable_reviewer_controls_identical(self):
        import runpy
        prepare = runpy.run_path(str(C.PROTO / 'prepare_live_run.py'))['prepare']
        run = prepare(TF.reviewer_source(), template=F.TEMPLATE)
        self.assertEqual((run / 'repo/toy_math.py').read_text(), F.SOURCE)
        self.assertEqual((run / 'repo/test_toy_math.py').read_text(), F.WEAK_TESTS)
        for name in ('reviewer_settings.json', 'empty_mcp.json', 'review_schema.json', 'developer_schema.json'):
            self.assertEqual((run / 'control' / name).read_bytes(), (TF.reviewer_source() / 'control' / name).read_bytes())
        self.assertEqual(C.APP['verified_startup_gate'](run)['verdict'], 'PASS')

    def test_final_integrity_failure_revokes_provisional_pass(self):
        original = W.APP['check_scope']
        def late_failure(repo, before):
            if (repo.parent / 'artifacts/cycle_1/reviewer.json').exists():
                raise RuntimeError('SYNTHETIC_FINAL_INTEGRITY_FAILURE')
            return original(repo, before)
        with patch.dict(W.APP, {'check_scope': late_failure}):
            _, _, report, _ = self.run_fixture()
        self.assertEqual(report['reason'], 'SYNTHETIC_FINAL_INTEGRITY_FAILURE')
        self.assertEqual(report['verdict'], 'FAIL')
        self.assertFalse(report['correction_verified'])
        self.assertEqual(W.result_state(report), 'failed')

    def test_git_diff_preserves_utf8_candidate_text(self):
        run = C.APP['prepare']()
        source = 'def clamp(value, lower, upper):\n    """Use “bounds” safely."""\n    raise NotImplementedError\n'
        (run / 'repo/toy_math.py').write_text(source, encoding='utf-8')
        diff = C.APP['git'](run, 'diff', '--no-ext-diff', '--no-textconv', '--', 'toy_math.py')
        self.assertIn('“bounds”', diff)


if __name__ == '__main__':
    unittest.main()
