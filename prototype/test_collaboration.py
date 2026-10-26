"""Bounded workflow fault injection. Fake agents and tests; no model calls/code execution."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import collaboration as W, coordinator as C
from dani_agent.state import Store, POLICIES



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class Agents:
    def __init__(self, run, fault=None):
        self.run, self.fault = run, fault
        self.calls, self.prompts = [], []
        self.developers = self.reviewers = 0

    def __call__(self, spec, folder, **options):
        kind = spec["kind"]
        self.calls.append(kind)
        if kind == "claude-inventory":
            return {"initialization": {"tools": [], "mcp_servers_count": 0}, "prompt_sent": False}
        if kind == "codex":
            index = self.developers
            self.developers += 1
            self.prompts.append(spec["argv"][-1])
            if self.fault == "quota":
                return {"exit_code": 1, "diagnostic_codes": ["QUOTA_EXHAUSTED"]}
            if self.fault != "unchanged" or index == 0:
                (self.run / "repo/toy_math.py").write_text(
                    "def clamp(value, lower, upper):\n    # Synthetic revision " + str(index)
                    + "\n    if lower > upper:\n        raise ValueError\n    return min(max(value, lower), upper)\n")
            if self.fault == "protected" and index == 1:
                (self.run / "repo/.mcp.json").write_text('{}')
            if options.get("watch"):
                options["watch"]()
            return {"exit_code": 0, "events": [{"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
                {"type": "final", "text": json.dumps({"status": "implemented", "changed_files": ["toy_math.py"], "summary": "Synthetic"})}]}
        if kind == "plain":
            if self.fault == "tests" and self.developers == 1:
                return {"exit_code": 1, "stdout": "SIMULATED TEST FAILURE"}
            return {"exit_code": 0, "stdout": "SIMULATED ONLY\nRan 8 tests\nOK\n"}
        if kind == "claude-review":
            handoff = json.loads(spec["prompt"].split("\nEvidence:\n")[1])
            changes = self.fault not in ("first_pass", "tests", "sha", "tool", "mutate") and (self.reviewers == 0 or self.fault == "limit")
            self.reviewers += 1
            review = {"candidate_sha": "wrong" if self.fault == "sha" else handoff["candidate_sha"],
                "verdict": "changes_requested" if changes else "pass", "findings": []}
            if changes:
                review["findings"] = [{"file": "toy_math.py", "line": 1, "severity": "medium", "explanation": "SYNTHETIC_REVIEW_FIX_MARKER"}]
            if self.fault == "mutate":
                (self.run / "repo/toy_math.py").write_text("# forbidden reviewer mutation")
            if options.get("watch"):
                options["watch"]()
            return {"exit_code": 0, "initialization": {"tools": ["Bash"] if self.fault == "tool" else [], "mcp_servers_count": 0},
                "prompt_sent": True, "events": [{"type": "result", "subtype": "success", "is_error": False,
                    "result": json.dumps(review), "usage": {"input_tokens": 50, "output_tokens": 10}}]}
        raise AssertionError(kind)


class CollaborationTests(unittest.TestCase):
    def pipeline(self, fault=None):
        run = C.APP["prepare"]()
        task = {"objective": "Fix the clamp bounds bug", "payload": {"template": "clamp-v1", "project_name": "Synthetic regression"}}
        fake, events = Agents(run, fault), []
        result = W.execute(run, task, runner=fake, simulated=True, event=lambda name, value: events.append((name, value)))
        return run, fake, result, events

    def test_automatic_review_feedback_fix_and_exact_revision_verification(self):
        run, fake, result, events = self.pipeline()
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["scope"], "SIMULATED_COLLABORATION")
        self.assertEqual(result["agent_sessions"], {"codex": 2, "claude": 2})
        self.assertEqual(result["correction_cycles"], 1)
        self.assertIn("SYNTHETIC_REVIEW_FIX_MARKER", fake.prompts[1])
        self.assertEqual(sum(name == "feedback_forwarded" for name, _ in events), 1)
        first, second = result["cycles"]
        self.assertNotEqual(first["candidate_sha"], second["candidate_sha"])
        self.assertEqual(result["candidate_sha"], second["review"]["candidate_sha"])
        self.assertEqual(len(result["usage"]), 4)
        self.assertIsNone(result["subscription_quota_used"])
        self.assertTrue((run / "artifacts/cycle_1/feedback.json").is_file())
        self.assertIn("SIMULATED_COLLABORATION", (run / "REPORT.md").read_text())

    def test_first_pass_does_not_spend_unused_cycles(self):
        _, _, result, _ = self.pipeline("first_pass")
        self.assertEqual(result["agent_sessions"], {"codex": 1, "claude": 1})
        self.assertEqual(W.result_state(result), "awaiting_acceptance")

    def test_maximum_two_corrections_no_fourth_developer_or_review(self):
        _, fake, result, _ = self.pipeline("limit")
        self.assertEqual(result["reason"], "CORRECTION_LIMIT_REACHED")
        self.assertEqual(result["agent_sessions"], {"codex": 3, "claude": 3})
        self.assertEqual(fake.developers, 3)

    def test_unchanged_candidate_stops_without_duplicate_review(self):
        _, _, result, _ = self.pipeline("unchanged")
        self.assertEqual(result["reason"], "UNCHANGED_CANDIDATE_NEEDS_HUMAN_DECISION")
        self.assertEqual(result["agent_sessions"], {"codex": 2, "claude": 1})

    def test_failed_mandatory_tests_forwarded_without_reviewing_failed_code(self):
        _, fake, result, _ = self.pipeline("tests")
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["agent_sessions"], {"codex": 2, "claude": 1})
        self.assertIn("SIMULATED TEST FAILURE", fake.prompts[1])

    def test_protected_addition_after_checkpoint_stops_correction(self):
        _, _, result, _ = self.pipeline("protected")
        self.assertEqual(result["reason"], "PROTECTED_OR_UNEXPECTED_PATH_CHANGED")
        self.assertEqual(result["agent_sessions"], {"codex": 2, "claude": 1})

    def test_actual_reviewer_tool_and_revision_checks_fail_closed(self):
        for fault, reason in (("tool", "REVIEWER_INVENTORY_OR_HOOK_FAILED"), ("sha", "REVIEWED_REVISION_MISMATCH"),
                ("mutate", "READ_ONLY_PHASE_MODIFIED_REPOSITORY")):
            with self.subTest(fault=fault):
                _, _, result, _ = self.pipeline(fault)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["agent_sessions"], {"codex": 1, "claude": 1})

    def test_confirmed_quota_stops_no_fallback_no_retry(self):
        _, fake, result, _ = self.pipeline("quota")
        self.assertEqual(W.result_state(result), "paused_quota")
        self.assertEqual(result["agent_sessions"], {"codex": 1, "claude": 0})
        self.assertNotIn("claude-review", fake.calls)

    def test_submission_cannot_select_external_project_or_security_policy(self):
        for payload in ({"template": "clamp-v1", "project_name": "safe", "cwd": str(Path.home())},
                {"template": "arbitrary", "project_name": "safe"}):
            with self.assertRaises(ValueError):
                W.submission("Fix code", payload)

    def test_new_workflow_requires_its_own_larger_budget_approval(self):
        self.assertEqual(POLICIES["integration"]["codex"], 1)
        self.assertEqual(POLICIES["collaboration"]["codex"], 3)
        folder = ROOT / "prototype" / ("decision_regression_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        store = Store(folder / "state.sqlite3")
        task_id = store.add("collaboration", "Fix bounds", {"template": "clamp-v1", "project_name": "Test"})
        self.assertIsNone(store.claim("sources"))
        store.approve(task_id, "sources")
        task = store.claim("sources")
        self.assertEqual(task["kind"], "collaboration")
        store.finish(task_id, task["run_id"], "awaiting_acceptance", {"candidate_sha": "test_sha", "verdict": "PASS"})
        with self.assertRaises(ValueError):
            store.decide(task_id, "different_sha", True)
        store.decide(task_id, "test_sha", False)
        self.assertEqual(store.get(task_id)["state"], "rejected")
        self.assertEqual(store.history(task_id)[-1]["kind"], "human_rejected")
        with self.assertRaises(ValueError):
            store.decide(task_id, "test_sha", True)

    def test_acceptance_revalidates_actual_repository_and_rejects_new_changes(self):
        run, _, result, _ = self.pipeline("first_pass")
        folder = ROOT / "prototype" / ("acceptance_regression_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        store = Store(folder / "state.sqlite3")
        task_id = store.add("collaboration", "Fix clamp")
        store.approve(task_id, "test")
        task = store.claim("test")
        store.finish(task_id, task["run_id"], "awaiting_acceptance", result)
        (run / "repo/toy_math.py").write_text("# unreviewed change")
        with self.assertRaisesRegex(RuntimeError, "REVIEWED_REVISION_CHANGED"):
            C.human_decision(store, task_id, True)
        self.assertEqual(store.get(task_id)["state"], "awaiting_acceptance")

    def test_cancel_pending_never_claims_or_consumes_quota(self):
        folder = ROOT / "prototype" / ("cancel_regression_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        store = Store(folder / "state.sqlite3")
        task_id = store.add("collaboration", "Synthetic")
        store.cancel_pending(task_id)
        self.assertEqual(store.get(task_id)["state"], "cancelled")
        self.assertIsNone(store.claim("test"))

    def test_usage_drops_non_numeric_private_and_unrecognized_fields(self):
        measured = W.usage({"events": [{"type": "turn.completed", "usage": {
            "input_tokens": 3, "output_tokens": True, "private": "do not store", "cached_input_tokens": -1}}]}, "codex", 0)
        self.assertEqual(measured["tokens"], {"input_tokens": 3})
        self.assertIsNone(measured["actual_charge_usd"])


if __name__ == "__main__":
    unittest.main()
