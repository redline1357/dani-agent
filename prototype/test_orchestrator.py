"""Offline tests. Fake agents/test executor; NEVER executes candidate code on host."""
import json
from pathlib import Path
import runpy
import unittest

APP = runpy.run_path(str(Path(__file__).with_name("orchestrator.py")), run_name="prototype_test")


class FakeRunner:
    def __init__(self, fault=None):
        self.fault = fault
        self.calls = []

    def __call__(self, spec, directory, **options):
        kind = spec["kind"]
        self.calls.append(kind)
        run = Path(directory).parent
        if kind == "claude-inventory":
            return {"initialization": {"tools": ["Bash"] if self.fault == "tool" else [], "mcp_servers_count": 0},
                    "prompt_sent": False, "error": None}
        if kind == "codex":
            (run / "repo/toy_math.py").write_text("def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError\n    return min(max(value, lower), upper)\n")
            if self.fault == "protected":
                (run / "repo/AGENTS.md").write_text("SYNTHETIC forbidden addition")
            if options.get("watch"):
                options["watch"]()
            text = "invalid json" if self.fault == "schema" else json.dumps(
                {"status": "implemented", "changed_files": ["toy_math.py"], "summary": "Synthetic fake-agent result"})
            return {"exit_code": 0, "events": [{"type": "final", "text": text}, {"type": "turn.completed"}]}
        if kind == "plain":
            # Protocol fixture, not a claim that candidate tests ran.
            return {"exit_code": 1 if self.fault == "tests" else 0, "stdout": "SIMULATED EXECUTOR\nRan 8 tests\nOK\n"}
        if kind == "claude-review":
            evidence = json.loads(spec["prompt"].split("\nEvidence:\n", 1)[1])
            assert evidence["candidate"] != APP["STUB"]
            assert "developer summary" not in evidence
            assert evidence["test_evidence"]["simulated"]
            review = {"candidate_sha": "wrong" if self.fault == "sha" else evidence["candidate_sha"],
                      "verdict": "pass", "findings": []}
            return {"exit_code": 0, "initialization": {"tools": [], "mcp_servers_count": 0}, "prompt_sent": True,
                    "events": [{"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(review)}]}
        raise AssertionError(kind)


class PrototypeTests(unittest.TestCase):
    def pipeline(self, fault=None):
        run = APP["prepare"]()
        fake = FakeRunner(fault)
        result = APP["pipeline"](run, fake, simulated=True)
        return run, fake, result

    def test_automatic_handoff(self):
        run, fake, result = self.pipeline()
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["scope"], "SIMULATED_PIPELINE")
        self.assertEqual(fake.calls, ["claude-inventory", "codex", "plain", "claude-review"])
        self.assertTrue((run / "artifacts/handoff.json").exists())
        self.assertTrue((run / "REPORT.md").exists())

    def test_unknown_tool_stops_before_developer(self):
        _, fake, result = self.pipeline("tool")
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(fake.calls, ["claude-inventory"])
        self.assertEqual(result["agent_sessions"], {"codex": 0, "claude": 0})

    def test_protected_addition_stops_before_tests(self):
        _, fake, result = self.pipeline("protected")
        self.assertEqual(result["reason"], "PROTECTED_OR_UNEXPECTED_PATH_CHANGED")
        self.assertNotIn("plain", fake.calls)

    def test_malformed_output_stops_before_tests(self):
        _, fake, result = self.pipeline("schema")
        self.assertEqual(result["verdict"], "FAIL")
        self.assertNotIn("plain", fake.calls)

    def test_test_failure_stops_before_reviewer(self):
        _, fake, result = self.pipeline("tests")
        self.assertEqual(result["reason"], "DETERMINISTIC_TESTS_FAILED")
        self.assertNotIn("claude-review", fake.calls)

    def test_review_sha_must_match(self):
        _, _, result = self.pipeline("sha")
        self.assertEqual(result["reason"], "REVIEW_NOT_PASSED")

    def test_strict_schema(self):
        with self.assertRaises(ValueError):
            APP["validate"]({"status": "implemented", "changed_files": [], "summary": "x", "extra": True}, APP["DEVELOPER_SCHEMA"])
        with self.assertRaises(ValueError):
            APP["validate"]({"candidate_sha": "x", "verdict": "pass", "findings": [
                {"severity": "low", "file": "toy_math.py", "line": True, "explanation": "x"}]}, APP["REVIEW_SCHEMA"])

    def test_no_arbitrary_external_repository(self):
        with self.assertRaises(ValueError):
            APP["approved_run"](Path.home())


if __name__ == "__main__":
    unittest.main()
