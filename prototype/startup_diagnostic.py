"""Model-free startup localization only; never invokes exec, turn/start or a model."""
import argparse
import json
from pathlib import Path
import runpy
import tomllib
import uuid

BASE = Path(__file__).resolve().parent
HOST = runpy.run_path(str(BASE / "host_launcher.py"), run_name="startup_host_checks")
APP = HOST["APP"]


def diagnostic_argv(run):
    live = APP["commands"](run)["codex"]
    config = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
    if "sandbox_mode" in config or "sandbox_workspace_write" in config:
        raise RuntimeError("LEGACY_USER_POLICY_PREVENTS_SAFE_STARTUP_DIAGNOSTIC")
    # Unlike exec, app-server loads user config. Disable all executable
    # integrations explicitly and preserve this non-equivalence in the report.
    extra = APP["overrides"]({"mcp_servers": {name: {"enabled": False} for name in config.get("mcp_servers", {})},
        "notify": []})
    return live[:live.index("exec")] + extra + ["app-server", "--listen", "stdio://"]


def diagnose(run):
    folder = run / "artifacts" / ("startup_localization_" + uuid.uuid4().hex[:8])
    folder.mkdir()
    report = {"scope": "MODEL_FREE_STARTUP_LOCALIZATION", "model_calls": 0,
        "verdict": "UNVERIFIED", "run": str(run), "exec_equivalent": False,
        "limitation": "App Server thread setup is diagnostic only; it loads user config unlike exec --ignore-user-config."}
    try:
        if not HOST["check"](run):
            raise RuntimeError("MODEL_FREE_HOST_CHECK_FAILED_NO_STARTUP_PROBE")
        before = {name: APP["inventory"](run / name) for name in ("repo", "control", "review_snapshot", ".claude")}
        mcp_before = (run / ".mcp.json").read_bytes()
        def unchanged():
            for name, inventory in before.items():
                if APP["inventory"](run / name) != inventory:
                    raise RuntimeError("DIAGNOSTIC_PROTECTED_INPUT_CHANGED")
            if (run / ".mcp.json").read_bytes() != mcp_before:
                raise RuntimeError("DIAGNOSTIC_MCP_CONFIGURATION_CHANGED")
            if any((run / "artifacts" / (name + ".marker")).exists() for name in ("hook", "mcp")):
                raise RuntimeError("DIAGNOSTIC_CANARY_EXECUTED")
        spec_path = folder / "startup_spec.json"
        APP["save"](spec_path, {"argv": diagnostic_argv(run), "cwd": str(run / "repo"), "env": APP["codex_env"](run)})
        result = APP["PROCESS"]["run"]({"argv": [APP["PYTHON"], "-I", "-B", str(HOST["PROBE"]), str(spec_path)],
            "cwd": str(folder), "env": APP["codex_env"](run), "kind": "plain"}, folder, timeout=15, watch=unchanged)
        report["process"] = result
        unchanged()
        if result.get("error") or result["exit_code"]:
            raise RuntimeError("MODEL_FREE_PROBE_PROCESS_FAILED")
        protocol = json.loads(result["stdout"])
        report["protocol"] = protocol
        if protocol.get("turns_started") != 0:
            raise RuntimeError("MODEL_FREE_PROTOCOL_CONTRACT_VIOLATED")
        if protocol.get("error"):
            report["verdict"] = "FAIL"
            report["reason"] = protocol["error"]
        elif protocol.get("thread_created") and (protocol.get("active_permission_profile") or {}).get("id") == "toydev":
            report["verdict"] = "PASS_DIAGNOSTIC_ONLY"
        else:
            raise RuntimeError("STARTUP_PROFILE_NOT_VERIFIED")
    except Exception as exc:
        report["verdict"] = "BLOCKED"
        report["reason"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    APP["save"](folder / "report.json", report)
    print(json.dumps(report, indent=2))
    print("Sanitized evidence: " + str(folder / "report.json"))
    return 0 if report["verdict"] == "PASS_DIAGNOSTIC_ONLY" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    raise SystemExit(diagnose(APP["approved_run"](parser.parse_args().run)))
