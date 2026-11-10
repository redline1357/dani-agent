"""Independent model-free regressions for approved full-source review batching."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import milestone_review as R


class MilestoneBatchTests(unittest.TestCase):
    def sources(self, count=3):
        return {**{name: 'security core\n' * 8 for name in R.CORE},
                **{f'dani_agent/synthetic_{i}.py': str(i) * 2000 for i in range(count)}}

    @contextmanager
    def fixture(self, sources=None, limit=5000, base=None):
        sources = self.sources() if sources is None else sources
        with tempfile.TemporaryDirectory(prefix='milestone_batches_', dir=ROOT / 'prototype') as temporary:
            run = Path(temporary)
            for name in ('control', 'repo', 'review_snapshot', '.claude', 'artifacts', 'execution'):
                (run / name).mkdir()
            (run / '.mcp.json').write_text('{}')
            def inventory(path):
                return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in path.rglob('*') if p.is_file()}
            def run_path(path):
                if Path(path).name == 'prepare_live_run.py':
                    return {'prepare': lambda source: run}
                if Path(path).name == 'host_launcher.py':
                    return {'verify_offline_tests': lambda: {'verdict': 'PASS', 'scope': 'SIMULATED'}}
                raise AssertionError('Unexpected execution: ' + str(path))
            with ExitStack() as stack:
                stack.enter_context(patch.object(R, 'review_sources', return_value=sources))
                stack.enter_context(patch.object(R, 'fingerprint', return_value='synthetic-fingerprint'))
                stack.enter_context(patch.object(R, 'baseline', return_value=base))
                stack.enter_context(patch.object(R, 'material_rejection', return_value=False))
                stack.enter_context(patch.object(R, 'MAX_BUNDLE_CHARS', limit))
                stack.enter_context(patch.object(R.C, 'reviewer_source', return_value=Path('synthetic')))
                stack.enter_context(patch.object(R.runpy, 'run_path', side_effect=run_path))
                stack.enter_context(patch.dict(R.APP, {
                    'approved_run': lambda value: Path(value), 'inventory': inventory,
                    'commands': lambda value: {'claude': ['synthetic-cli', '--max-turns', '1']},
                    'env': lambda value: {}, 'preflight': Mock(), 'verified_startup_gate': Mock(),
                }))
                try:
                    yield run, sources
                finally:
                    # prepare() intentionally makes snapshots read-only, including on Windows.
                    for path in (run / 'review_snapshot').iterdir():
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def output(self, sha, findings=(), verdict='pass', **changes):
        result = {'candidate_sha': sha, 'verdict': verdict, 'findings': list(findings)}
        return {'exit_code': 0, 'initialization': {'tools': [], 'mcp_servers_count': 0},
            'prompt_sent': True, 'events': [{'type': 'result', 'subtype': 'success',
                'result': json.dumps(result), 'usage': {'input_tokens': 7, 'output_tokens': 3}}], **changes}

    def finding(self, severity='low', label='advice'):
        return {'severity': severity, 'file': R.CORE[0], 'line': 1, 'explanation': label}

    def test_initial_review_preserves_every_full_file_and_core_in_each_bounded_batch(self):
        sources = self.sources()
        with patch.object(R, 'MAX_BUNDLE_CHARS', 5000):
            batches = R.evidence_batches(sources, None)
            self.assertEqual(batches, R.evidence_batches(sources, None))
        self.assertEqual(len(batches), 3)
        covered = {}
        seen_noncore = []
        for index, shown in enumerate(batches, 1):
            self.assertLessEqual(len(json.dumps(shown, sort_keys=True)), 5000)
            self.assertEqual((shown['batch_index'], shown['batch_count']), (index, 3))
            self.assertEqual(shown['candidate_sha'], R.digest(sources))
            self.assertEqual(shown['candidate_file_count'], len(sources))
            for name in R.CORE:
                self.assertEqual(shown['sources'][name], sources[name])
            covered.update(shown['sources'])
            seen_noncore.extend(name for name in shown['sources'] if name not in R.CORE)
        self.assertEqual(covered, sources)
        self.assertEqual(sorted(seen_noncore), sorted(set(sources) - set(R.CORE)))

    def test_incremental_review_that_fits_remains_exactly_one_session(self):
        old = self.sources()
        changed = {**old, 'dani_agent/synthetic_0.py': 'changed\n'}
        base = {'sources': old, 'candidate_sha': R.digest(old)}
        with patch.object(R, 'MAX_BUNDLE_CHARS', 10000):
            batches = R.evidence_batches(changed, base)
            self.assertEqual(batches, [R.evidence(changed, base)])
        self.assertEqual(batches[0]['mode'], 'changes_since_last_passed_review')

    def test_large_core_large_file_and_more_than_three_batches_fail_before_prepare(self):
        for sources in [self.sources(4), {R.CORE[0]: 'x' * 6000}, {**self.sources(0), 'huge.py': 'x' * 6000}]:
            with self.subTest(files=list(sources)), self.fixture(sources) as (_, _):
                with patch.object(R.runpy, 'run_path') as preparer:
                    with self.assertRaisesRegex(RuntimeError, 'MILESTONE_REVIEW_CONTEXT_LIMIT'):
                        R.prepare()
                    preparer.assert_not_called()

    def test_prepare_payload_binds_exact_counts_hashes_and_read_only_snapshots(self):
        with self.fixture() as (run, sources):
            payload = R.prepare()
            plan = json.loads((run / 'artifacts/milestone_plan.json').read_text())
            self.assertEqual(payload['review_batch_count'], 3)
            self.assertEqual(payload['review_source_files'], len(sources))
            self.assertEqual(payload['candidate_sha'], R.digest(sources))
            self.assertEqual(payload['evidence_sha'], R.digest(plan['evidence_batches']))
            self.assertEqual(payload['review_evidence_chars'], [len(json.dumps(v, sort_keys=True)) for v in plan['evidence_batches']])
            self.assertEqual(payload, R.approval_payload(run, plan))
            for index, shown in enumerate(plan['evidence_batches'], 1):
                self.assertEqual(json.loads((run / 'review_snapshot' / f'phase2_milestone_{index}.txt').read_text()), shown)

    def test_three_batches_run_sequentially_aggregate_low_findings_and_exact_usage(self):
        with self.fixture() as (run, sources):
            payload = R.prepare()
            calls, states = [], []
            def runner(spec, folder, **kwargs):
                calls.append(spec)
                self.assertEqual(kwargs['timeout'], None)
                self.assertEqual(kwargs['idle'], R.SESSION_IDLE_SECONDS)
                self.assertEqual(spec['kind'], 'claude-review')
                self.assertFalse(spec['startup_inventory_verified'])
                self.assertEqual(spec['argv'], ['synthetic-cli', '--max-turns', '1'])
                kwargs['watch']()
                progress = json.loads((run / 'artifacts/milestone_progress.json').read_text())
                self.assertEqual(progress['agent_sessions']['claude'], len(calls))
                self.assertEqual(folder.name, 'review_batch_' + str(len(calls)))
                shown = json.loads(spec['prompt'].split('\nEvidence:\n', 1)[1])
                self.assertEqual(shown['batch_index'], len(calls))
                return self.output(payload['candidate_sha'], [self.finding(label=f'batch {len(calls)}')])
            result = R.execute({'payload': payload}, run / 'execution', runner=runner, simulated=True,
                               status=lambda phase, **values: states.append((phase, values)))
            self.assertEqual(result['verdict'], 'PASS')
            self.assertEqual(result['agent_sessions'], {'codex': 0, 'claude': 3})
            self.assertEqual(result['completed_review_batches'], 3)
            self.assertEqual([f['explanation'] for f in result['review']['findings']], ['batch 1', 'batch 2', 'batch 3'])
            self.assertEqual([u['review_batch'] for u in result['usage']], [1, 2, 3])
            self.assertEqual([u['tokens']['input_tokens'] for u in result['usage']], [7, 7, 7])
            self.assertEqual([item[1]['review_batch'] for item in states], [1, 2, 3])
            self.assertTrue(all(item[1]['review_batch_count'] == 3 for item in states))
            self.assertTrue(all((run / 'artifacts' / f'claude_milestone_session_{i}.json').is_file() for i in (1, 2, 3)))
            self.assertEqual(json.loads((run / 'result.json').read_text()), result)
            with self.assertRaisesRegex(RuntimeError, 'NO_REPLAY'):
                R.execute({'payload': payload}, run / 'execution', runner=Mock(), simulated=True)

    def test_any_material_or_nonpass_batch_stops_without_spending_later_sessions(self):
        for severity, verdict in [('medium', 'pass'), ('high', 'pass'), ('low', 'changes_requested'), ('low', 'blocked')]:
            with self.subTest(severity=severity, verdict=verdict), self.fixture() as (run, _):
                payload = R.prepare()
                runner = Mock(side_effect=[self.output(payload['candidate_sha'], [self.finding()]),
                    self.output(payload['candidate_sha'], [self.finding(severity)], verdict)])
                result = R.execute({'payload': payload}, run / 'execution', runner=runner, simulated=True)
                self.assertEqual(result['verdict'], 'FAIL')
                self.assertEqual(runner.call_count, 2)
                self.assertEqual(result['agent_sessions']['claude'], 2)
                self.assertEqual(len(result['review']['findings']), 2)

    def test_candidate_mismatch_and_security_errors_preserve_attempts_and_earlier_findings(self):
        for error in ('candidate', 'tools', 'exit', 'completion', 'malformed'):
            with self.subTest(error=error), self.fixture() as (run, _):
                payload = R.prepare()
                bad = self.output('wrong' if error == 'candidate' else payload['candidate_sha'])
                if error == 'tools': bad['initialization']['tools'] = ['Bash']
                if error == 'exit': bad['exit_code'] = 1
                if error == 'completion': bad['events'] = []
                if error == 'malformed': bad['events'][0]['result'] = '{bad'
                runner = Mock(side_effect=[self.output(payload['candidate_sha'], [self.finding()]), bad])
                with self.assertRaises((RuntimeError, ValueError)):
                    R.execute({'payload': payload}, run / 'execution', runner=runner, simulated=True)
                self.assertEqual(runner.call_count, 2)
                result = json.loads((run / 'result.json').read_text())
                self.assertEqual(result['verdict'], 'FAIL')
                self.assertEqual(result['agent_sessions']['claude'], 2)
                self.assertEqual(result['completed_review_batches'], 1)
                self.assertEqual(result['review']['findings'], [self.finding()])
                self.assertEqual(len(result['usage']), 2)

    def test_approval_metadata_tampering_and_incomplete_batch_coverage_never_launch(self):
        for field in ('review_batch_count', 'review_evidence_chars', 'review_source_files', 'evidence_sha', 'coverage'):
            with self.subTest(field=field), self.fixture() as (run, _):
                payload = R.prepare()
                if field == 'coverage':
                    path = run / 'artifacts/milestone_plan.json'
                    plan = json.loads(path.read_text())
                    victim = next(name for name in plan['evidence_batches'][-1]['sources'] if name not in R.CORE)
                    del plan['evidence_batches'][-1]['sources'][victim]
                    plan['evidence_sha'] = R.digest(plan['evidence_batches'])
                    payload = R.approval_payload(run, plan)
                    plan.update({key: payload[key] for key in ('review_batch_count', 'review_evidence_chars', 'review_source_files')})
                    path.write_text(json.dumps(plan))
                else:
                    payload[field] = 'tampered'
                runner = Mock()
                with self.assertRaisesRegex(RuntimeError, 'MISMATCH|REPREPARE'):
                    R.execute({'payload': payload}, run / 'execution', runner=runner, simulated=True)
                runner.assert_not_called()

    def test_source_or_read_only_change_stops_before_next_batch_and_preserves_progress(self):
        with self.fixture() as (run, _):
            payload = R.prepare()
            def runner(spec, folder, **kwargs):
                (run / 'repo/changed.txt').write_text('synthetic tamper')
                return self.output(payload['candidate_sha'])
            with self.assertRaisesRegex(RuntimeError, 'READ_ONLY_INPUT_CHANGED'):
                R.execute({'payload': payload}, run / 'execution', runner=runner, simulated=True)
            result = json.loads((run / 'result.json').read_text())
            self.assertEqual(result['agent_sessions']['claude'], 1)
            self.assertEqual(result['verdict'], 'FAIL')

    def test_legacy_single_approval_and_artifact_name_remain_supported(self):
        with self.fixture({'synthetic.py': 'pass\n'}, limit=5000) as (run, sources):
            plan = {'sources': sources, 'candidate_sha': R.digest(sources), 'fingerprint': 'synthetic-fingerprint'}
            (run / 'artifacts/milestone_plan.json').write_text(json.dumps(plan))
            payload = R.approval_payload(run, plan)
            self.assertEqual(set(payload), {'run', 'candidate_sha', 'fingerprint'})
            result = R.execute({'payload': payload}, run / 'execution',
                runner=Mock(return_value=self.output(payload['candidate_sha'])), simulated=True)
            self.assertEqual(result['agent_sessions']['claude'], 1)
            self.assertTrue((run / 'artifacts/claude_milestone_session.json').is_file())

    def test_prior_material_rejection_still_prevents_approval(self):
        with self.fixture({'synthetic.py': 'pass\n'}) as (run, _):
            payload = R.prepare()
            with patch.object(R, 'material_rejection', return_value=True):
                result = R.execute({'payload': payload}, run / 'execution',
                    runner=Mock(return_value=self.output(payload['candidate_sha'])), simulated=True)
            self.assertEqual(result['verdict'], 'FAIL')
            self.assertEqual(result['reason'], 'MILESTONE_CONFLICTING_REVIEW_REQUIRES_CORRECTION')

    def test_validated_material_progress_survives_failed_finalization_and_blocks_a_later_pass(self):
        check_rejection = R.material_rejection
        with self.fixture(self.sources(0)) as (rejected_run, sources):
            payload = R.prepare()
            plan = json.loads((rejected_run / 'artifacts/milestone_plan.json').read_text())
            (rejected_run / 'artifacts/local_live_approval.json').write_text('{}')
            runner = Mock(return_value=self.output(payload['candidate_sha'], [self.finding('medium')], 'changes_requested'))
            with patch('dani_agent.reports.finalize', side_effect=OSError('SYNTHETIC_PUBLICATION_FAILED')):
                with self.assertRaisesRegex(OSError, 'SYNTHETIC_PUBLICATION_FAILED'):
                    R.execute({'payload': payload}, rejected_run / 'execution', runner=runner)
            runner.assert_called_once()
            self.assertFalse((rejected_run / 'result.json').exists())
            progress_path = rejected_run / 'artifacts/milestone_progress.json'
            saved = progress_path.read_bytes()
            progress = json.loads(saved)
            self.assertEqual(progress['completed_review_batches'], 1)
            self.assertEqual(progress['review']['findings'], [self.finding('medium')])
            self.assertEqual(progress['reason'], 'MILESTONE_FINDINGS_REQUIRE_CORRECTION')
            with patch.object(R, 'review_index', return_value=[(rejected_run, R.brief(plan))]):
                self.assertTrue(check_rejection(payload['candidate_sha']))
                with self.fixture(sources) as (later_run, _):
                    later_payload = R.prepare()
                    with patch.object(R, 'material_rejection', side_effect=check_rejection):
                        result = R.execute({'payload': later_payload}, later_run / 'execution',
                            runner=Mock(return_value=self.output(later_payload['candidate_sha'])))
                    self.assertEqual(result['verdict'], 'FAIL')
                    self.assertEqual(result['reason'], 'MILESTONE_CONFLICTING_REVIEW_REQUIRES_CORRECTION')
            self.assertEqual(progress_path.read_bytes(), saved)

    def test_progress_rejection_requires_live_approval_and_matching_snapshot(self):
        sources = self.sources(0)
        candidate = R.digest(sources)
        for case in ('valid', 'simulated', 'candidate', 'sources', 'unapproved', 'low'):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=ROOT / 'prototype') as temporary:
                run = Path(temporary)
                (run / 'artifacts').mkdir()
                plan = {'sources': {'wrong': 'snapshot'} if case == 'sources' else sources, 'candidate_sha': candidate}
                progress = {'scope': 'SIMULATED_MILESTONE_REVIEW' if case == 'simulated' else 'LIVE_MILESTONE_REVIEW',
                    'candidate_sha': 'wrong' if case == 'candidate' else candidate,
                    'review': {'findings': [self.finding('low' if case == 'low' else 'high')]}}
                (run / 'artifacts/milestone_progress.json').write_text(json.dumps(progress))
                if case != 'unapproved':
                    (run / 'artifacts/local_live_approval.json').write_text('{}')
                with patch.object(R, 'review_index', return_value=[(run, R.brief(plan))]):
                    self.assertEqual(R.material_rejection(candidate), case == 'valid')

    def test_interrupted_review_cannot_replay_any_already_attempted_batch(self):
        with self.fixture() as (run, _):
            payload = R.prepare()
            (run / 'artifacts/milestone_progress.json').write_text(json.dumps({'agent_sessions': {'codex': 0, 'claude': 1}}))
            runner = Mock()
            with self.assertRaisesRegex(RuntimeError, 'ALREADY_STARTED_NO_REPLAY'):
                R.execute({'payload': payload}, run / 'execution', runner=runner, simulated=True)
            runner.assert_not_called()

    def test_four_batch_plan_cannot_bypass_limit_with_consistent_payload_hashes(self):
        with self.fixture() as (run, _):
            R.prepare()
            path = run / 'artifacts/milestone_plan.json'
            plan = json.loads(path.read_text())
            plan['evidence_batches'].append(plan['evidence_batches'][0])
            plan['evidence_sha'] = R.digest(plan['evidence_batches'])
            payload = R.approval_payload(run, plan)
            plan.update({key: payload[key] for key in ('review_batch_count', 'review_evidence_chars', 'review_source_files')})
            path.write_text(json.dumps(plan))
            runner = Mock()
            with self.assertRaisesRegex(RuntimeError, 'SNAPSHOT_HASH_MISMATCH'):
                R.execute({'payload': payload}, run / 'execution', runner=runner, simulated=True)
            runner.assert_not_called()

    def test_live_path_retains_preflight_and_empty_inventory_gate_before_mocked_session(self):
        with self.fixture({'synthetic.py': 'pass\n'}) as (run, _):
            payload = R.prepare()
            def runner(spec, folder, **kwargs):
                R.APP['preflight'].assert_called_once_with(run)
                R.APP['verified_startup_gate'].assert_called_once_with(run)
                self.assertTrue(spec['startup_inventory_verified'])
                return self.output(payload['candidate_sha'])
            result = R.execute({'payload': payload}, run / 'execution', runner=runner)
            self.assertEqual(result['scope'], 'LIVE_MILESTONE_REVIEW')
            self.assertEqual(result['verdict'], 'PASS')


if __name__ == '__main__':
    unittest.main()
