"""Offline checks for the exact exec path, tighter policy and approval gate."""
import json
import contextlib
import io
from pathlib import Path
import runpy
import sys
import tomllib
import unittest
import uuid
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
DIAG = runpy.run_path(str(BASE / "codex_exec_diagnostic.py"), run_name="exec_diagnostic_regression")
APP = DIAG["APP"]


import fixture_runs as TF

class ExecDiagnosticTests(unittest.TestCase):
    def test_unexpected_tool_event_stops_real_guarded_process(self):
        folder = BASE / ("exec_tool_guard_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        command = ('import json,time; print(json.dumps({"type":"item.started",'
            '"item":{"type":"command_execution","command":"SYNTHETIC_NEVER_EXECUTED"}}),flush=True); time.sleep(10)')
        result = APP["PROCESS"]["run"]({"argv": [sys.executable, "-I", "-B", "-c", command],
            "cwd": str(folder), "env": {"SystemRoot": r"C:\Windows"}, "kind": "codex", "forbid_tools": True}, folder, timeout=5)
        self.assertEqual(result["error"], "DIAGNOSTIC_TOOL_USE_FORBIDDEN")
        self.assertTrue(result["containment"]["job_empty_after_termination"])
        self.assertNotIn("SYNTHETIC_NEVER_EXECUTED", json.dumps(result))

    def test_only_declared_config_differences_from_production(self):
        run = TF.toy_run()
        live = APP["commands"](run)["codex"]
        diag = APP["commands"](run, exec_diagnostic=True)["codex"]
        position = live.index("exec")
        production = tomllib.loads("\n".join(live[2:position:2]))
        diagnostic = tomllib.loads("\n".join(diag[2:position:2]))
        expected = json.loads(json.dumps(production))
        expected["permissions"]["toydev"]["filesystem"][str(run / "repo")] = "read"
        expected["model_reasoning_effort"] = "low"
        self.assertEqual(diagnostic, expected)
        self.assertEqual(live[position:-1], diag[position:-1])
        self.assertEqual(diag[0], live[0])
        self.assertIn("Do not use any tools", diag[-1])
        self.assertIn('"summary":"DANI_EXEC_OK"', diag[-1])

    def execute_fake(self, approved, image_denied=False):
        run = APP["prepare"]()
        # Provide equivalent synthetic directories for the unchanged integrity guard.
        (run / ".claude").mkdir()
        (run / ".mcp.json").write_text('{}')
        argv = APP["commands"](run, exec_diagnostic=True)["codex"]
        APP["save"](run / "artifacts/exec_diagnostic_plan.json", {"argv": argv,
            "source_hashes": {name: DIAG["hashlib"].sha256((BASE / name).read_bytes()).hexdigest()
                for name in ("orchestrator.py", "process.py", "diagnostics.py", "codex_exec_diagnostic.py")}})
        calls = []
        def runner(spec, directory, **kwargs):
            calls.append((spec, kwargs))
            if spec["kind"] == "codex-local-check":
                return {"exit_code": 0, "stdout": "DANI_MXC_OK\n", "error": None, "containment": {
                    key: True for key in ("assigned_before_resume", "job_queries_passed", "job_empty_after_termination",
                        "job_termination_succeeded", "all_observed_processes_stopped")}}
            if spec['kind'] == 'plain':
                checks = {name: {'read': True} for name in ('codex_image', 'workspace_source', 'test_source', 'output_schema')}
                checks['codex_version'] = {'process_started': True, 'exit_code': 0, 'expected_version': True}
                if image_denied:
                    checks['codex_image'] = {'read': False, 'winerror': 5, 'errno': 13}
                    checks['codex_version'] = {'process_started': False, 'winerror': 5, 'errno': 13}
                return {'exit_code': 0, 'stdout': json.dumps({'scope': 'MODEL_FREE_MXC_IMAGE_CHECK', 'checks': checks})}
            return {"exit_code": 0, "error": None, "events": [{"type": "turn.completed"},
                {"type": "final", "text": '{"status":"blocked","changed_files":[],"summary":"DANI_EXEC_OK"}'}]}
        with patch.dict(DIAG["run_diagnostic"].__globals__, {"codex_only_preflight": lambda run: None}):
            with patch.dict(APP["PROCESS"], {"run": runner}), contextlib.redirect_stdout(io.StringIO()):
                result = DIAG["run_diagnostic"](run, approved=approved)
        return run, calls, result

    def test_unapproved_path_runs_only_model_free_control(self):
        run, calls, result = self.execute_fake(False)
        self.assertEqual(result, 2)
        self.assertEqual([spec["kind"] for spec, kwargs in calls], ["codex-local-check", "plain"])
        self.assertFalse((run / "artifacts/exec_diagnostic_attempt.json").exists())
        self.assertFalse((run / "result.json").exists())
        # Mark the synthetic fixture so it can never be used as a real run.
        APP["save"](run / "result.json", {"scope": "OFFLINE_DIAGNOSTIC_REGRESSION_ONLY"})

    def test_approved_path_uses_production_runner_once_for_60_seconds_and_no_claude(self):
        run, calls, result = self.execute_fake(True)
        self.assertEqual(result, 0)
        self.assertEqual([spec["kind"] for spec, kwargs in calls], ["codex-local-check", "plain", "codex"])
        spec, options = calls[2]
        self.assertEqual(spec["argv"], APP["commands"](run, exec_diagnostic=True)["codex"])
        self.assertEqual(spec["env"], DIAG["diagnostic_env"](run))
        self.assertEqual(spec["env"], calls[0][0]["env"])
        native_argv = calls[0][0]["argv"]
        self.assertEqual(native_argv[:native_argv.index("sandbox")], spec["argv"][:spec["argv"].index("exec")])
        self.assertTrue(spec["forbid_tools"])
        self.assertEqual(options["timeout"], 60)
        with self.assertRaisesRegex(RuntimeError, "USED_RUN_NO_LIVE_RETRY"):
            DIAG["run_diagnostic"](run, approved=True)

    def test_runtime_probe_preserves_policy_and_never_requests_inference(self):
        _, calls, _ = self.execute_fake(False)
        native, probe = (calls[index][0]['argv'] for index in (0, 1))
        self.assertEqual(native[:native.index('--') + 1], probe[:probe.index('--') + 1])
        self.assertEqual(probe[probe.index('--') + 1:probe.index('--') + 5], [APP['PYTHON'], '-I', '-B', '-c'])
        self.assertIn("'--version'", probe[-1])
        self.assertNotIn("'exec'", probe[-1])
        self.assertNotIn('auth.json', probe[-1])

    def test_image_access_failure_stops_before_any_approved_model_session(self):
        run, calls, code = self.execute_fake(True, image_denied=True)
        self.assertEqual(code, 1)
        self.assertEqual([spec['kind'] for spec, _ in calls], ['codex-local-check', 'plain'])
        self.assertFalse((run / 'artifacts/exec_diagnostic_attempt.json').exists())

    def test_targeted_logging_changes_only_diagnostic_verbosity_not_credentials_or_policy(self):
        run = TF.toy_run()
        original = APP['codex_env'](run)
        instrumented = DIAG['diagnostic_env'](run)
        self.assertEqual({key: value for key, value in instrumented.items() if key != 'RUST_LOG'}, original)
        self.assertNotIn('auth', instrumented['RUST_LOG'])
        self.assertNotIn('http', instrumented['RUST_LOG'])
        self.assertNotIn('trace', instrumented['RUST_LOG'])
        detail = APP['PROCESS']['DIAGNOSTICS']['error_detail'](
            'codex_core::fs_client private-data SYNTHETIC_SECRET Access is denied. (os error 5)')
        self.assertIn('SANDBOXED_FILESYSTEM_RUNTIME', detail['operation_contexts'])
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(detail))
        self.assertFalse(detail['underlying_operation_identified'])


if __name__ == "__main__":
    unittest.main()
