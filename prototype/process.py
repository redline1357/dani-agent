"""Small Windows Job Object process guard; no provider SDK or dependencies."""
import ctypes as c
import json
import os
from pathlib import Path
import queue
import runpy
import subprocess
import sys
import threading
import time
import uuid

BASE = Path(__file__).resolve().parent
WIN_API = BASE.parent / "phase0_security/final/process_checks.py"
DIAGNOSTICS = runpy.run_path(str(BASE / "diagnostics.py"), run_name="process_diagnostics")
STARTUP_IDLE_SECONDS = 60   # a Claude CLI that prints nothing at all for this long before its tool inventory is stuck
STARTUP_MAX_SECONDS = 300   # noisy startup must also terminate; does not limit an initialized model session


class ProcessFailure(RuntimeError):
    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.evidence = evidence


def last_progress(path):
    """Read the last complete journal entry, tolerating termination mid-write."""
    path = Path(path)
    if not path.exists():
        return None
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 128 * 1024))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except (ValueError, UnicodeError):
            pass
    return None


def job_pids(win, job, statistics):
    """Read the variable-length Windows PID list, including nested child jobs."""
    capacity = 16
    while capacity <= 4096:
        class Members(c.Structure):
            _fields_ = [("assigned", c.c_uint32), ("listed", c.c_uint32), ("pids", c.c_size_t * capacity)]
        members, returned = Members(), c.c_uint32()
        c.set_last_error(0)
        ok = win["queryjob"](job, 3, c.byref(members), c.sizeof(members), c.byref(returned))
        error = 0 if ok else c.get_last_error()
        statistics["pid_query_calls"] = statistics.get("pid_query_calls", 0) + 1
        statistics["max_job_processes"] = max(statistics.get("max_job_processes", 0), members.assigned)
        statistics["largest_pid_buffer_capacity"] = max(statistics.get("largest_pid_buffer_capacity", 0), capacity)
        if not ok and error not in (122, 234):  # INSUFFICIENT_BUFFER / MORE_DATA
            statistics["fatal_job_query_winerror"] = error
            raise RuntimeError("JOB_QUERY_FAILED_WINERROR_" + str(error))
        if ok and members.listed == members.assigned and members.listed <= capacity:
            return [int(pid) for pid in members.pids[:members.listed]]
        statistics["pid_buffer_resizes"] = statistics.get("pid_buffer_resizes", 0) + 1
        capacity = max(capacity * 2, members.assigned)
    raise RuntimeError("JOB_PID_QUERY_MEMORY_BOUND_EXCEEDED")


def worker(spec_path, result_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    launched = time.monotonic()
    progress_path = Path(result_path).with_name(Path(result_path).name.replace(".process.json", ".progress.jsonl"))
    journal = progress_path.open("x", encoding="utf-8")
    journal.write(json.dumps({"phase": "creating_cli", "cli_process_started": False, "elapsed_seconds": 0}) + "\n")
    journal.flush()
    try:
        proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=spec["env"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        with Path(result_path).open("x", encoding="utf-8") as output:
            json.dump({"exit_code": None, "stdout": "", "events": [],
                "diagnostic_codes": ["ACCESS_DENIED"] if getattr(exc, "winerror", None) == 5 else [],
                "error": "CLI_PROCESS_CREATION_FAILED", "cli_process_started": False,
                "process_creation_winerror": getattr(exc, "winerror", None)}, output, indent=2)
        journal.close()
        return
    events = queue.Queue()
    oversized = threading.Event()
    byte_counts = {"stdout": 0, "stderr": 0}
    kind = spec.get("kind", "plain")
    def read(stream, label):
        buffer = b""
        for chunk in iter(lambda: stream.read1(16384), b""):
            byte_counts[label] += len(chunk)
            if byte_counts[label] > 1024 * 1024:
                oversized.set()
                continue
            if label == "stderr":
                # Stderr may never end with a newline. Classify a short rolling
                # context immediately; plain trusted commands retain exact bytes.
                value = chunk if kind == "plain" else buffer + chunk
                buffer = value[-2048:]
                events.put((label, value.decode("utf-8", errors="replace")))
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                events.put((label, (line + b"\n").decode("utf-8", errors="replace")))
            if len(buffer) > 256 * 1024:
                oversized.set()
                buffer = b""
        if buffer and label != "stderr":
            events.put((label, buffer.decode("utf-8", errors="replace")))
        events.put((label, None))
    for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        threading.Thread(target=read, args=(stream, label), daemon=True).start()
    result = {"exit_code": None, "stdout": "", "stderr_categories": [], "events": [],
              "initialization": None, "control_response_keys": [], "prompt_sent": False,
              "hook_events": [], "diagnostic_codes": [], "error": None,
              "cli_process_started": True, "protocol_milestones": [], "sanitized_error_contexts": []}
    last_checkpoint = -1
    def checkpoint(phase="running", force=False):
        nonlocal last_checkpoint
        elapsed = time.monotonic() - launched
        if not force and elapsed - last_checkpoint < .5:
            return
        # Journal metadata only, never raw stdout/stderr, reasoning or responses.
        record = {key: result[key] for key in ("cli_process_started", "protocol_milestones", "diagnostic_codes",
            "sanitized_error_contexts", "stderr_categories", "error", "initialization", "prompt_sent", "hook_events")}
        record.update(phase=phase, elapsed_seconds=round(elapsed, 3), output_bytes=dict(byte_counts))
        journal.write(json.dumps(record) + "\n")
        journal.flush()
        last_checkpoint = elapsed
    checkpoint("cli_started", force=True)
    # Never send a user/model message before inspecting the initialization inventory.
    if kind.startswith("claude"):
        proc.stdin.write((json.dumps({"type": "control_request", "request_id": "dani_initialize",
                                     "request": {"subtype": "initialize"}}) + "\n").encode())
        proc.stdin.flush()
        # Empty SDK input stream: initialize control message only, never a user
        # message. EOF lets the CLI flush startup events without a conversation.
        if kind == "claude-inventory-eof":
            proc.stdin.close()
    else:
        proc.stdin.close()
    ended = set()
    # No prompt is ever sent before Claude reports its (empty) tool inventory. That wait is not a time limit: it ends
    # only after STARTUP_IDLE_SECONDS with no output at all, and any startup output renews it (CODEX_REVIEW_3 F7:
    # a fixed 8 seconds stopped a cold or busy CLI that was still visibly starting).
    # A spec may only make the window shorter (tests use a short one), never longer.
    startup_idle = min(float(spec.get("startup_idle_seconds") or STARTUP_IDLE_SECONDS), STARTUP_IDLE_SECONDS)
    startup_max = min(float(spec.get("startup_max_seconds") or STARTUP_MAX_SECONDS), STARTUP_MAX_SECONDS)
    init_deadline = time.monotonic() + startup_idle
    while len(ended) < 2:
        checkpoint()
        if oversized.is_set():
            result["error"] = "OUTPUT_LIMIT"
            proc.terminate()
            break
        if (kind.startswith("claude") and result["initialization"] is None
                and (time.monotonic() > init_deadline or time.monotonic() - launched > startup_max)):
            result["error"] = "NO_PREPROMPT_TOOL_INVENTORY"
            proc.terminate()
            break
        try:
            label, line = events.get(timeout=.1)
        except queue.Empty:
            if proc.poll() is not None and len(ended) == 2:
                break
            continue
        if line is not None and result["initialization"] is None:
            init_deadline = time.monotonic() + startup_idle   # still starting, visibly
        if line is None:
            ended.add(label)
            continue
        if kind == "plain":
            result["stdout"] += line
            continue
        if label == "stderr":
            if len(result["sanitized_error_contexts"]) < 32:
                detail = DIAGNOSTICS["error_detail"](line, spec["cwd"])
                if detail["codes"] or detail["native_windows_errors"] or detail["operation_contexts"]:
                    if not any(all(item.get(key) == value for key, value in detail.items())
                            for item in result["sanitized_error_contexts"] if item.get("stream") == "stderr"):
                        result["sanitized_error_contexts"].append({"stream": "stderr",
                            "elapsed_seconds": round(time.monotonic() - launched, 3), **detail})
            for code in DIAGNOSTICS["error_codes"](line):
                if code not in result["diagnostic_codes"]:
                    result["diagnostic_codes"].append(code)
            for token in ("quota", "rate limit", "login", "auth", "permission", "sandbox", "mxc", "config", "unknown", "invalid"):
                if token in line.lower() and token not in result["stderr_categories"]:
                    result["stderr_categories"].append(token)
            continue
        if kind == "codex-local-check":
            if line.strip() == "DANI_MXC_OK":
                result["stdout"] += "DANI_MXC_OK\n"
            continue
        if kind == "codex-help-check":
            # Store only a fixed signature, never arbitrary help/stderr text.
            if line.startswith("Usage: codex exec "):
                result["stdout"] = "DANI_EXEC_HELP_OK\n"
            continue
        try:
            event = json.loads(line)
        except ValueError:
            result["error"] = "NON_JSON_PROTOCOL"
            proc.terminate()
            break
        if not isinstance(event, dict):
            result["error"] = "INVALID_PROTOCOL_EVENT"
            proc.terminate()
            break
        if kind.startswith("claude"):
            if event.get("type") == "system" and event.get("subtype") in ("hook_started", "hook_progress", "hook_response"):
                result["hook_events"].append(event["subtype"])
                result["error"] = "HOOK_ACTIVITY_FORBIDDEN"
                proc.terminate()
                break
            if event.get("type") == "control_response":
                response = event.get("response", {}).get("response", {})
                if isinstance(response, dict):
                    result["control_response_keys"] = sorted(response.keys())
                # Separately approved diagnostic only: a synthetic message may
                # be necessary for this version to emit its actual inventory.
                # This branch is NEVER used by the integration pipeline.
                if ((kind == "claude-tool-diagnostic" or
                        kind == "claude-review" and spec.get("startup_inventory_verified") is True)
                        and not result["prompt_sent"]
                        and event.get("response", {}).get("request_id") == "dani_initialize"
                        and event.get("response", {}).get("subtype") == "success"):
                    proc.stdin.write((json.dumps({"type": "user", "message": {
                        "role": "user", "content": spec["prompt"]},
                        "parent_tool_use_id": None, "session_id": ""}) + "\n").encode())
                    proc.stdin.flush()
                    result["prompt_sent"] = True
            if event.get("type") == "system" and event.get("subtype") == "init":
                tools = event.get("tools")
                servers = event.get("mcp_servers")
                result["initialization"] = {"tools": tools, "mcp_servers_count": len(servers) if isinstance(servers, list) else None}
                if kind == "claude-tool-diagnostic":
                    # Observe names only and stop even if the inventory is safe.
                    # Do not run tools or continue to an agent review.
                    proc.terminate()
                    break
                # This prototype intentionally offers ZERO tools, including output-format tools.
                if tools != [] or servers != []:
                    result["error"] = "UNEXPECTED_REVIEWER_TOOL_OR_MCP"
                    proc.terminate()
                    break
                if kind in ("claude-inventory", "claude-inventory-eof"):
                    if not proc.stdin.closed:
                        proc.stdin.close()
                    proc.terminate()
                    break
                if not result["prompt_sent"]:
                    proc.stdin.write((json.dumps({"type": "user", "message": {"role": "user", "content": spec["prompt"]},
                                                 "parent_tool_use_id": None, "session_id": ""}) + "\n").encode())
                    proc.stdin.flush()
                    result["prompt_sent"] = True
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        result["error"] = "REVIEWER_TOOL_CALL_FORBIDDEN"
                        proc.terminate()
            if event.get("type") == "result":
                result["events"].append({"type": "result", "subtype": event.get("subtype"),
                                         "is_error": event.get("is_error", False), "result": event.get("result"),
                                         "usage": event.get("usage")})
                proc.stdin.close()
        else:
            # Preserve structured completion, not shell output or reasoning transcripts.
            typ = event.get("type")
            if (spec.get("forbid_tools") is True and typ in ("item.started", "item.updated", "item.completed")
                    and event.get("item", {}).get("type") not in ("agent_message", "reasoning")):
                result["error"] = "DIAGNOSTIC_TOOL_USE_FORBIDDEN"
                proc.terminate()
                break
            if typ in ("thread.started", "turn.started", "turn.completed", "turn.failed", "error"):
                if len(result["protocol_milestones"]) < 256:
                    result["protocol_milestones"].append({"type": typ,
                        "elapsed_seconds": round(time.monotonic() - launched, 3)})
            if typ in ("turn.failed", "error"):
                detail = DIAGNOSTICS["error_detail"](json.dumps(event), spec["cwd"])
                if len(result["sanitized_error_contexts"]) < 32:
                    result["sanitized_error_contexts"].append({"stream": "stdout", **detail})
                for code in detail["codes"]:
                    if code not in result["diagnostic_codes"]:
                        result["diagnostic_codes"].append(code)
            if typ in ("turn.completed", "turn.failed", "error"):
                result["events"].append({"type": typ, "usage": event.get("usage")})
            if typ == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                result["events"].append({"type": "final", "text": event["item"].get("text")})
    try:
        result["exit_code"] = proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        result["error"] = result["error"] or "CLI_DID_NOT_EXIT"
    checkpoint("cli_exited", force=True)
    journal.close()
    with Path(result_path).open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)


def activity_token(path):
    """What changes while a session is doing something: its output so far and its protocol milestones."""
    progress = last_progress(path) or {}
    return json.dumps([progress.get("output_bytes"), len(progress.get("protocol_milestones") or []),
                       progress.get("phase")], sort_keys=True)


def run(spec, directory, timeout=120, watch=None, idle=None, activity=None):
    """Create trusted worker suspended, contain before resume, kill whole job on every exit.

    timeout: a fixed deadline in seconds, or None for none. idle: stop only after this many seconds with no sign of
    work (reason STALLED), so a long but active session is never cut off. activity: a callable whose value changes
    while there is work (default: the session's own output and milestones)."""
    memory_limit = spec.get('job_memory_limit_bytes')
    if memory_limit is not None and (type(memory_limit) is not int or not 64 * 1024 * 1024 <= memory_limit <= 4 * 1024 ** 3):
        raise ValueError('INVALID_JOB_MEMORY_LIMIT')
    directory = Path(directory)
    stem = uuid.uuid4().hex
    spec_path, result_path = directory / (stem + ".spec.json"), directory / (stem + ".process.json")
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    win = runpy.run_path(str(WIN_API), run_name="prototype_job_api")
    job = win["check"](win["createjob"](None, None))
    pi = win["PROCESS"]()
    reason = None
    observed = {}
    query_ok = True
    assigned = False
    statistics = {}
    failure_evidence = {}
    job_empty = False
    terminate_ok = False
    started = time.monotonic()
    try:
        limits = win["LIMITS"]()
        limits.basic.flags = 0x2000
        if memory_limit is not None:
            limits.basic.flags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY, in addition to kill-on-close.
            limits.job_mem = memory_limit
        win["check"](win["setjob"](job, 9, c.byref(limits), c.sizeof(limits)))
        if memory_limit is not None:
            verified_limits = win['LIMITS']()
            win['check'](win['queryjob'](job, 9, c.byref(verified_limits), c.sizeof(verified_limits), None))
            if not verified_limits.basic.flags & 0x200 or verified_limits.job_mem != memory_limit:
                raise RuntimeError('JOB_MEMORY_LIMIT_NOT_VERIFIED')
        command = c.create_unicode_buffer(subprocess.list2cmdline([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                                                                 "--worker", str(spec_path), str(result_path)]))
        si = win["STARTUP"]()
        si.cb = c.sizeof(si)
        win["check"](win["create"](sys.executable, command, None, None, False, 0x4 | 0x08000000,
                                    None, str(BASE), c.byref(si), c.byref(pi)))
        win["check"](win["assign"](job, pi.process))
        assigned = True
        if win["resume"](pi.thread) == 0xFFFFFFFF:
            raise c.WinError(c.get_last_error())
        deadline = time.monotonic() + timeout if timeout else None
        sign = activity or (lambda: activity_token(directory / (stem + ".progress.jsonl")))
        last_sign, last_change, next_look = None, time.monotonic(), 0.0
        while win["wait"](pi.process, 100) == 258:
            try:
                pids = job_pids(win, job, statistics)
            except RuntimeError:
                query_ok = False
                raise
            for pid in pids:
                if pid not in observed:
                    handle = win["openprocess"](0x100000, False, pid)
                    if handle:
                        observed[pid] = handle
            if watch:
                watch()
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                reason = "TIMEOUT"
                break
            if idle and now >= next_look:
                next_look = now + 2
                current = sign()
                if current != last_sign:
                    last_sign, last_change = current, now
                elif now - last_change >= idle:
                    reason = "STALLED"
                    break
    except BaseException as exc:
        reason = reason or (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ProcessFailure(reason, failure_evidence) from exc
    finally:
        terminate_ok = bool(win["terminatejob"](job, 124))
        # Keep the job handle until its entire process list is empty. This also
        # verifies cleanup of members that cannot be opened individually.
        try:
            cleanup_deadline = time.monotonic() + 2
            while True:
                if not job_pids(win, job, statistics):
                    job_empty = True
                    break
                if time.monotonic() >= cleanup_deadline:
                    break
                time.sleep(.02)
        except RuntimeError:
            query_ok = False
        win["close"](job)
        if pi.process:
            if win["wait"](pi.process, 1000) == 258:
                win["terminateprocess"](pi.process, 124)
                win["wait"](pi.process, 5000)
            win["close"](pi.process)
        if pi.thread:
            win["close"](pi.thread)
        stopped = all(win["wait"](handle, 5000) == 0 for handle in observed.values())
        for handle in observed.values():
            win["close"](handle)
        containment = {"assigned_before_resume": assigned, "job_queries_passed": query_ok,
            "observed_processes": len(observed), "all_observed_processes_stopped": stopped,
            "job_termination_succeeded": terminate_ok, "job_empty_after_termination": job_empty,
            "pid_query_statistics": statistics,
            "job_memory_limit_bytes": memory_limit,
            "elapsed_seconds": round(time.monotonic() - started, 3), "termination_reason": reason}
        with (directory / (stem + ".containment.json")).open("x", encoding="utf-8") as output:
            json.dump(containment, output, indent=2)
        failure_evidence.update(error=reason, partial=True, containment=containment,
            progress=last_progress(directory / (stem + ".progress.jsonl")),
            artifacts={"spec": str(spec_path), "progress": str(directory / (stem + ".progress.jsonl")),
                "containment": str(directory / (stem + ".containment.json"))})
    if reason:
        raise ProcessFailure(reason, failure_evidence)
    if not result_path.is_file():
        raise ProcessFailure("NO_PROCESS_RESULT", failure_evidence)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not stopped or not job_empty or not terminate_ok or not query_ok:
        raise ProcessFailure("PROCESS_CONTAINMENT_CLEANUP_FAILED", failure_evidence)
    result["containment"] = containment
    return result


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        raise SystemExit("Internal process worker only")
    worker(sys.argv[2], sys.argv[3])
