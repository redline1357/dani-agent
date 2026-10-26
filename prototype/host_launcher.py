"""One normal-terminal launcher: grouped model-free checks, then explicit local approval."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import uuid

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="host_launcher_settings")
DIAGNOSTICS = runpy.run_path(str(BASE / "diagnostics.py"), run_name="host_launcher_diagnostics")
PROBE = BASE / "second_failure_f3fb4430/session_probe.py"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def require_host(context):
    if context.get("appcontainer") is not False:
        raise RuntimeError("HOST_TERMINAL_REQUIRED_APPCONTAINER_DETECTED")
    if context.get("elevated") is not False:
        raise RuntimeError("NON_ELEVATED_TERMINAL_REQUIRED")


def verify_offline_tests():
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in BASE.glob("*.py")}
    sources["second_failure_f3fb4430/session_probe.py"] = hashlib.sha256(PROBE.read_bytes()).hexdigest()
    for path in list((BASE.parent / "dani_agent").glob("*.py")) + [BASE.parent / "DaniAgent.py", BASE.parent / "DaniDashboard.py"]:
        sources["../" + path.relative_to(BASE.parent).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in BASE.glob("offline_test_results*.json"):
        record = json.loads(path.read_text())
        if (record.get("verdict") == "PASS" and record.get("model_calls") == 0
                and record.get("sources_sha256") == sources):
            return {"verdict": "PASS", "tests_run": record["tests_run"], "evidence": str(path)}
    raise RuntimeError("CURRENT_SOURCES_REQUIRE_OFFLINE_TESTS_BEFORE_LIVE")


def exec_help_argv(run):
    # Drop the task prompt, append --help. Clap exits without initializing a
    # session or sending a turn. This validates arguments, NOT session startup.
    return APP["commands"](run)["codex"][:-1] + ["--help"]


def exec_probe(run, directory):
    return APP["PROCESS"]["run"]({"argv": exec_help_argv(run), "cwd": str(run / "repo"),
        "env": APP["codex_env"](run), "kind": "codex-help-check"}, directory, timeout=15)


def mxc_probe(run, directory, profile):
    if profile not in ("toydev", "toytest"):
        raise RuntimeError("UNKNOWN_MXC_DIAGNOSTIC_PROFILE")
    flags = {**APP["permissions"](run, profile, profile == "toydev"), "features.hooks": False,
             "features.plugins": False, "features.remote_plugin": False, "features.multi_agent": False}
    argv = [APP["CODEX"]] + APP["overrides"](flags) + ["sandbox", "--include-managed-config",
        "--permission-profile", profile, "-C", str(run / "repo"), "--",
        r"C:\Windows\System32\cmd.exe", "/d", "/c", "echo", "DANI_MXC_OK"]
    return APP["PROCESS"]["run"]({"argv": argv, "cwd": str(run / "repo"), "env": APP["codex_env"](run),
        "kind": "codex-local-check"}, directory, timeout=15)


def pass_contained_marker(result, marker):
    contained = result.get("containment", {})
    return (result.get("exit_code") == 0 and result.get("error") is None
            and result.get("stdout", "").strip() == marker
            and contained.get("assigned_before_resume") is True
            and contained.get("job_queries_passed") is True
            and contained.get("job_empty_after_termination") is True
            and contained.get("job_termination_succeeded") is True
            and contained.get("all_observed_processes_stopped") is True)


def pass_mxc(result):
    return pass_contained_marker(result, "DANI_MXC_OK")


def consume_recorded_approval(run):
    """Accept the Product Owner's recorded approval once, bound to this run and commands."""
    approval = json.loads((run / "artifacts/project_owner_live_approval.json").read_text())
    expected = {"run": str(run), "codex_sessions": 1, "claude_sessions": 1,
        "seconds_per_agent": 120, "retry_count": 0, "api_fallback": False,
        "model_upgrades": False, "commands_sha256": digest(APP["commands"](run))}
    if (approval.get("approval_source") != "EXPLICIT_PRODUCT_OWNER_CHAT_APPROVAL"
            or any(approval.get(key) != value for key, value in expected.items())):
        raise RuntimeError("RECORDED_LIVE_APPROVAL_INVALID_STOP")
    # Exclusive creation also prevents two launcher processes consuming approval.
    APP["save"](run / "artifacts/live_approval_consumed.json", approval)


def check(run):
    directory = run / "artifacts" / ("host_check_" + uuid.uuid4().hex[:8])
    directory.mkdir()
    report = {"verdict": "FAIL", "model_calls": 0, "run": str(run), "checks": {}}
    try:
        context = DIAGNOSTICS["execution_context"]()
        report["execution_context"] = context
        require_host(context)  # Never try to escape or initialize MXC in this app's token.
        report["offline_tests"] = verify_offline_tests()
        if (run / "result.json").exists() or (run / "artifacts/codex_attempt.json").exists():
            raise RuntimeError("USED_RUN_NO_LIVE_RETRY")
        APP["preflight"](run)
        APP["verified_startup_gate"](run)
        original_environment = {k.casefold(): v for k, v in APP["env"](run).items()}
        corrected_environment = {k.casefold(): v for k, v in APP["codex_env"](run).items()}
        names = ("SystemRoot", "WINDIR", "SystemDrive", "LOCALAPPDATA", "TEMP", "TMP")
        report["windows_environment"] = {name: {"original_present": bool(original_environment.get(name.casefold())),
            "corrected_present": bool(corrected_environment.get(name.casefold())),
            "changed": original_environment.get(name.casefold()) != corrected_environment.get(name.casefold())} for name in names}
        report["frozen_appcontainer_runtime_paths"] = any("packages" in value.casefold() and "sandbox." in value.casefold()
            for key, value in original_environment.items() if key in ("localappdata", "temp", "tmp"))
        before = {name: APP["inventory"](run / name) for name in ("repo", "control", "review_snapshot", ".claude")}
        root_entries = sorted(path.name for path in run.iterdir())
        mcp_before = (run / ".mcp.json").read_bytes()
        def unchanged():
            if any(APP["inventory"](run / name) != value for name, value in before.items()):
                raise RuntimeError("HOST_CHECK_PROTECTED_INPUT_CHANGED")
            if sorted(path.name for path in run.iterdir()) != root_entries or (run / ".mcp.json").read_bytes() != mcp_before:
                raise RuntimeError("HOST_CHECK_CONFIGURATION_CHANGED")
            if any((run / "artifacts" / (label + ".marker")).exists() for label in ("hook", "mcp")):
                raise RuntimeError("HOST_CHECK_CANARY_EXECUTED")
        report["session_initialization"] = "UNVERIFIED_REQUIRES_SEPARATELY_APPROVED_CODEX_EXEC"
        report["historical_app_server_denial"] = "UNRESOLVED_NOT_AN_EXEC_EQUIVALENCE_TEST"
        report["checks"]["exec_arguments"] = exec_probe(run, directory)
        unchanged()
        if not pass_contained_marker(report["checks"]["exec_arguments"], "DANI_EXEC_HELP_OK"):
            raise RuntimeError("EXEC_ARGUMENT_CHECK_NOT_VERIFIED")
        for profile in ("toydev", "toytest"):
            result = mxc_probe(run, directory, profile)
            report["checks"][profile + "_mxc"] = result
            unchanged()
            if not pass_mxc(result):
                raise RuntimeError(profile.upper() + "_MXC_CONTAINED_STARTUP_NOT_VERIFIED")
        report["diagnosis"] = "EXEC_MODEL_FREE_PREFLIGHT_PASSED_SESSION_NOT_STARTED"
        report["verdict"] = "PASS"
        gate = {"verdict": "PASS", "binary_sha256": hashlib.sha256(Path(APP["CODEX"]).read_bytes()).hexdigest(),
            "argv": APP["commands"](run)["codex"], "environment_sha256": digest(APP["codex_env"](run)),
            "orchestrator_sha256": hashlib.sha256((BASE / "orchestrator.py").read_bytes()).hexdigest(),
            "process_sha256": hashlib.sha256((BASE / "process.py").read_bytes()).hexdigest(),
            "extra_sources": {name: hashlib.sha256((BASE / name).read_bytes()).hexdigest()
                for name in ("diagnostics.py", "host_launcher.py", "second_failure_f3fb4430/session_probe.py")},
            "context": context, "preflight_kind": "EXEC_CLI_MODEL_FREE_V1", "exec_arguments_pass": True,
            "toydev_mxc_pass": True, "toytest_mxc_pass": True,
            "session_initialization": report["session_initialization"],
            "evidence": str(directory / "report.json")}
        gate_path = run / "artifacts/mxc_host_gate.json"
        if gate_path.exists():
            raise RuntimeError("HOST_GATE_ALREADY_RECORDED_NO_OVERWRITE")
        APP["save"](gate_path, gate)
    except Exception as exc:
        report["verdict"] = "FAIL"
        report["reason"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    APP["save"](directory / "report.json", report)
    print(json.dumps(report, indent=2))
    print("Evidence saved automatically: " + str(directory / "report.json"))
    return report["verdict"] == "PASS"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    approval_mode = parser.add_mutually_exclusive_group()
    approval_mode.add_argument("--interactive-approval", action="store_true")
    approval_mode.add_argument("--approved-once", action="store_true")
    args = parser.parse_args()
    run = APP["approved_run"](args.run)
    if not check(run):
        return 1
    if not args.interactive_approval and not args.approved_once:
        return 0
    print("Model-free checks passed. Exact planned commands: " + str(run / "control/planned_commands.json"))
    print("One Codex + one Claude session, 120s each; existing subscriptions only; no retry or API fallback.")
    if args.approved_once:
        consume_recorded_approval(run)
    else:
        expected = "APPROVE " + run.name
        try:
            approved = input("To approve this ONE live attempt, type " + expected + ": ").strip() == expected
        except EOFError:
            approved = False
        if not approved:
            print("No model calls started.")
            return 0
        APP["save"](run / "artifacts/local_live_approval.json", {"explicit_local_human_approval": True,
            "run": str(run), "codex_sessions": 1, "claude_sessions": 1, "seconds_per_agent": 120, "retry_count": 0})
    APP["preflight"](run)
    result = APP["pipeline"](run)
    print(json.dumps(result, indent=2))
    print("Final report: " + str(run / "REPORT.md"))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
