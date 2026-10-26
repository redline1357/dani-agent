"""Persistent approval/recovery tests. No real provider calls."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import coordinator as C
from dani_agent.state import Store



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / "prototype" / ("coordinator_regression_" + uuid.uuid4().hex[:8])
        self.folder.mkdir()
        self.store = Store(self.folder / "dani_agent.sqlite3")

    def test_model_approval_claim_is_atomic_and_single_use(self):
        task_id = self.store.add("codex_diagnostic", "Synthetic diagnostic", {"run": "unused"})
        self.assertIsNone(self.store.claim("source_a"))
        self.store.approve(task_id, "source_a")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.store.claim("source_a"), range(2)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        claimed = next(claim for claim in claims if claim)
        self.store.finish(task_id, claimed["run_id"], "failed", {"reason": "SYNTHETIC_FAILURE"})
        self.assertIsNone(self.store.claim("source_a"))
        with self.assertRaises(ValueError):
            self.store.approve(task_id, "source_a")
        self.assertEqual([event["kind"] for event in self.store.history(task_id)],
            ["created", "explicit_human_approval", "started", "failed"])

    def test_code_change_invalidates_pending_approval(self):
        task_id = self.store.add("codex_diagnostic", "Synthetic diagnostic")
        self.store.approve(task_id, "old_sources")
        self.assertIsNone(self.store.claim("new_sources"))
        self.assertEqual(self.store.get(task_id)["state"], "awaiting_approval")

    def test_changed_task_payload_invalidates_approval_without_consuming_it(self):
        task_id = self.store.add("codex_diagnostic", "Approved scope", {"run": "original"})
        self.store.approve(task_id, "same")
        with self.store.connection() as db:
            db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps({"run": "different"}), task_id))
        self.assertIsNone(self.store.claim("same"))
        self.assertEqual(self.store.get(task_id)["state"], "awaiting_approval")

    def test_restricted_executor_does_not_consume_host_model_approval(self):
        task_id = self.store.add("codex_diagnostic", "Host-only task")
        self.store.approve(task_id, "same")
        local_id = self.store.add("refresh_evidence", "Safe local work")
        claimed = self.store.claim("same", {"offline_tests", "refresh_evidence"})
        self.assertEqual(claimed["id"], local_id)
        self.assertEqual(self.store.get(task_id)["state"], "queued")
        with self.store.connection() as db:
            self.assertIsNone(db.execute("SELECT consumed FROM approvals WHERE task_id=?", (task_id,)).fetchone()[0])

    def test_restart_does_not_replay_uncertain_model_call_and_continues_local_work(self):
        task_id = self.store.add("codex_diagnostic", "Synthetic diagnostic")
        self.store.approve(task_id, "same")
        self.store.claim("same")
        local_id = self.store.add("refresh_evidence", "Resume safe local work")
        restarted = Store(self.store.path)
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(restarted.get(task_id)["state"], "interrupted")
        self.assertEqual(restarted.claim("same")["id"], local_id)
        with restarted.connection() as db:
            self.assertIsNotNone(db.execute("SELECT consumed FROM approvals WHERE task_id=?", (task_id,)).fetchone()[0])

    def test_run_identity_and_unknown_workflow_fail_closed(self):
        with self.assertRaises(ValueError):
            self.store.add("arbitrary_shell", "Do not execute")
        task_id = self.store.add("refresh_evidence", "Local")
        self.store.claim("same")
        with self.assertRaisesRegex(ValueError, "RUN_NOT_ACTIVE_FOR_TASK"):
            self.store.finish(task_id, "wrong_run", "completed", {})
        self.assertEqual(self.store.get(task_id)["state"], "running")

    def test_import_only_real_approved_evidence_without_false_quota_estimates(self):
        for name in ("real", "mocked"):
            run = self.folder / "runs" / name
            (run / "artifacts").mkdir(parents=True)
            (run / "result.json").write_text(json.dumps({"scope": "LIVE_CODEX_EXEC_DIAGNOSTIC", "verdict": "FAIL",
                "reason": "TIMEOUT", "codex_sessions": 1, "claude_sessions": 0}))
        real = self.folder / "runs/real/artifacts"
        (real / "exec_diagnostic_approval.json").write_text('{}')
        (real / "native.containment.json").write_text(json.dumps({"termination_reason": "TIMEOUT", "elapsed_seconds": 60.125}))
        with patch.object(C, "PROTO", self.folder), patch.dict(C.APP, {"WORK_RUNS": self.folder / "work_runs"}):
            self.assertEqual(C.import_evidence(self.store), 1)
        evidence = self.store.evidence()
        self.assertEqual(evidence[0]["reason"], "TIMEOUT")
        self.assertEqual(evidence[0]["timeouts"], [60.125])
        self.assertIsNone(evidence[0]["subscription_quota_used"])

    def test_worker_cannot_start_twice_or_accept_external_state_paths(self):
        task_id = self.store.add("offline_tests", "Synthetic local task")
        self.store.claim(C.fingerprint())
        job_dir = self.folder / "jobs" / task_id
        job_dir.mkdir(parents=True)
        with patch.object(C, "DATA", self.folder):
            # Never the real self-test here: it would run this suite again, which would start it again (2026-10-23).
            with patch.object(C, "run_while_active", return_value=1) as self_test, \
                    patch("subprocess.Popen", side_effect=AssertionError("real process started")):
                self.assertEqual(C.worker(self.store.path, task_id, job_dir), 1)
                with self.assertRaises(FileExistsError):
                    C.worker(self.store.path, task_id, job_dir)
                self.assertEqual(self_test.call_count, 1)
            with self.assertRaisesRegex(RuntimeError, "INVALID_STATE_DATABASE"):
                C.worker(self.folder / "outside.sqlite3", task_id, job_dir)

    def test_worker_checks_approval_before_any_provider_operation(self):
        task_id = self.store.add("codex_diagnostic", "Unapproved")
        with self.store.connection() as db:
            db.execute("UPDATE tasks SET state='running' WHERE id=?", (task_id,))
        job_dir = self.folder / "jobs" / task_id
        job_dir.mkdir(parents=True)
        with patch.object(C, "DATA", self.folder), patch("subprocess.run") as process:
            with self.assertRaisesRegex(RuntimeError, "EXPLICIT_CURRENT_APPROVAL_REQUIRED"):
                C.worker(self.store.path, task_id, job_dir)
            process.assert_not_called()

    def test_collaboration_result_reports_agent_sessions_at_top_level(self):
        # Regression: live job 9419ecc7 saved agent_sessions codex=1/claude=1 but top-level 0/0.
        task_id = self.store.add("collaboration", "Synthetic collaboration")
        self.store.approve(task_id, C.fingerprint())
        self.store.claim(C.fingerprint())
        job_dir, run = self.folder / "jobs" / task_id, self.folder / "synthetic_run"
        job_dir.mkdir(parents=True)
        (run / "artifacts").mkdir(parents=True)
        fakes = {"require_host": lambda context: None, "DIAGNOSTICS": {"execution_context": lambda: {}},
            "prepare": lambda source: run, "check": lambda run: True}
        observed = {"verdict": "PASS", "reason": "SYNTHETIC", "agent_sessions": {"codex": 2, "claude": 1}}
        with patch.object(C, "DATA", self.folder), patch.object(C.runpy, "run_path", return_value=fakes),                 patch.object(C, "integration_ready", return_value=True),                 patch("dani_agent.collaboration.execute", return_value=observed), patch("subprocess.run") as process:
            self.assertEqual(C.worker(self.store.path, task_id, job_dir), 0)
            process.assert_not_called()
        result = json.loads((job_dir / "result.json").read_text())
        self.assertEqual(result["agent_sessions"], {"codex": 2, "claude": 1})
        self.assertEqual((result["codex_sessions"], result["claude_sessions"]), (2, 1))

    def test_finished_result_is_recovered_without_replaying_worker(self):
        task_id = self.store.add("refresh_evidence", "Local result")
        self.store.claim("same")
        folder = self.folder / "jobs" / task_id
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({"verdict": "PASS", "reason": "SAVED_BEFORE_UI_CRASH"}))
        (folder / "outer.process.json").write_text(json.dumps({"exit_code": 0, "error": None}))
        (folder / "outer.containment.json").write_text(json.dumps({key: True for key in (
            "assigned_before_resume", "job_queries_passed", "job_empty_after_termination",
            "job_termination_succeeded", "all_observed_processes_stopped")}))
        with patch.object(C, "DATA", self.folder), patch("subprocess.run") as process:
            self.assertEqual(C.recover_saved_results(self.store), {"published_results": 1, "interrupted": 0})
            process.assert_not_called()
        self.assertEqual(self.store.get(task_id)["state"], "completed")

    def test_single_instance_lock_prevents_two_coordinators(self):
        first, second = C.InstanceLock(self.folder / "coordinator.lock"), C.InstanceLock(self.folder / "coordinator.lock")
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "COORDINATOR_ALREADY_RUNNING"):
                second.acquire()
        finally:
            first.close()
        second.acquire()
        second.close()

    def test_interrupted_workflow_recovers_partial_history_without_replay(self):
        task_id = self.store.add("collaboration", "Synthetic task")
        self.store.approve(task_id, "test")
        self.store.claim("test")
        run = C.APP["prepare"]()
        C.APP["save"](run / "artifacts/workflow_progress.json", {"agent_sessions": {"codex": 2, "claude": 1},
            "cycles": [{"cycle": 0, "candidate_sha": "synthetic"}], "usage": [{"tokens": None}]})
        folder = self.folder / "jobs" / task_id
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps({"run": str(run), "phase": "codex"}))
        with patch.object(C, "DATA", self.folder), patch("subprocess.run") as process:
            self.assertEqual(C.recover_saved_results(self.store), {"published_results": 0, "interrupted": 1})
            process.assert_not_called()
        task = self.store.get(task_id)
        self.assertEqual(task["state"], "interrupted")
        self.assertEqual(task["result"]["agent_sessions"], {"codex": 2, "claude": 1})
        self.assertEqual(task["result"]["containment_after_interruption"], "UNVERIFIED_UNLESS_RECORDED")
        self.assertIsNone(self.store.claim("test"))

    def test_new_passing_preflight_supersedes_unapproved_old_diagnostic(self):
        old = self.store.add("codex_diagnostic", "Old unapproved", {"run": "old"})
        C.queue_diagnostic(self.store, {"run": "new"})
        self.assertEqual(self.store.get(old)["state"], "cancelled")
        self.assertEqual(sum(task["kind"] == "codex_diagnostic" and task["state"] == "awaiting_approval"
            for task in self.store.tasks()), 1)

    def test_correction_validation_creates_one_unapproved_task_and_model_free_preflight(self):
        task_id = C.prepare_correction_validation(self.store)
        self.assertEqual(C.prepare_correction_validation(self.store), task_id)
        task = self.store.get(task_id)
        self.assertEqual(task['state'], 'awaiting_approval')
        self.assertFalse(C.correction_preflight_ready(self.store, task))
        local = self.store.claim(C.fingerprint(), {'correction_preflight'})
        self.assertEqual(local['kind'], 'correction_preflight')
        self.store.finish(local['id'], local['run_id'], 'completed', {'verdict': 'PASS', 'run': task['payload']['run'], 'fingerprint': C.fingerprint()})
        self.assertTrue(C.correction_preflight_ready(self.store, task))
        self.assertIsNone(self.store.claim(C.fingerprint()))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM approvals').fetchone()[0], 0)
        C.check_correction_plan(task)
        with patch.object(C, 'fingerprint', return_value='changed_sources'):
            self.assertFalse(C.correction_preflight_ready(self.store, task))
            with self.assertRaisesRegex(RuntimeError, 'PREPARED_CORRECTION_PLAN_SOURCE_CHANGED'):
                C.check_correction_plan(task)

    def test_human_decision_does_not_claim_independent_testing_by_default(self):
        task_id = self.store.add('refresh_evidence', 'Synthetic acceptance metadata')
        task = self.store.claim('test')
        self.store.finish(task_id, task['run_id'], 'awaiting_acceptance', {'verdict': 'PASS', 'candidate_sha': 'synthetic'})
        self.store.decide(task_id, 'synthetic', True)
        self.assertFalse(self.store.get(task_id)['result']['human_decision']['independently_tested_by_human'])

    def test_project_checkpoint_refuses_restricted_executor_before_git(self):
        with patch.dict(C.APP['PROCESS']['DIAGNOSTICS'], {'execution_context': lambda: {'appcontainer': True, 'elevated': False}}), \
                patch('subprocess.run') as process:
            with self.assertRaisesRegex(RuntimeError, 'NORMAL_NON_ELEVATED_HOST_REQUIRED_FOR_PROJECT_CHECKPOINT'):
                C.checkpoint_project()
            process.assert_not_called()
