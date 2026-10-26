"""Protocol safety checks with a trusted fake CLI; no provider/model invocation."""
import json
from pathlib import Path
import runpy
import sys
import unittest
import uuid

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="diagnostic_protocol_test")
DIAG = runpy.run_path(str(BASE / "tool_diagnostic.py"), run_name="diagnostic_classification_test")
FAKE = '''import json,sys,time
control=json.loads(sys.stdin.readline())
assert control["type"]=="control_request" and control["request"]["subtype"]=="initialize"
print(json.dumps({"type":"control_response","response":{"subtype":"success","request_id":"dani_initialize","response":{}}}),flush=True)
line=sys.stdin.readline()
if line:
    user=json.loads(line)
    assert user["type"]=="user"
    print(json.dumps({"type":"system","subtype":"init","tools":["StructuredOutput"],"mcp_servers":[]}),flush=True)
    time.sleep(5)
'''


import fixture_runs as TF

class DiagnosticTests(unittest.TestCase):
    def invoke(self, kind):
        folder = BASE / ("protocol_test_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        run = TF.toy_run()
        return APP["PROCESS"]["run"]({"argv": [sys.executable, "-I", "-B", "-c", FAKE],
            "cwd": str(folder), "env": APP["env"](run), "kind": kind,
            "prompt": "Synthetic test only"}, folder, timeout=12)

    def test_approved_diagnostic_observes_name_and_stops(self):
        result = self.invoke("claude-tool-diagnostic")
        self.assertTrue(result["prompt_sent"])
        self.assertEqual(result["initialization"], {"tools": ["StructuredOutput"], "mcp_servers_count": 0})
        self.assertEqual(result["events"], [])

    def test_empty_stream_does_not_send_user_message(self):
        result = self.invoke("claude-inventory-eof")
        self.assertFalse(result["prompt_sent"])
        self.assertIsNone(result["initialization"])
        self.assertEqual(result["exit_code"], 0)

    def test_only_known_formatter_is_classified_as_formatter(self):
        self.assertEqual(DIAG["classify"]("StructuredOutput"), "INTERNAL_FINAL_RESPONSE_FORMATTER_STATICALLY_READ_ONLY")
        self.assertIn("FORBIDDEN", DIAG["classify"]("Bash"))
        self.assertEqual(DIAG["classify"]("FutureUnknownTool"), "UNKNOWN_UNAUTHORIZED_STOP")


if __name__ == "__main__":
    unittest.main()
