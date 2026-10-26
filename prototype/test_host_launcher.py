"""Local-only regressions for environment repair, safe evidence, and host gating."""
import json
import hashlib
import os
from pathlib import Path
import runpy
import sys
import tomllib
import unittest
from unittest.mock import patch
import uuid

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="host_regression_settings")
HOST = runpy.run_path(str(BASE / "host_launcher.py"), run_name="host_regression_launcher")
DIAG = runpy.run_path(str(BASE / "diagnostics.py"), run_name="host_regression_diagnostics")


import fixture_runs as TF

class HostLauncherTests(unittest.TestCase):
    def test_recorded_approval_is_command_bound_and_consumed_only_once(self):
        run = APP["prepare"]()
        APP["save"](run / "result.json", {"scope": "OFFLINE_APPROVAL_REGRESSION_ONLY"})
        approval = {"approval_source": "EXPLICIT_PRODUCT_OWNER_CHAT_APPROVAL", "run": str(run),
            "codex_sessions": 1, "claude_sessions": 1, "seconds_per_agent": 120, "retry_count": 0,
            "api_fallback": False, "model_upgrades": False, "commands_sha256": HOST["digest"](APP["commands"](run))}
        path = run / "artifacts/project_owner_live_approval.json"
        for key, value in (("run", "wrong_run"), ("commands_sha256", "wrong_hash"), ("codex_sessions", 2)):
            path.write_text(json.dumps({**approval, key: value}))
            with self.assertRaisesRegex(RuntimeError, "RECORDED_LIVE_APPROVAL_INVALID_STOP"):
                HOST["consume_recorded_approval"](run)
            self.assertFalse((run / "artifacts/live_approval_consumed.json").exists())
        path.write_text(json.dumps(approval))
        HOST["consume_recorded_approval"](run)
        with self.assertRaises(FileExistsError):
            HOST["consume_recorded_approval"](run)

    def test_exec_argument_probe_has_no_prompt_or_app_server(self):
        run = TF.toy_run()
        live = APP["commands"](run)["codex"]
        probe = HOST["exec_help_argv"](run)
        self.assertEqual(probe, live[:-1] + ["--help"])
        self.assertNotIn(live[-1], probe)
        self.assertIn("--ignore-user-config", probe)
        self.assertIn("--ignore-rules", probe)
        self.assertNotIn("app-server", probe)

    def test_real_exec_help_is_model_free_contained_and_sanitized(self):
        run = TF.toy_run()
        folder = BASE / ("exec_help_regression_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        result = HOST["exec_probe"](run, folder)
        self.assertTrue(HOST["pass_contained_marker"](result, "DANI_EXEC_HELP_OK"))
        self.assertEqual(result["events"], [])
        self.assertFalse(result["prompt_sent"])
        self.assertEqual(result["stdout"], "DANI_EXEC_HELP_OK\n")

    def test_each_mxc_probe_uses_original_profile_without_policy_changes(self):
        run = TF.toy_run()
        calls = []
        def capture(spec, directory, **kwargs):
            calls.append(spec)
            return {}
        with patch.dict(HOST["APP"]["PROCESS"], {"run": capture}):
            for profile in ("toydev", "toytest"):
                HOST["mxc_probe"](run, BASE, profile)
        for profile, spec in zip(("toydev", "toytest"), calls):
            argv = spec["argv"]
            position = argv.index("sandbox")
            config = tomllib.loads("\n".join(argv[2:position:2]))
            self.assertEqual(config["windows"]["sandbox"], "mxc")
            self.assertEqual(config["permissions"], tomllib.loads("\n".join(
                APP["overrides"](APP["permissions"](run, profile, profile == "toydev"))[1::2]))["permissions"])
            self.assertEqual(argv[argv.index("--permission-profile") + 1], profile)
            self.assertEqual(argv[argv.index("--") + 1:],
                [r"C:\Windows\System32\cmd.exe", "/d", "/c", "echo", "DANI_MXC_OK"])
            self.assertEqual(spec["env"], APP["codex_env"](run))

    def test_host_gate_rejects_old_evidence_and_requires_both_mxc_profiles(self):
        run = APP["prepare"]()
        context = {"appcontainer": False, "elevated": False}
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        gate = {"verdict": "PASS", "context": context, "preflight_kind": "EXEC_CLI_MODEL_FREE_V1",
            "exec_arguments_pass": True, "toydev_mxc_pass": True, "toytest_mxc_pass": True,
            "session_initialization": "UNVERIFIED_REQUIRES_SEPARATELY_APPROVED_CODEX_EXEC",
            "argv": APP["commands"](run)["codex"],
            "environment_sha256": HOST["digest"](APP["codex_env"](run)),
            "binary_sha256": sha(Path(APP["CODEX"])), "orchestrator_sha256": sha(BASE / "orchestrator.py"),
            "process_sha256": sha(BASE / "process.py"), "extra_sources": {name: sha(BASE / name)
                for name in ("diagnostics.py", "host_launcher.py", "second_failure_f3fb4430/session_probe.py")}}
        path = run / "artifacts/mxc_host_gate.json"
        # This fixture is explicitly marked synthetic; never used by a live run.
        APP["save"](run / "result.json", {"scope": "OFFLINE_GATE_REGRESSION_ONLY"})
        with patch.dict(APP["PROCESS"]["DIAGNOSTICS"], {"execution_context": lambda: context}):
            for key in ("preflight_kind", "exec_arguments_pass", "toydev_mxc_pass", "toytest_mxc_pass", "session_initialization"):
                path.write_text(json.dumps({**gate, key: None}))
                with self.assertRaisesRegex(RuntimeError, "MXC_HOST_PREFLIGHT_CHANGED_STOP"):
                    APP["verified_mxc_host_gate"](run)
            path.write_text(json.dumps(gate))
            self.assertEqual(APP["verified_mxc_host_gate"](run)["preflight_kind"], "EXEC_CLI_MODEL_FREE_V1")

    def test_only_codex_environment_gets_required_host_drive(self):
        run = TF.frozen_run()
        reviewer = APP["env"](run)
        with patch.dict(os.environ, {"SYSTEMDRIVE": "C:", "LOCALAPPDATA": r"C:\SyntheticHost\Local",
                "TEMP": r"C:\SyntheticHost\Temp", "TMP": r"C:\SyntheticHost\Temp", "OPENAI_API_KEY": "SYNTHETIC_NOT_A_CREDENTIAL"}):
            developer = APP["codex_env"](run)
        self.assertNotIn("SystemDrive", reviewer)
        self.assertEqual(developer["SystemDrive"], "C:")
        self.assertEqual(developer["LOCALAPPDATA"], r"C:\SyntheticHost\Local")
        self.assertEqual(developer["TEMP"], r"C:\SyntheticHost\Temp")
        self.assertNotIn("OPENAI_API_KEY", developer)
        refreshed = {"systemroot", "windir", "systemdrive", "localappdata", "temp", "tmp", "path"}
        self.assertEqual({k.casefold(): v for k, v in developer.items() if k.casefold() not in refreshed},
                         {k.casefold(): v for k, v in reviewer.items() if k.casefold() not in refreshed})
        # PATH: the reviewer's entries minus anything under the user profile (2026-10-22 Pocket Ledger root cause).
        home = str(Path.home()).rstrip("\\").casefold()
        expected = [p for p in reviewer["PATH"].split(";") if p.strip() and p.strip().rstrip("\\").casefold() != home
                    and not p.strip().casefold().startswith(home + "\\")]
        self.assertEqual(developer["PATH"].split(";") if developer["PATH"] else [], expected)
        self.assertEqual(len(developer), len({key.casefold() for key in developer}))

    def test_shell_environment_remains_explicit_and_minimal(self):
        run = TF.frozen_run()
        argv = APP["commands"](run)["codex"]
        config = tomllib.loads("\n".join(argv[2:argv.index("exec"):2]))
        self.assertEqual(config["shell_environment_policy"]["inherit"], "none")
        self.assertEqual(set(config["shell_environment_policy"]["set"]), {"SystemDrive", "SystemRoot", "WINDIR", "PATH"})
        self.assertFalse(config["permissions"]["toydev"]["network"]["enabled"])

    def test_appcontainer_and_elevated_launchers_are_refused(self):
        for context in ({"appcontainer": True, "elevated": False}, {"appcontainer": False, "elevated": True}, {}):
            with self.assertRaises(RuntimeError):
                HOST["require_host"](context)
        HOST["require_host"]({"appcontainer": False, "elevated": False})

    def test_error_classifier_never_returns_arbitrary_message_values(self):
        text = "failed to prepare MXC sandbox: MXC requires nonempty Unicode host SystemDrive SYNTHETIC_SECRET"
        codes = DIAG["error_codes"](text)
        self.assertIn("MXC_HOST_SYSTEMDRIVE_MISSING", codes)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(codes))
        self.assertEqual(DIAG["error_codes"]("Unclassified private message"), [])

    def test_sanitized_stderr_and_containment_are_persisted(self):
        folder = BASE / ("host_regression_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        command = "import sys; print('MXC requires nonempty Unicode host SystemDrive', file=sys.stderr); sys.exit(1)"
        result = APP["PROCESS"]["run"]({"argv": [sys.executable, "-I", "-B", "-c", command],
            "cwd": str(folder), "env": APP["env"](TF.frozen_run()),
            "kind": "codex-local-check"}, folder, timeout=10)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["diagnostic_codes"], ["MXC_HOST_SYSTEMDRIVE_MISSING"])
        self.assertEqual(result["stdout"], "")
        evidence = list(folder.glob("*.containment.json"))
        self.assertEqual(len(evidence), 1)
        self.assertTrue(json.loads(evidence[0].read_text())["all_observed_processes_stopped"])

    def test_mxc_marker_alone_does_not_clear_containment_gate(self):
        result = {"exit_code": 0, "stdout": "DANI_MXC_OK\n", "error": None}
        self.assertFalse(HOST["pass_mxc"](result))
        result["containment"] = {"assigned_before_resume": True, "job_queries_passed": True,
                                 "all_observed_processes_stopped": True, "job_empty_after_termination": True,
                                 "job_termination_succeeded": True}
        self.assertTrue(HOST["pass_mxc"](result))

    def test_real_pipeline_refuses_appcontainer_before_calling_any_agent(self):
        run = APP["prepare"]()
        def forbidden(*args, **kwargs):
            self.fail("No agent may be called")
        with patch.dict(APP["PROCESS"]["DIAGNOSTICS"], {"execution_context": lambda: {"appcontainer": True, "elevated": False}}):
            result = APP["pipeline"](run, runner=forbidden)
        self.assertEqual(result["reason"], "NORMAL_NON_ELEVATED_HOST_REQUIRED_NO_MODEL_STARTED")
        self.assertEqual(result["agent_sessions"], {"codex": 0, "claude": 0})

    def test_real_pipeline_requires_mxc_host_evidence_before_calling_any_agent(self):
        run = APP["prepare"]()
        def forbidden(*args, **kwargs):
            self.fail("No agent may be called")
        with patch.dict(APP["PROCESS"]["DIAGNOSTICS"], {"execution_context": lambda: {"appcontainer": False, "elevated": False}}):
            result = APP["pipeline"](run, runner=forbidden)
        self.assertEqual(result["reason"], "MODEL_FREE_MXC_HOST_PREFLIGHT_REQUIRED")
        self.assertEqual(result["agent_sessions"], {"codex": 0, "claude": 0})


if __name__ == "__main__":
    unittest.main()
