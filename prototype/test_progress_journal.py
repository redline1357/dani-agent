"""Real harmless process tests: timeout/cancellation must preserve sanitized progress."""
import json
from pathlib import Path
import runpy
import sys
import unittest
import uuid

BASE = Path(__file__).resolve().parent
PROCESS = runpy.run_path(str(BASE / "process.py"), run_name="progress_regression")


class ProgressTests(unittest.TestCase):
    def test_hidden_background_child_keeps_pipes_and_job_containment(self):
        folder=BASE/('hidden_console_'+uuid.uuid4().hex);folder.mkdir()
        code=('import ctypes,sys; k=ctypes.WinDLL("kernel32"); '
              'k.GetConsoleWindow.restype=ctypes.c_void_p; '
              'print("CONSOLE="+str(k.GetConsoleWindow() or 0)); '
              'print("PIPE="+str(not sys.stdout.isatty()))')
        result=PROCESS['run']({'argv':[sys.executable,'-I','-B','-c',code], 'cwd':str(folder),
            'env':{'SystemRoot':r'C:\Windows'},'kind':'plain'},folder,timeout=10)
        self.assertEqual(result['exit_code'],0)
        self.assertIn('CONSOLE=0',result['stdout'])
        self.assertIn('PIPE=True',result['stdout'])
        for key in ('assigned_before_resume','job_queries_passed','all_observed_processes_stopped',
                    'job_termination_succeeded','job_empty_after_termination'):
            self.assertTrue(result['containment'][key])
        (folder/'verification.json').write_text(json.dumps({'model_calls':0,'result':result},indent=2))

    def test_timeout_keeps_milestones_and_unterminated_stderr_without_secrets(self):
        folder = BASE / ("progress_timeout_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        code = ('import json,sys,time; print(json.dumps({"type":"thread.started"}),flush=True); '
            'sys.stderr.write("failed to open: Access is denied. (os error 5) SYNTHETIC_SECRET"); '
            'sys.stderr.flush(); time.sleep(40)')
        with self.assertRaises(PROCESS["ProcessFailure"]) as caught:
            PROCESS["run"]({"argv": [sys.executable, "-I", "-B", "-c", code], "cwd": str(folder),
                "env": {"SystemRoot": r"C:\Windows"}, "kind": "codex"}, folder, timeout=6)   # a busy PC needs a few seconds to start Python
        evidence = caught.exception.evidence
        self.assertEqual(str(caught.exception), "TIMEOUT")
        progress = evidence["progress"]
        self.assertTrue(progress["cli_process_started"])
        self.assertIn("thread.started", [item["type"] for item in progress["protocol_milestones"]])
        self.assertIn("ACCESS_DENIED", progress["diagnostic_codes"])
        self.assertGreater(progress["output_bytes"]["stderr"], 0)
        self.assertTrue(evidence["containment"]["job_empty_after_termination"])
        journal = Path(evidence["artifacts"]["progress"]).read_text()
        self.assertNotIn("SYNTHETIC_SECRET", journal)

    def test_incomplete_last_record_does_not_hide_last_valid_progress(self):
        folder = BASE / ("progress_partial_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        path = folder / "progress.jsonl"
        path.write_text(json.dumps({"phase": "running"}) + '\n{"incomplete":')
        self.assertEqual(PROCESS["last_progress"](path), {"phase": "running"})

    def test_cancellation_carries_cleanup_evidence(self):
        folder = BASE / ("progress_cancel_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        def cancel():
            raise RuntimeError("USER_CANCELLED")
        with self.assertRaises(PROCESS["ProcessFailure"]) as caught:
            PROCESS["run"]({"argv": [sys.executable, "-I", "-B", "-c", "import time; time.sleep(15)"],
                "cwd": str(folder), "env": {"SystemRoot": r"C:\Windows"}, "kind": "plain"}, folder, timeout=5, watch=cancel)
        self.assertEqual(str(caught.exception), "USER_CANCELLED")
        self.assertTrue(caught.exception.evidence["containment"]["job_empty_after_termination"])
