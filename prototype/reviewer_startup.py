"""One separately approved, schema-free reviewer startup; never launches Codex inference."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import stat
import subprocess
import uuid

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="final_startup_settings")


def execute(*, profile=False):
    """profile=True: the Connect page's approved check that creates this installation's reviewer profile.

    Each attempt uses its own fresh run; the child environment is frozen BEFORE the startup so every
    run later copied from this profile (prepare_live_run.py) is bound to exactly the verified one."""
    if not profile:
        # A single approved attempt globally for this final diagnostic, no retries.
        attempt = BASE / "final_reviewer_startup_attempt.json"
        APP["save"](attempt, {"started": True, "deadline_seconds": 15, "max_turns": 1})
    run = APP["prepare"](BASE / "task.json")
    APP["preflight"](run)
    if profile:
        APP["save"](run / "artifacts/verified_startup_environment.json", APP["OLD_GATE"]["child_env"]())
    (run / ".claude").mkdir()
    canary = run / "control/startup_canary.py"
    canary.write_text("import sys\nfrom pathlib import Path\n"
        "assert sys.argv[1] in ('hook','mcp','positive')\n"
        "p=Path(__file__).resolve().parent.parent/'artifacts'/(sys.argv[1]+'.marker')\n"
        "with p.open('x') as f: f.write('SYNTHETIC STARTUP CANARY')\n", encoding="utf-8")
    command = subprocess.list2cmdline([APP["PYTHON"], "-I", "-B", str(canary), "hook"])
    APP["save"](run / ".claude/settings.json", {"disableAllHooks": False,
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}})
    APP["save"](run / ".mcp.json", {"mcpServers": {"synthetic_denied_startup_canary": {
        "command": APP["PYTHON"], "args": ["-I", "-B", str(canary), "mcp"]}}})
    snapshot = run / "review_snapshot/startup_probe.txt"
    snapshot.write_text("SYNTHETIC READ-ONLY STARTUP PROBE\n", encoding="utf-8")
    snapshot.chmod(stat.S_IREAD)
    # Verify marker plumbing independently, using trusted local code, not a hook.
    subprocess.run([APP["PYTHON"], "-I", "-B", str(canary), "positive"],
        cwd=run, env=APP["env"](run), capture_output=True, check=True, timeout=5)
    argv = APP["commands"](run)["claude"]
    assert "--json-schema" not in argv and argv[argv.index("--max-turns") + 1] == "1"
    before = {name: APP["inventory"](run / name) for name in ("repo", "control", "review_snapshot", ".claude")}
    mcp_hash = hashlib.sha256((run / ".mcp.json").read_bytes()).hexdigest()
    root_children = sorted(p.name for p in run.iterdir())
    configuration = {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                     for name in (".claude/settings.json", ".mcp.json")}
    def watch():
        if any((run / "artifacts" / (name + ".marker")).exists() for name in ("hook", "mcp")):
            raise RuntimeError("STARTUP_CANARY_EXECUTED_STOP")
        if (any(APP["inventory"](run / name) != files for name, files in before.items())
                or hashlib.sha256((run / ".mcp.json").read_bytes()).hexdigest() != mcp_hash
                or sorted(p.name for p in run.iterdir()) != root_children):
            raise RuntimeError("UNEXPECTED_FILE_CHANGE_STOP")
    APP["save"](run / "artifacts/startup_plan.json", {"argv": argv, "cwd": str(run / "review_snapshot"),
        "prompt": "Synthetic startup inventory only. Do not use tools or access files. Reply OK.",
        "deadline_seconds": 15, "max_turns": 1, "before": before})
    report = {"verdict": "FAIL", "run": str(run), "criteria": {}, "runtime": None}
    try:
        result = APP["PROCESS"]["run"]({"argv": argv, "cwd": str(run / "review_snapshot"),
            "kind": "claude-tool-diagnostic", "prompt": "Synthetic startup inventory only. Do not use tools or access files. Reply OK.",
            "env": APP["env"](run)}, run / "artifacts", timeout=15, watch=watch)
        report["runtime"] = result
        watch()
        initial = result.get("initialization") or {}
        containment = result.get("containment", {})
        report["criteria"] = {"zero_tools": initial.get("tools") == [],
            "zero_mcp": initial.get("mcp_servers_count") == 0,
            "no_hook_execution": not result.get("hook_events") and (run / "artifacts/positive.marker").exists(),
            "no_unexpected_file_changes": True,
            "process_containment": containment.get("assigned_before_resume") is True
                and containment.get("job_queries_passed") is True and containment.get("observed_processes", 0) >= 2
                and containment.get("all_observed_processes_stopped") is True}
        if result.get("error") is None and all(report["criteria"].values()):
            report["verdict"] = "PASS"
            gate = {"verdict": "PASS", "criteria": report["criteria"], "initialization": initial,
                "argv": argv, "cwd": str(run / "review_snapshot"),
                "binary_sha256": hashlib.sha256(Path(APP["CLAUDE"]).read_bytes()).hexdigest(),
                "control_inventory": before["control"], "canary_configuration": configuration,
                "child_environment_sha256": hashlib.sha256(json.dumps(APP["env"](run), sort_keys=True).encode()).hexdigest(),
                "source": "Separately approved one-turn schema-free runtime diagnostic; no tool use observed"}
            APP["save"](run / "artifacts/reviewer_startup_gate.json", gate)
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    APP["save"](run / "artifacts/final_startup_result.json", report)
    if profile:
        return report
    APP["save"](BASE / "final_reviewer_startup_result.json", report)
    print(json.dumps(report, indent=2))
    return report["verdict"] == "PASS"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostic-quota-approved", action="store_true")
    args = parser.parse_args()
    if not args.diagnostic_quota_approved:
        parser.error("Separate explicit diagnostic approval required")
    raise SystemExit(0 if execute() else 1)
