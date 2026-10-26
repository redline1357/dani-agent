"""Disposable, standard-library-only Codex -> sandboxed tests -> Claude prototype."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import uuid

BASE = Path(__file__).resolve().parent
PROJECT = BASE.parent
# Program paths and versions the user confirmed on the dashboard's Connect page (data/setup.json).
# Nothing is guessed at run time: an unconfirmed CLI has an empty path and every live check stops.
SETUP_FILE = PROJECT / "data/setup.json"


def load_setup():
    try:
        value = json.loads(SETUP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if type(value) is dict and type(value.get("tools", {})) is dict else {}


SETUP = load_setup()
TOOLS = {name: entry for name, entry in SETUP.get("tools", {}).items()
         if type(entry) is dict and type(entry.get("path")) is str and type(entry.get("version")) is str}
PYTHON = TOOLS.get("python", {}).get("path") or str(Path(sys.base_prefix) / "python.exe")
GIT = TOOLS.get("git", {}).get("path") or r"C:\Program Files\Git\cmd\git.exe"
CODEX = TOOLS.get("codex", {}).get("path", "")
CLAUDE = TOOLS.get("claude", {}).get("path", "")
# Only for the fixed toy fixture; product and project runs use the user's probe-verified model.
TOY_CODEX_MODEL = "gpt-6-astra"
# Real-project workspaces live outside the (sandbox-denied) user profile: under a denied ancestor,
# sandboxed PowerShell cannot enter the workspace and Codex's shell tool is unusable. The Connect
# page creates the folder with an ACL limited to the current user and SYSTEM.
# The folder is chosen on the Connect page (data/setup.json "work_root"); C:\DaniAgentWork until then.
# Quick helper commands get a generous hang guard only; nothing on the product path stops for time (2026-10-23).
HELPER_SECONDS = 300
DEFAULT_WORK_ROOT = r"C:\DaniAgentWork"
WORK_ROOT = SETUP.get("work_root") if type(SETUP.get("work_root")) is str and Path(SETUP["work_root"]).is_absolute() else DEFAULT_WORK_ROOT
WORK_RUNS = Path(WORK_ROOT) / "runs"
# Python must be installed outside the user profile, which the sandbox denies.
PYTHON_HOME = str(Path(PYTHON).parent)
# PATH for Codex's sandboxed shell tool; Codex runs commands through powershell.exe on Windows.
SHELL_PATH = PYTHON_HOME + r";C:\Windows\System32;" + str(Path(GIT).parent) + r";C:\Windows\System32\WindowsPowerShell\v1.0"
PROCESS = runpy.run_path(str(BASE / "process.py"), run_name="prototype_process")
OLD_GATE = runpy.run_path(str(PROJECT / "phase0_security/final/claude_gate_check.py"), run_name="prototype_settings")
OBJECTIVE = "Implement integer clamp(value, lower, upper) in toy_math.py; raise ValueError if lower > upper. Change only toy_math.py."
STUB = "def clamp(value, lower, upper):\n    raise NotImplementedError\n"
TESTS = '''import unittest
from toy_math import clamp
class ClampTests(unittest.TestCase):
    def test_below(self): self.assertEqual(clamp(-1, 0, 10), 0)
    def test_lower(self): self.assertEqual(clamp(0, 0, 10), 0)
    def test_inside(self): self.assertEqual(clamp(4, 0, 10), 4)
    def test_upper(self): self.assertEqual(clamp(10, 0, 10), 10)
    def test_above(self): self.assertEqual(clamp(11, 0, 10), 10)
    def test_equal(self): self.assertEqual(clamp(1, 3, 3), 3)
    def test_negative(self): self.assertEqual(clamp(-5, -10, -2), -5)
    def test_reversed(self):
        with self.assertRaises(ValueError): clamp(5, 10, 0)
'''
DEVELOPER_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": ["implemented", "blocked"]},
                   "changed_files": {"type": "array", "items": {"type": "string"}}, "summary": {"type": "string"}},
    "required": ["status", "changed_files", "summary"]}
REVIEW_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"candidate_sha": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "changes_requested", "blocked"]},
        "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"severity": {"type": "string", "enum": ["low", "medium", "high"]}, "file": {"type": "string"},
                           "line": {"type": ["integer", "null"]}, "explanation": {"type": "string"}},
            "required": ["severity", "file", "line", "explanation"]}}},
    "required": ["candidate_sha", "verdict", "findings"]}


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2)


def validate(value, schema):
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types
    match = {"object": type(value) is dict, "array": type(value) is list, "string": type(value) is str,
             "integer": type(value) is int, "null": value is None}
    if types and not any(match.get(t, False) for t in types):
        raise ValueError("SCHEMA_TYPE")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("SCHEMA_ENUM")
    if type(value) is dict:
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys() or schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError("SCHEMA_KEYS")
        for key in value:
            if key in properties:
                validate(value[key], properties[key])
    if type(value) is list:
        for item in value:
            validate(item, schema["items"])
    return value


def inventory(directory):
    out = {}
    def walk(folder):
        for path in folder.iterdir():
            info = path.lstat()
            if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or info.st_nlink > 1:
                raise RuntimeError("LINK_OR_REPARSE_POINT")
            if path.is_dir():
                walk(path)
            else:
                out[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    walk(directory)
    return out


def check_scope(repo, before):
    after = inventory(repo)
    changes = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if changes - {"toy_math.py"}:
        raise RuntimeError("PROTECTED_OR_UNEXPECTED_PATH_CHANGED")
    if not (repo / "toy_math.py").is_file():
        raise RuntimeError("CANDIDATE_MISSING")
    return sorted(changes)


def env(run):
    environment = OLD_GATE["child_env"]()
    frozen = run / "artifacts/verified_startup_environment.json"
    if frozen.exists():
        environment = json.loads(frozen.read_text())
        allowed = {"systemroot", "windir", "comspec", "pathext", "path", "userprofile", "appdata", "localappdata",
            "temp", "tmp", "username", "userdomain", "homedrive", "homepath", "processor_architecture", "os",
            "programfiles", "programfiles(x86)", "programw6432", "disable_autoupdater", "disable_updates",
            "claude_code_disable_nonessential_traffic", "enable_claudeai_mcp_servers", "git_config_nosystem",
            "git_config_global", "git_config_system", "git_config_count", "git_terminal_prompt"}
        if (type(environment) is not dict or any(k.lower() not in allowed or type(v) is not str
                                                for k, v in environment.items())):
            raise RuntimeError("UNEXPECTED_FROZEN_ENVIRONMENT_STOP")
    else:
        # MXC refuses to start without a nonempty host SystemDrive; child_env() omits it.
        environment["SystemDrive"] = os.environ.get("SystemDrive") or Path(os.environ.get("SystemRoot", r"C:\Windows")).drive
    empty = run / "control/empty_git"
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(empty / "absent"),
                       GIT_CONFIG_SYSTEM=str(empty / "absent"), GIT_CONFIG_COUNT="0", GIT_TERMINAL_PROMPT="0")
    return environment


def git(run, *args):
    command = [GIT, "-c", "core.hooksPath=" + str(run / "control/empty_git"), "-c", "core.fsmonitor=false",
               "-c", "commit.gpgsign=false", "-c", "core.attributesFile=" + str(run / "control/empty_git/absent"),
               "-c", "user.name=Dani Prototype", "-c", "user.email=dani-prototype@example.invalid"] + list(args)
    proc = subprocess.run(command, cwd=run / "repo", env=env(run), capture_output=True, text=True, encoding='utf-8', timeout=HELPER_SECONDS,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    if proc.returncode:
        raise RuntimeError("SAFE_GIT_COMMAND_FAILED")
    return proc.stdout.strip()


def codex_env(run):
    # Preserve the credential-free allowlist, but do not replay AppContainer
    # runtime directories into a normal host process. Claude's profile is unchanged.
    environment = env(run)
    names = ("SystemRoot", "WINDIR", "SystemDrive", "LOCALAPPDATA", "TEMP", "TMP")
    current = {key.casefold(): value for key, value in os.environ.items() if key.casefold() in {name.casefold() for name in names}}
    current.setdefault("systemdrive", Path(current.get("systemroot", r"C:\Windows")).drive)
    for name in names:
        value = current.get(name.casefold())
        if not value:
            raise RuntimeError("REQUIRED_WINDOWS_RUNTIME_VARIABLE_MISSING_" + name.upper())
        for existing in list(environment):
            if existing.casefold() == name.casefold():
                del environment[existing]
        environment[name] = value
    # Never hand Codex another program's private runtimes. The Codex desktop app puts its bundled PowerShell
    # (~/.cache/codex-runtimes/.../powershell) on PATH; Codex then wraps every shell command in it, the sandbox
    # denies the user profile, and each command fails ("Failed to resolve full path of the current executable").
    # Nothing under the user profile is usable inside the sandbox, so PATH keeps system locations only.
    home = str(Path(os.environ.get("USERPROFILE") or Path.home())).rstrip("\\").casefold()
    for key in [key for key in environment if key.casefold() == "path"]:
        environment[key] = ";".join(entry for entry in environment[key].split(";") if entry.strip()
                                    and entry.strip().rstrip("\\").casefold() != home
                                    and not entry.strip().casefold().startswith(home + "\\"))
    return environment


def config_value(value):
    """Codex -c consumes TOML, not JSON objects or quoted dotted key paths."""
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(key) + " = " + config_value(item)
                                 for key, item in value.items()) + "}"
    return json.dumps(value)


def overrides(values):
    args = []
    for key, value in values.items():
        args += ["-c", key + "=" + config_value(value)]
    return args


def permissions(run, name, writable):
    filesystem = {":minimal": "read", str(Path.home()): "deny", str(run): "read",
        str(run / "repo"): "write" if writable else "read", str(run / "repo/.git"): "read",
        str(run / "repo/test_toy_math.py"): "read", PYTHON_HOME: "read"}
    for path in (".claude", ".codex", ".mcp.json", "AGENTS.md", "AGENTS.override.md", "CLAUDE.md", ".agents"):
        filesystem[str(run / "repo" / path)] = "deny"
    return {"windows.sandbox": "mxc", "permissions." + name + ".filesystem": filesystem,
            "permissions." + name + ".network.enabled": False}


def reviewer_flags(run):
    return ["--safe-mode", "--restricted", "--tools", "", "--disallowedTools", "*", "--permission-mode", "dontAsk",
        "--permission-prompts", "none", "--setting-sources", "", "--settings", str(run / "control/reviewer_settings.json"),
        "--mcp-config", str(run / "control/empty_mcp.json"), "--strict-mcp-config", "--disable-slash-commands", "--no-chrome",
        "--model", "sonnet", "--effort", "low", "--max-turns", "1", "--no-session-persistence",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--print"]


def commands(run, *, exec_diagnostic=False, model=TOY_CODEX_MODEL):
    code = {**permissions(run, "toydev", True), "default_permissions": "toydev", "approval_policy": "never",
        "forced_login_method": "chatgpt", "model_provider": "openai", "model_reasoning_effort": "medium",
        "features.hooks": False, "features.plugins": False, "features.remote_plugin": False,
        "features.multi_agent": False, "features.skill_mcp_dependency_install": False,
        "allow_login_shell": False, "web_search": "disabled", "shell_environment_policy.inherit": "none",
        # Codex 0.163 discovers AGENTS.md through the sandboxed filesystem; the protected
        # repo/AGENTS.md deny rule makes that probe fail with os error 5 at thread start.
        # 0 skips discovery entirely (verified with `codex debug prompt-input`), so the deny stays.
        "project_doc_max_bytes": 0,
        "shell_environment_policy.set": {"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows",
            "SystemDrive": r"C:",
            "PATH": SHELL_PATH}}
    prompt = (run / "control/developer_prompt.txt").read_text()
    if exec_diagnostic:
        # Same exec builder; deliberately tighten only repository writes and
        # lower effort for the separately approved startup diagnostic.
        code.update(permissions(run, "toydev", False))
        code["model_reasoning_effort"] = "low"
        prompt = ('Startup diagnostic only. Do not use any tools, run commands, read files, or modify files. '
            'Do not implement the repository task. Return ONLY this JSON, then stop: '
            '{"status":"blocked","changed_files":[],"summary":"DANI_EXEC_OK"}')
    return {"codex": [CODEX] + overrides(code) + ["exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
        "--model", model, "--ephemeral", "--json", "--output-schema", str(run / "control/developer_schema.json"),
        "-C", str(run / "repo"), prompt],
        "tests": [CODEX] + overrides(permissions(run, "toytest", False)) + ["sandbox", "--include-managed-config",
            "--permission-profile", "toytest", "-C", str(run / "repo"), "--", PYTHON, "-I", "-B", "-m", "unittest",
            "discover", "-s", str(run / "repo"), "-p", "test_toy_math.py"],
        "claude": [CLAUDE] + reviewer_flags(run)}


def prepare(task_path=None, *, template="clamp-v1", parent=None):
    objective = OBJECTIVE
    if task_path:
        task_path = Path(task_path).resolve()
        if not task_path.is_relative_to(BASE):
            raise ValueError("TASK_FILE_MUST_BE_INSIDE_PROTOTYPE")
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if set(task) != {"task_id", "objective"} or task["task_id"] != "clamp-v1" or type(task["objective"]) is not str or not 1 <= len(task["objective"]) <= 2000:
            raise ValueError("ONLY_CLAMP_V1_TASK_SUPPORTED")
        objective = task["objective"]
    source, fixed_tests, developer_prompt = STUB, TESTS, objective
    fixture = None
    if template == "clamp-review-gap-v1":
        fixture = runpy.run_path(str(PROJECT / "dani_agent/correction_fixture.py"), run_name="trusted_correction_fixture")
        source, fixed_tests, objective = fixture["SOURCE"], fixture["WEAK_TESTS"], fixture["OBJECTIVE"]
        developer_prompt = fixture["INITIAL_PROMPT"]
    elif template != "clamp-v1":
        raise ValueError("UNKNOWN_DISPOSABLE_TEMPLATE")
    if parent is None:
        parent = BASE / "runs"
        parent.mkdir(exist_ok=True)
    elif parent != WORK_RUNS:
        raise ValueError("UNAPPROVED_RUN_LOCATION")
    elif not parent.is_dir():
        # Never re-create it implicitly: a new folder would inherit the broader C:\ permissions.
        raise RuntimeError("PRIVATE_WORK_ROOT_MISSING_C_DANIAGENTWORK")
    run = parent / (datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8])
    run.mkdir()
    for part in ("repo", "control", "control/empty_git", "artifacts", "review_snapshot"):
        (run / part).mkdir()
    (run / "repo/toy_math.py").write_text(source, encoding="utf-8")
    (run / "repo/test_toy_math.py").write_text(fixed_tests, encoding="utf-8")
    (run / "control/developer_prompt.txt").write_text(developer_prompt + " Do not edit tests, configuration or Git metadata. "
        "Do not install anything, commit, use MCP/network or request escalation. Python will test and commit your changes. "
        "Return JSON matching the output schema.", encoding="utf-8")
    task_data = {"objective": objective, "allowed_change": "toy_math.py", "max_sessions_per_agent": 1,
        "timeout_seconds_per_agent": 120, "retry_count": 0}
    if fixture:
        task_data.update(template=template, max_sessions_per_agent=2, max_correction_cycles=1)
        (run / "control/acceptance_test.py").write_text(fixture["ACCEPTANCE_TEST"], encoding="utf-8")
    save(run / "control/task.json", task_data)
    save(run / "control/developer_schema.json", DEVELOPER_SCHEMA)
    save(run / "control/review_schema.json", REVIEW_SCHEMA)
    save(run / "control/reviewer_settings.json", {"disableAllHooks": True, "disableClaudeAiConnectors": True,
                                               "permissions": {"defaultMode": "dontAsk", "deny": ["*"]}})
    save(run / "control/empty_mcp.json", {"mcpServers": {}})
    git(run, "init", "--template=" + str(run / "control/empty_git"))
    git(run, "add", "--", "toy_math.py", "test_toy_math.py")
    git(run, "commit", "-m", "Synthetic clamp baseline")
    save(run / "control/baseline.json", {"sha": git(run, "rev-parse", "HEAD"), "inventory": inventory(run / "repo")})
    save(run / "control/planned_commands.json", commands(run))
    return run


def approved_run(path):
    run = Path(path).resolve()
    if run.parent not in ((BASE / "runs").resolve(), WORK_RUNS.resolve()) or not (run / "control/baseline.json").is_file():
        raise ValueError("NOT_A_DISPOSABLE_PROTOTYPE_RUN")
    return run


def version(name, environment):
    """The confirmed CLI must still report exactly the version the user confirmed; an update needs re-confirming."""
    binary, expected = TOOLS.get(name, {}).get("path"), TOOLS.get(name, {}).get("version")
    if not binary or not expected or not Path(binary).is_file():
        raise RuntimeError("CONNECT_" + name.upper() + "_NOT_CONFIRMED")
    p = subprocess.run([binary, "--version"], env=environment, capture_output=True, text=True, timeout=HELPER_SECONDS,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode or (p.stdout.strip().splitlines() or [""])[0].strip() != expected:
        raise RuntimeError("UNEXPECTED_" + name.upper() + "_VERSION")


def preflight(run):
    bad = [name for name, value in os.environ.items() if value and (name.startswith("ANTHROPIC_") or
        name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN") or name.startswith("CLAUDE_CODE_USE_"))]
    if bad:
        raise RuntimeError("PROVIDER_ENVIRONMENT_OVERRIDE_PRESENT")
    # No managed source is ignored or modified to make this tiny test run.
    for path in (Path(r"C:\ProgramData\OpenAI\Codex\config.toml"), Path(r"C:\ProgramData\OpenAI\Codex\requirements.toml"),
                 Path.home() / ".codex/managed_config.toml", Path(r"C:\Program Files\ClaudeCode\managed-settings.json"),
                 Path(r"C:\Program Files\ClaudeCode\managed-settings.d"), Path(r"C:\Program Files\ClaudeCode\managed-mcp.json"),
                 Path.home() / ".claude/remote-settings.json"):
        try:
            path.stat()
        except FileNotFoundError:
            continue
        raise RuntimeError("MANAGED_SOURCE_REQUIRES_REVIEW")
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Policies\ClaudeCode", 0, winreg.KEY_READ | view) as key:
                    winreg.QueryValueEx(key, "Settings")
            except FileNotFoundError:
                continue
            raise RuntimeError("MANAGED_REGISTRY_SOURCE_REQUIRES_REVIEW")
    for parent in (run / "repo", *(run / "repo").parents):
        names = ["AGENTS.md", "AGENTS.override.md"] + ([] if parent == Path.home() else [".codex/config.toml"])
        if any((parent / name).exists() for name in names):
            raise RuntimeError("ANCESTOR_INSTRUCTIONS_REQUIRE_REVIEW")
    for name in ("codex", "claude"):
        version(name, env(run))
    p = subprocess.run([CLAUDE] + reviewer_flags(run)[:reviewer_flags(run).index("--model")] + ["auth", "status", "--json"],
                       cwd=run / "review_snapshot", env=env(run), capture_output=True, text=True, timeout=HELPER_SECONDS,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        auth = json.loads(p.stdout)
    except ValueError:
        auth = {}
    # A claude.ai subscription login (any plan), never Console/API-key billing.
    if (p.returncode or auth.get("authMethod") != "claude.ai" or type(auth.get("subscriptionType")) is not str
            or not auth["subscriptionType"] or auth.get("loggedIn") is not True):
        raise RuntimeError("SUBSCRIPTION_AUTH_NOT_CONFIRMED")
    p = subprocess.run([CODEX, "login", "status"], env=env(run), capture_output=True, text=True, timeout=HELPER_SECONDS,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode or "chatgpt" not in (p.stdout + p.stderr).lower():
        raise RuntimeError("CHATGPT_LOGIN_NOT_CONFIRMED")


def inventory_probe(run, runner=None):
    if runner is None and (run / "artifacts/reviewer_startup_gate.json").exists():
        return verified_startup_gate(run)
    result = (runner or PROCESS["run"])({"argv": commands(run)["claude"], "cwd": str(run / "review_snapshot"),
        "env": env(run), "kind": "claude-inventory"}, run / "artifacts", timeout=None, idle=HELPER_SECONDS)
    # Preserve every startup probe, including a prior manually requested probe.
    evidence = run / "artifacts/reviewer_inventory.json"
    if evidence.exists():
        evidence = run / "artifacts" / ("reviewer_inventory_" + uuid.uuid4().hex + ".json")
    save(evidence, result)
    if result.get("error") or result.get("initialization") != {"tools": [], "mcp_servers_count": 0} or result.get("prompt_sent"):
        raise RuntimeError("REVIEWER_INVENTORY_NOT_VERIFIED_NO_MODELS_STARTED")
    return result


def verified_startup_gate(run):
    gate = json.loads((run / "artifacts/reviewer_startup_gate.json").read_text())
    expected = {"zero_tools", "zero_mcp", "no_hook_execution", "no_unexpected_file_changes", "process_containment"}
    if (gate.get("verdict") != "PASS" or set(gate.get("criteria", {})) != expected
            or not all(value is True for value in gate["criteria"].values())
            or gate.get("initialization") != {"tools": [], "mcp_servers_count": 0}
            or gate.get("argv") != commands(run)["claude"]
            or gate.get("cwd") != str(run / "review_snapshot")
            or gate.get("binary_sha256") != hashlib.sha256(Path(CLAUDE).read_bytes()).hexdigest()
            or gate.get("control_inventory") != inventory(run / "control")
            or gate.get("child_environment_sha256") != hashlib.sha256(json.dumps(env(run), sort_keys=True).encode()).hexdigest()):
        raise RuntimeError("VERIFIED_REVIEWER_STARTUP_PROFILE_CHANGED_STOP")
    for name, digest in gate.get("canary_configuration", {}).items():
        if hashlib.sha256((run / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("STARTUP_CANARY_CONFIGURATION_CHANGED_STOP")
    return gate


def verified_mxc_host_gate(run):
    context = PROCESS["DIAGNOSTICS"]["execution_context"]()
    if context.get("appcontainer") is not False or context.get("elevated") is not False:
        raise RuntimeError("NORMAL_NON_ELEVATED_HOST_REQUIRED_NO_MODEL_STARTED")
    path = run / "artifacts/mxc_host_gate.json"
    if not path.is_file():
        raise RuntimeError("MODEL_FREE_MXC_HOST_PREFLIGHT_REQUIRED")
    gate = json.loads(path.read_text())
    if (gate.get("verdict") != "PASS" or gate.get("context") != context
            or gate.get("preflight_kind") != "EXEC_CLI_MODEL_FREE_V1"
            or gate.get("exec_arguments_pass") is not True
            or gate.get("toydev_mxc_pass") is not True
            or gate.get("toytest_mxc_pass") is not True
            or gate.get("session_initialization") != "UNVERIFIED_REQUIRES_SEPARATELY_APPROVED_CODEX_EXEC"
            or gate.get("argv") != commands(run)["codex"]
            or gate.get("environment_sha256") != hashlib.sha256(json.dumps(codex_env(run), sort_keys=True).encode()).hexdigest()
            or gate.get("binary_sha256") != hashlib.sha256(Path(CODEX).read_bytes()).hexdigest()
            or gate.get("orchestrator_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or gate.get("process_sha256") != hashlib.sha256((BASE / "process.py").read_bytes()).hexdigest()):
        raise RuntimeError("MXC_HOST_PREFLIGHT_CHANGED_STOP")
    sources = ("diagnostics.py", "host_launcher.py", "second_failure_f3fb4430/session_probe.py")
    if gate.get("extra_sources") != {name: hashlib.sha256((BASE / name).read_bytes()).hexdigest() for name in sources}:
        raise RuntimeError("MXC_HOST_CHECK_SOURCE_CHANGED_STOP")
    return gate


def pipeline(run, runner=None, simulated=False):
    runner = runner or PROCESS["run"]
    report = {"verdict": "FAIL", "scope": "SIMULATED_PIPELINE" if simulated else "LIVE_INTEGRATION",
              "reason": "NOT_COMPLETED", "agent_sessions": {"codex": 0, "claude": 0}, "tests": None}
    repo = run / "repo"
    before = json.loads((run / "control/baseline.json").read_text())["inventory"]
    control_before = inventory(run / "control")
    try:
        if not simulated:
            verified_mxc_host_gate(run)
        if not simulated and (run / "artifacts/reviewer_startup_gate.json").exists():
            verified_startup_gate(run)
        else:
            inventory_probe(run, runner)
        # Gate ALL quota consumption on a verified, effective reviewer inventory.
        report["agent_sessions"]["codex"] = 1
        save(run / "artifacts/codex_attempt.json", {"started": True})
        output = runner({"argv": commands(run)["codex"], "cwd": str(repo), "env": codex_env(run), "kind": "codex"},
                        run / "artifacts", timeout=120, watch=lambda: check_scope(repo, before))
        save(run / "artifacts/codex_session.json", output)
        check_scope(repo, before)
        if output.get("error") or output["exit_code"] != 0 or not any(e["type"] == "turn.completed" for e in output["events"]):
            codes = output.get("diagnostic_codes", [])
            raise RuntimeError("CODEX_SESSION_FAILED" + (":" + ",".join(codes) if codes else ""))
        finals = [e["text"] for e in output["events"] if e["type"] == "final"]
        developer = validate(json.loads(finals[-1]), DEVELOPER_SCHEMA)
        if developer["status"] != "implemented" or developer["changed_files"] != ["toy_math.py"]:
            raise RuntimeError("DEVELOPER_RESULT_OR_SCOPE_FAILED")
        save(run / "artifacts/developer.json", developer)
        if inventory(run / "control") != control_before:
            raise RuntimeError("CONTROL_CHANGED")
        test_before = inventory(repo)
        tests = runner({"argv": commands(run)["tests"], "cwd": str(repo), "env": codex_env(run), "kind": "plain"},
                       run / "artifacts", timeout=30, watch=lambda: check_scope(repo, before))
        if inventory(repo) != test_before:
            raise RuntimeError("TESTS_MODIFIED_REPOSITORY")
        report["tests"] = {"exit_code": tests["exit_code"], "output": tests["stdout"], "simulated": simulated}
        save(run / "artifacts/tests.json", report["tests"])
        if tests.get("error") or tests["exit_code"] != 0 or "Ran 8 tests" not in tests["stdout"] or "OK" not in tests["stdout"]:
            raise RuntimeError("DETERMINISTIC_TESTS_FAILED")
        diff = git(run, "diff", "--no-ext-diff", "--no-textconv", "--", "toy_math.py")
        git(run, "add", "--", "toy_math.py")
        git(run, "commit", "-m", "Validated synthetic candidate")
        sha = git(run, "rev-parse", "HEAD")
        for name in ("toy_math.py", "test_toy_math.py"):
            target = run / "review_snapshot" / (name + ".txt")
            target.write_text((repo / name).read_text(), encoding="utf-8")
            target.chmod(stat.S_IREAD)
        snapshot_before = inventory(run / "review_snapshot")
        # Handoff contains independently collected source/test/diff evidence, not developer self-praise.
        handoff = {"task": json.loads((run / "control/task.json").read_text())["objective"], "candidate_sha": sha,
                   "candidate": (repo / "toy_math.py").read_text(), "fixed_tests": TESTS,
                   "diff": diff, "test_evidence": report["tests"]}
        save(run / "artifacts/handoff.json", handoff)
        prompt = "Independently review this synthetic candidate. You have no tools. Do not request tools or access files/network. " \
            "Return ONLY one JSON object matching this schema: " + json.dumps(REVIEW_SCHEMA) + "\nEvidence:\n" + json.dumps(handoff)
        report["agent_sessions"]["claude"] = 1
        save(run / "artifacts/claude_attempt.json", {"started": True})
        output = runner({"argv": commands(run)["claude"], "cwd": str(run / "review_snapshot"), "env": env(run),
                         "kind": "claude-review", "prompt": prompt,
                         "startup_inventory_verified": not simulated and verified_startup_gate(run)["verdict"] == "PASS"},
                        run / "artifacts", timeout=120)
        if output.get("error") or output["exit_code"] != 0 or output.get("initialization") != {"tools": [], "mcp_servers_count": 0} or not output.get("prompt_sent"):
            raise RuntimeError("CLAUDE_SESSION_OR_INVENTORY_FAILED")
        completed = [e for e in output["events"] if e["type"] == "result" and e["subtype"] == "success" and not e["is_error"]]
        if len(completed) != 1:
            raise RuntimeError("NO_UNIQUE_REVIEW_COMPLETION")
        review = validate(json.loads(completed[0]["result"]), REVIEW_SCHEMA)
        save(run / "artifacts/reviewer.json", review)
        if inventory(run / "review_snapshot") != snapshot_before or inventory(run / "control") != control_before:
            raise RuntimeError("REVIEW_MUTATED_PROTECTED_INPUT")
        if review["candidate_sha"] != sha or review["verdict"] != "pass" or review["findings"]:
            raise RuntimeError("REVIEW_NOT_PASSED")
        report.update(verdict="PASS", reason="AUTOMATIC_HANDOFF_COMPLETED", candidate_sha=sha)
    except Exception as exc:
        report["reason"] = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        if getattr(exc, "evidence", None):
            save(run / "artifacts/process_failure.json", exc.evidence)
            report["failure_evidence"] = "artifacts/process_failure.json"
    finally:
        save(run / "result.json", report)
        (run / "REPORT.md").write_text("**Dani Agent prototype: " + report["verdict"] + "**\n\nScope: " + report["scope"] +
            "\n\nReason: " + report["reason"] + "\n\nAgent session attempts: " + json.dumps(report["agent_sessions"]) +
            "\n\nTests: " + ("not run" if report["tests"] is None else json.dumps(report["tests"])) +
            "\n\nStructured evidence: artifacts/developer.json, tests.json, handoff.json and reviewer.json when reached.\n" +
            "\nHistorical Claude unknown tool and outside-read cause remain unresolved. This profile allows no tools.\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "preflight", "inventory", "run"])
    parser.add_argument("--run")
    parser.add_argument("--task")
    parser.add_argument("--live-approved", action="store_true")
    args = parser.parse_args()
    if args.mode == "prepare":
        print(prepare(args.task))
        return
    run = approved_run(args.run)
    if args.mode in ("preflight", "inventory", "run"):
        preflight(run)
    if args.mode == "inventory":
        inventory_probe(run)
    if args.mode == "run":
        if not args.live_approved:
            parser.error("Separate explicit human quota approval required")
        if (run / "result.json").exists() or (run / "artifacts/codex_attempt.json").exists():
            raise RuntimeError("THIS_RUN_HAS_ALREADY_BEEN_ATTEMPTED_NO_RETRY")
        result = pipeline(run)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
