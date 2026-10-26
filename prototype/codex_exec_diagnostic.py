"""One separately approved, read-only Codex exec diagnostic. Never calls Claude."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import uuid

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="exec_diagnostic_app")
HOST = runpy.run_path(str(BASE / "host_launcher.py"), run_name="exec_diagnostic_host")

# Official RUST_LOG filtering is diagnostic only. Never enable HTTP/auth trace,
# save raw log messages, or alter provider routing/permissions to get past a stop.
STARTUP_LOG_FILTER = ('error,codex_core::codex=debug,codex_core::fs_client=debug,'
    'codex_core::exec_server=debug,codex_sandboxing=debug,codex_mxc_sandbox=debug')


def diagnostic_env(run):
    return {**APP['codex_env'](run), 'RUST_LOG': STARTUP_LOG_FILTER}


def runtime_image_probe(run, folder, native, unchanged):
    """No inference: compare reads and CLI --version under the exact MXC policy.

    Only fixed public executable/fixture paths are touched. Child CLI output is
    consumed in memory; only fixed booleans and native error numbers are emitted.
    This does not establish that session initialization uses the same operation.
    """
    program = '''import json, subprocess
from pathlib import Path
targets = TARGETS
checks = {}
for name, path in targets.items():
    try:
        with Path(path).open('rb') as source:
            source.read(1)
        checks[name] = {'read': True}
    except OSError as exc:
        checks[name] = {'read': False, 'winerror': getattr(exc, 'winerror', None), 'errno': exc.errno}
try:
    result = subprocess.run([targets['codex_image'], '--version'], capture_output=True, timeout=8,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    checks['codex_version'] = {'process_started': True, 'exit_code': result.returncode,
        'expected_version': EXPECTED in result.stdout}
except OSError as exc:
    checks['codex_version'] = {'process_started': False, 'winerror': getattr(exc, 'winerror', None), 'errno': exc.errno}
except subprocess.TimeoutExpired:
    checks['codex_version'] = {'timeout': True}
print(json.dumps({'scope': 'MODEL_FREE_MXC_IMAGE_CHECK', 'checks': checks}), flush=True)
'''.replace('TARGETS', repr({'codex_image': APP['CODEX'], 'workspace_source': str(run / 'repo/toy_math.py'),
        'test_source': str(run / 'repo/test_toy_math.py'), 'output_schema': str(run / 'control/developer_schema.json')})
        ).replace('EXPECTED', repr(confirmed_version().encode()))
    # Keep every config argument identical; replace only the harmless workload.
    argv = native[:native.index('--') + 1] + [APP['PYTHON'], '-I', '-B', '-c', program]
    output = APP['PROCESS']['run']({'argv': argv, 'cwd': str(run / 'repo'), 'env': diagnostic_env(run),
        'kind': 'plain'}, folder, timeout=15, watch=unchanged)
    if output.get('error') or output.get('exit_code') != 0:
        raise RuntimeError('MODEL_FREE_IMAGE_PROBE_DID_NOT_COMPLETE')
    observed = json.loads(output['stdout'])
    # Enforce a fixed result vocabulary even if a child was unexpectedly noisy.
    allowed = {'read', 'winerror', 'errno', 'process_started', 'exit_code', 'expected_version', 'timeout'}
    if (set(observed) != {'scope', 'checks'} or observed['scope'] != 'MODEL_FREE_MXC_IMAGE_CHECK'
            or set(observed['checks']) != {'codex_image', 'workspace_source', 'test_source', 'output_schema', 'codex_version'}
            or any(set(value) - allowed or any(val is not None and type(val) not in (bool, int) for val in value.values())
                for value in observed['checks'].values())):
        raise RuntimeError('UNEXPECTED_IMAGE_PROBE_OUTPUT')
    APP['save'](folder / 'runtime_image_probe.json', {**observed, 'containment': output.get('containment')})
    return observed


def confirmed_version():
    value = APP["TOOLS"].get("codex", {}).get("version")
    if not value:
        raise RuntimeError("CONNECT_CODEX_NOT_CONFIRMED")
    return value


def plan_argv(run, plan):
    # The model under test is part of the approved plan; the toy fixture model is the default.
    return APP["commands"](run, exec_diagnostic=True, model=plan.get("model", APP["TOY_CODEX_MODEL"]))["codex"]


def codex_only_preflight(run):
    HOST["require_host"](HOST["DIAGNOSTICS"]["execution_context"]())
    HOST["verify_offline_tests"]()
    # Enforce the same provider override prohibition without invoking Claude.
    import os
    if any(value and (key.upper().startswith("ANTHROPIC_") or key.upper() in (
            "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
            or key.upper().startswith("CLAUDE_CODE_USE_")) for key, value in os.environ.items()):
        raise RuntimeError("PROVIDER_ENVIRONMENT_OVERRIDE_PRESENT")
    for path in (Path(r"C:\ProgramData\OpenAI\Codex\config.toml"),
            Path(r"C:\ProgramData\OpenAI\Codex\requirements.toml"), Path.home() / ".codex/managed_config.toml"):
        try:
            path.stat()
        except FileNotFoundError:
            continue
        raise RuntimeError("MANAGED_SOURCE_REQUIRES_REVIEW")
    for parent in (run / "repo", *(run / "repo").parents):
        names = ["AGENTS.md", "AGENTS.override.md"] + ([] if parent == Path.home() else [".codex/config.toml"])
        if any((parent / name).exists() for name in names):
            raise RuntimeError("ANCESTOR_INSTRUCTIONS_REQUIRE_REVIEW")
    APP["version"]("codex", APP["codex_env"](run))
    result = subprocess.run([APP["CODEX"], "login", "status"], cwd=run / "repo", env=APP["codex_env"](run),
                            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or "chatgpt" not in (result.stdout + result.stderr).lower():
        raise RuntimeError("PINNED_CODEX_OR_CHATGPT_LOGIN_NOT_VERIFIED")
    APP["git"](run, "rev-parse", "--verify", "HEAD")
    json.loads((run / "control/developer_schema.json").read_text())
    if APP["inventory"](run / "repo") != json.loads((run / "control/baseline.json").read_text())["inventory"]:
        raise RuntimeError("FRESH_TOY_BASELINE_CHANGED")


def run_diagnostic(run, approved=False):
    if (run / "result.json").exists() or (run / "artifacts/exec_diagnostic_attempt.json").exists():
        raise RuntimeError("USED_RUN_NO_LIVE_RETRY")
    report = {"scope": "LIVE_CODEX_EXEC_DIAGNOSTIC", "verdict": "BLOCKED", "codex_sessions": 0, "claude_sessions": 0,
        "startup_logging": "TARGETED_MODULE_FILTERS_RAW_LOGS_NOT_RETAINED"}
    folder = run / "artifacts" / ("exec_diagnostic_" + uuid.uuid4().hex[:8])
    folder.mkdir()
    try:
        codex_only_preflight(run)
        before = {name: APP["inventory"](run / name) for name in ("repo", "control", "review_snapshot", ".claude")}
        mcp_before = (run / ".mcp.json").read_bytes()
        def unchanged():
            if any(APP["inventory"](run / name) != value for name, value in before.items()):
                raise RuntimeError("DIAGNOSTIC_FILE_MODIFICATION_FORBIDDEN")
            if (run / ".mcp.json").read_bytes() != mcp_before or any(
                    (run / "artifacts" / (name + ".marker")).exists() for name in ("hook", "mcp")):
                raise RuntimeError("DIAGNOSTIC_CONFIGURATION_OR_CANARY_CHANGED")
        plan = json.loads((run / "artifacts/exec_diagnostic_plan.json").read_text())
        argv = plan_argv(run, plan)
        if plan["argv"] != argv or plan["source_hashes"] != {name: hashlib.sha256((BASE / name).read_bytes()).hexdigest()
                for name in ("orchestrator.py", "process.py", "diagnostics.py", "codex_exec_diagnostic.py")}:
            raise RuntimeError("DIAGNOSTIC_PLAN_CHANGED_REVIEW_REQUIRED")
        # Compile the entire diagnostic config through the native MXC command,
        # rather than the smaller config used by the older echo control.
        native = argv[:argv.index("exec")] + ["sandbox", "--include-managed-config", "--permission-profile", "toydev",
            "-C", str(run / "repo"), "--", r"C:\Windows\System32\cmd.exe", "/d", "/c", "echo", "DANI_MXC_OK"]
        control = APP["PROCESS"]["run"]({"argv": native, "cwd": str(run / "repo"), "env": diagnostic_env(run),
            "kind": "codex-local-check"}, folder, timeout=15, watch=unchanged)
        report["full_config_mxc_control"] = control
        unchanged()
        if not HOST["pass_mxc"](control):
            raise RuntimeError("FULL_EXEC_CONFIG_MXC_CONTROL_FAILED_NO_MODEL_STARTED")
        image = runtime_image_probe(run, folder, native, unchanged)
        report['runtime_image_probe'] = image
        unchanged()
        if (not all(image['checks'][name].get('read') is True for name in
                ('codex_image', 'workspace_source', 'test_source', 'output_schema'))
                or image['checks']['codex_version'] != {'process_started': True, 'exit_code': 0, 'expected_version': True}):
            raise RuntimeError('MXC_RUNTIME_IMAGE_OR_FIXTURE_ACCESS_FAILED_NO_MODEL_STARTED')
        if not approved:
            report.update(verdict="APPROVAL_REQUIRED", reason="MODEL_FREE_CHECKS_PASSED_LIVE_DIAGNOSTIC_NOT_APPROVED")
            APP["save"](folder / "report.json", report)
            print(json.dumps(report, indent=2))
            print("Separate approval required for one 60-second Codex exec diagnostic. Plan: " + str(run / "artifacts/exec_diagnostic_plan.json"))
            return 2
        APP["save"](run / "artifacts/exec_diagnostic_attempt.json", {"approved": True, "codex_sessions": 1,
            "claude_sessions": 0, "timeout_seconds": 60, "retries": 0})
        report["codex_sessions"] = 1
        result = APP["PROCESS"]["run"]({"argv": argv, "cwd": str(run / "repo"), "env": diagnostic_env(run),
            "kind": "codex", "forbid_tools": True}, folder, timeout=60, watch=unchanged)
        report["codex"] = result
        unchanged()
        finals = [event["text"] for event in result["events"] if event["type"] == "final"]
        if result.get("error") or result["exit_code"] != 0 or not any(
                event["type"] == "turn.completed" for event in result["events"]):
            if ('ACCESS_DENIED' in result.get('diagnostic_codes', []) and result.get('cli_process_started')
                    and not result.get('protocol_milestones')):
                raise RuntimeError('CODEX_SESSION_INITIALIZATION_ACCESS_DENIED_WINERROR_5')
            raise RuntimeError("CODEX_EXEC_DIAGNOSTIC_FAILED")
        if len(finals) != 1 or json.loads(finals[0]) != {"status": "blocked", "changed_files": [], "summary": "DANI_EXEC_OK"}:
            raise RuntimeError("DIAGNOSTIC_OBJECTIVE_NOT_MET")
        report["verdict"] = "PASS"
        report["reason"] = "CODEX_EXEC_RESPONDED_WITHOUT_TOOLS_OR_FILE_CHANGES"
    except Exception as exc:
        report["verdict"] = "FAIL" if report["codex_sessions"] else "BLOCKED"
        report["reason"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        if getattr(exc, "evidence", None):
            report["failure_evidence"] = exc.evidence
    APP["save"](folder / "report.json", report)
    if report["codex_sessions"]:
        APP["save"](run / "result.json", report)
        (run / "REPORT.md").write_text("Codex exec diagnostic: " + report["verdict"] + "\n\n" + report["reason"] +
            "\n\nSanitized evidence: " + str(folder / "report.json") + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--live-approved", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_diagnostic(APP["approved_run"](args.run), approved=args.live_approved))
