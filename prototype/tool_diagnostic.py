"""Targeted Claude tool identification. Prepare by default; no inference without approval."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import uuid

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="tool_diagnostic_settings")
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
PROMPT = "Synthetic tool-inventory diagnostic. Do not use any tools or access any files. Return only {\"ok\":true}."


def classify(name):
    # Classification is not authorization: the live reviewer still permits ZERO tools.
    if name == "StructuredOutput":
        return "INTERNAL_FINAL_RESPONSE_FORMATTER_STATICALLY_READ_ONLY"
    if name in ("Read", "Grep", "Glob"):
        return "FILESYSTEM_READ_CAPABILITY_FORBIDDEN_IN_ZERO_TOOL_REVIEWER"
    if name in ("Bash", "PowerShell", "REPL", "Edit", "Write", "NotebookEdit", "Agent", "Task", "Monitor"):
        return "EXECUTABLE_OR_MODIFYING_CAPABILITY_FORBIDDEN"
    return "UNKNOWN_UNAUTHORIZED_STOP"


def static_evidence():
    binary = Path(APP["CLAUDE"]).read_bytes()
    start = binary.find(b'ra="StructuredOutput";')
    end = binary.find(b'var GQo=me()', start)
    fragment = binary[start:end] if start >= 0 and end > start else b""
    checks = {
        "single_named_formatter": binary.count(b'"StructuredOutput"') == 1,
        "declares_read_only": b'isReadOnly(){return!0}' in fragment,
        "not_mcp": b'isMcp:!1' in fragment,
        "closed_world": b'isOpenWorld(){return!1}' in fragment,
        "call_only_callback_and_final_result": b'async call(r){return n(r),{data:"Structured output provided successfully",structured_output:r,endsTurn:!0}}' in fragment,
    }
    return {"binary_sha256": hashlib.sha256(binary).hexdigest(), "checks": checks,
            "formatter_fragment": fragment.decode("utf-8") if all(checks.values()) else None,
            "historical_tool_identity": "NOT_RECOVERABLE_FROM_REDACTED_ARTIFACT",
            "classification_scope": "INSTALLED_FORMATTER_IMPLEMENTATION_NOT_HISTORICAL_RUNTIME_IDENTITY"}


def prepare():
    run = APP["approved_run"](BASE / "runs/20261018T120602Z_8927f7fa")
    folder = BASE / ("tool_identification_" + uuid.uuid4().hex[:8])
    folder.mkdir()
    static = static_evidence()
    if not all(static["checks"].values()):
        raise RuntimeError("STATIC_CLASSIFICATION_FAILED_STOP")
    argv = APP["commands"](run)["claude"]
    argv[argv.index("--max-turns") + 1] = "1"
    argv += ["--json-schema", json.dumps(SCHEMA)]
    plan = {"argv": argv, "cwd": str(run / "review_snapshot"),
            "kind": "claude-tool-diagnostic", "prompt": PROMPT,
            "timeout_seconds": 15, "run": str(run), "binary_sha256": static["binary_sha256"],
            "warning": "Separate quota approval required. Sending a user message can start inference before cancellation at init."}
    APP["save"](folder / "static_evidence.json", static)
    APP["save"](folder / "plan.json", plan)
    print(folder)


def execute(folder):
    folder = Path(folder).resolve()
    if folder.parent != BASE or not folder.name.startswith("tool_identification_"):
        raise ValueError("INVALID_DIAGNOSTIC_FOLDER")
    plan = json.loads((folder / "plan.json").read_text())
    if hashlib.sha256(Path(APP["CLAUDE"]).read_bytes()).hexdigest() != plan["binary_sha256"]:
        raise RuntimeError("CLAUDE_BINARY_CHANGED_STOP")
    run = APP["approved_run"](plan["run"])
    expected = APP["commands"](run)["claude"]
    expected[expected.index("--max-turns") + 1] = "1"
    expected += ["--json-schema", json.dumps(SCHEMA)]
    if (plan["argv"] != expected or plan["cwd"] != str(run / "review_snapshot")
            or plan["kind"] != "claude-tool-diagnostic" or plan["prompt"] != PROMPT):
        raise RuntimeError("DIAGNOSTIC_PLAN_CHANGED_STOP")
    APP["preflight"](run)
    # One attempt only. Never repeat on timeout, missing inventory or failure.
    APP["save"](folder / "attempt.json", {"started": True, "quota_approval_supplied": True})
    try:
        result = APP["PROCESS"]["run"]({"argv": plan["argv"], "cwd": plan["cwd"],
            "kind": plan["kind"], "prompt": plan["prompt"], "env": APP["env"](run)}, folder, timeout=15)
        initial = result.get("initialization") or {}
        tools = initial.get("tools")
        classifications = {name: classify(name) for name in tools} if isinstance(tools, list) and all(isinstance(n, str) for n in tools) else {}
        report = {"runtime": result, "classifications": classifications,
                  "historical_identity": "STILL_NOT_PROVEN_BY_REPRODUCTION",
                  "current_zero_tool_profile": "NOT_TESTED_THIS_PLAN_ADDS_SCHEMA_FOR_DIAGNOSIS",
                  "ready_for_integration": False,
                  "quota_consumption": "POSSIBLE_ONE_SYNTHETIC_SESSION_NO_RETRY"}
    except Exception as exc:
        report = {"ready_for_integration": False, "error": type(exc).__name__,
                  "quota_consumption": "POSSIBLE_ONE_SYNTHETIC_SESSION_NO_RETRY"}
    APP["save"](folder / "result.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "execute"])
    parser.add_argument("--folder")
    parser.add_argument("--diagnostic-quota-approved", action="store_true")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
    elif not args.diagnostic_quota_approved:
        parser.error("Separate explicit diagnostic quota approval required; no CLI launched")
    else:
        execute(args.folder)
