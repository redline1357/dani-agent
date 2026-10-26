"""Model-free regression checks for failure localization and non-secret evidence."""
import json
from pathlib import Path
import runpy
import sys
import unittest
import uuid
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="startup_evidence_app")
DIAG = runpy.run_path(str(BASE / "diagnostics.py"), run_name="startup_evidence_codes")
STARTUP = runpy.run_path(str(BASE / "startup_diagnostic.py"), run_name="startup_evidence_diagnostic")


import fixture_runs as TF

class StartupEvidenceTests(unittest.TestCase):
    def folder(self):
        folder = BASE / ("startup_evidence_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        return folder

    def test_error_context_cannot_retain_secrets_or_private_path_values(self):
        private = str(Path.home() / ".codex/auth.json")
        detail = DIAG["error_detail"]('failed to open ' + private + ': Access is denied. (os error 5) SYNTHETIC_SECRET')
        self.assertEqual(detail["native_windows_errors"], [5])
        self.assertIn("OPEN_FILE", detail["operation_contexts"])
        self.assertEqual(detail["referenced_path_classes"], ["CODEX_AUTH"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(detail))
        self.assertNotIn(private, json.dumps(detail))

    def test_agents_md_discovery_denial_is_classified(self):
        # Exact final line of the 2026-10-18 failure, reproduced model-free.
        line = ("Error: thread/start: thread/start failed: error creating thread: failed to load AGENTS.md "
                "instructions for environment `local`: Access is denied. (os error 5) (code -32603) SYNTHETIC_SECRET")
        detail = DIAG["error_detail"](line)
        self.assertIn("AGENTS_MD_LOAD_FAILED", detail["codes"])
        self.assertIn("ACCESS_DENIED", detail["codes"])
        self.assertIn("AGENTS_MD_DISCOVERY", detail["operation_contexts"])
        self.assertEqual(detail["native_windows_errors"], [5])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(detail))

    def test_early_stderr_is_captured_without_claiming_a_session_started(self):
        folder = self.folder()
        command = "import sys; print('failed to create directory: Access is denied. (os error 5) SYNTHETIC_SECRET',file=sys.stderr); sys.exit(1)"
        result = APP["PROCESS"]["run"]({"argv": [sys.executable, "-I", "-B", "-c", command],
            "cwd": str(folder), "env": {"SystemRoot": r"C:\Windows"}, "kind": "codex"}, folder, timeout=5)
        self.assertTrue(result["cli_process_started"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["protocol_milestones"], [])
        self.assertEqual(result["sanitized_error_contexts"][0]["native_windows_errors"], [5])
        self.assertIn("CREATE_DIRECTORY", result["sanitized_error_contexts"][0]["operation_contexts"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_native_process_creation_error_is_distinguished_from_cli_exit(self):
        folder = self.folder()
        result = APP["PROCESS"]["run"]({"argv": [str(folder / "absent.exe")],
            "cwd": str(folder), "env": {"SystemRoot": r"C:\Windows"}, "kind": "codex"}, folder, timeout=5)
        self.assertFalse(result["cli_process_started"])
        self.assertEqual(result["error"], "CLI_PROCESS_CREATION_FAILED")
        self.assertEqual(result["process_creation_winerror"], 2)

    def test_protocol_error_details_are_sanitized_and_milestones_retained(self):
        folder = self.folder()
        events = [{"type": "thread.started", "thread_id": "PRIVATE_ID"},
            {"type": "error", "message": "failed to write: Access is denied. (os error 5) SYNTHETIC_SECRET"}]
        command = "import json; events=" + repr(events) + "; [print(json.dumps(event),flush=True) for event in events]"
        result = APP["PROCESS"]["run"]({"argv": [sys.executable, "-I", "-B", "-c", command],
            "cwd": str(folder), "env": {"SystemRoot": r"C:\Windows"}, "kind": "codex"}, folder, timeout=5)
        self.assertEqual([item["type"] for item in result["protocol_milestones"]], ["thread.started", "error"])
        self.assertIn("ACCESS_DENIED", result["diagnostic_codes"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        self.assertNotIn("PRIVATE_ID", json.dumps(result))

    def test_diagnostic_disables_mcp_and_notify_without_a_model_command(self):
        run = TF.toy_run()
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == Path.home() / ".codex/config.toml":
                return '[mcp_servers.synthetic]\ncommand="DO_NOT_EXECUTE"\n'
            return original_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            argv = STARTUP["diagnostic_argv"](run)
        self.assertNotIn("exec", argv)
        self.assertEqual(argv[-3:], ["app-server", "--listen", "stdio://"])
        self.assertIn('mcp_servers={"synthetic" = {"enabled" = false}}', argv)
        self.assertIn("notify=[]", argv)
        self.assertNotIn("DO_NOT_EXECUTE", " ".join(argv))

    def test_model_free_rpc_error_keeps_native_code_but_not_message(self):
        folder = self.folder()
        server = ('import sys,json; '
            'a=json.loads(sys.stdin.readline()); print(json.dumps({"id":a["id"],"result":{}}),flush=True); '
            'b=json.loads(sys.stdin.readline()); d=json.loads(sys.stdin.readline()); '
            'assert d["method"]=="thread/start"; '
            'print(json.dumps({"id":d["id"],"error":{"code":-32603,"message":"failed to open: Access is denied. (os error 5) SYNTHETIC_SECRET"}}),flush=True); '
            'sys.stdin.read()')
        spec = folder / "probe.json"
        APP["save"](spec, {"argv": [sys.executable, "-I", "-B", "-c", server], "cwd": str(folder), "env": {"SystemRoot": r"C:\Windows"}})
        result = APP["PROCESS"]["run"]({"argv": [sys.executable, "-I", "-B", str(STARTUP["HOST"]["PROBE"]), str(spec)],
            "cwd": str(folder), "env": {"SystemRoot": r"C:\Windows"}, "kind": "plain"}, folder, timeout=10)
        self.assertEqual(result["exit_code"], 0)
        protocol = json.loads(result["stdout"])
        self.assertTrue(protocol["initialize_succeeded"])
        self.assertEqual(protocol["turns_started"], 0)
        self.assertEqual(protocol["rpc_error_code"], -32603)
        self.assertEqual(protocol["rpc_error_detail"]["native_windows_errors"], [5])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
