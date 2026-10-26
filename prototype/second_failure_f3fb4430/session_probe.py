"""Model-free diagnostic: initialize + ephemeral thread/start only; NEVER turn/start."""
import json
from pathlib import Path
import queue
import runpy
import subprocess
import sys
import threading
import time

base = Path(__file__).resolve().parent.parent
app = runpy.run_path(str(base / "orchestrator.py"), run_name="session_probe_settings")
diagnostics = runpy.run_path(str(base / "diagnostics.py"), run_name="session_probe_diagnostics")
spec = json.loads(Path(sys.argv[1]).read_text())
proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
messages = queue.Queue()
stderr_codes = set()
stderr_contexts = []
def read_stdout():
    for line in iter(lambda: proc.stdout.readline(262145), b""):
        if len(line) > 262144:
            messages.put({"fatal": "OUTPUT_LIMIT"})
            return
        try:
            messages.put(json.loads(line))
        except ValueError:
            messages.put({"fatal": "NON_JSON_PROTOCOL"})
    messages.put({"fatal": "CLI_EXITED"})
def read_stderr():
    for line in iter(lambda: proc.stderr.readline(262145), b""):
        text = line.decode("utf-8", "replace")
        stderr_codes.update(diagnostics["error_codes"](text))
        detail = diagnostics["error_detail"](text, spec["cwd"])
        if len(stderr_contexts) < 32 and (detail["codes"] or detail["native_windows_errors"] or detail["operation_contexts"]):
            stderr_contexts.append(detail)
readers = [threading.Thread(target=target, daemon=True) for target in (read_stdout, read_stderr)]
for reader in readers:
    reader.start()
def send(value):
    if "method" in value and value["method"] not in ("initialize", "initialized", "thread/start"):
        raise RuntimeError("MODEL_OR_EXECUTION_RPC_FORBIDDEN")
    proc.stdin.write((json.dumps(value) + "\n").encode())
    proc.stdin.flush()
report = {"scope": "MODEL_FREE_EPHEMERAL_THREAD_START", "turns_started": 0,
          "error_codes": [], "notification_names": [], "thread_created": False}
try:
    send({"id": 0, "method": "initialize", "params": {"clientInfo": {
        "name": "dani_model_free_probe", "title": "Dani diagnostic", "version": "0.0.1"},
        "capabilities": {"experimentalApi": True}}})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            event = messages.get(timeout=.1)
        except queue.Empty:
            continue
        if "fatal" in event:
            report["error"] = event["fatal"]
            break
        if event.get("method"):
            report["notification_names"].append(event["method"])
            if "id" in event:
                send({"id": event["id"], "error": {"code": -32601, "message": "No tools allowed in diagnostic"}})
        if event.get("id") == 0:
            if "error" in event:
                report["error_codes"] = diagnostics["error_codes"](json.dumps(event["error"]))
                report["error"] = "INITIALIZE_FAILED"
                report["rpc_error_detail"] = diagnostics["error_detail"](json.dumps(event["error"]), spec["cwd"])
                break
            send({"method": "initialized", "params": {}})
            report["initialize_succeeded"] = True
            send({"id": 1, "method": "thread/start", "params": {"model": "gpt-6-astra", "modelProvider": "openai",
                "cwd": spec["cwd"], "ephemeral": True, "approvalPolicy": "never"}})
        if event.get("id") == 1:
            if "error" in event:
                report["error_codes"] = diagnostics["error_codes"](json.dumps(event["error"]))
                report["error"] = "THREAD_START_FAILED"
                report["rpc_error_detail"] = diagnostics["error_detail"](json.dumps(event["error"]), spec["cwd"])
                report["rpc_error_code"] = event["error"].get("code") if type(event["error"].get("code")) is int else None
            else:
                report["thread_created"] = True
                report["active_permission_profile"] = event["result"].get("activePermissionProfile")
            break
    else:
        report["error"] = "PROBE_TIMEOUT"
finally:
    proc.terminate()
    proc.wait(timeout=2)
    for reader in readers:
        reader.join(timeout=1)
report["stderr_codes"] = sorted(stderr_codes)
report["stderr_contexts"] = stderr_contexts
print(json.dumps(report))
