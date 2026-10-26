"""Offline test fixtures that do not depend on any one PC's earlier live runs.

The tests used to read historic run folders from the original installation. A fresh clone has none,
so these build equivalent disposable runs locally (no model calls). The synthetic reviewer profile is
returned only through unittest patches; production code never selects it (coordinator.reviewer_source).
"""
import hashlib
import json
from pathlib import Path
import runpy
import stat
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
APP = runpy.run_path(str(BASE / "orchestrator.py"), run_name="fixture_runs")
_cache = {}


def toy_run():
    """A freshly prepared toy run (repository, controls, planned commands)."""
    if "toy" not in _cache:
        _cache["toy"] = APP["prepare"](BASE / "task.json")
    return _cache["toy"]


def frozen_run():
    """A toy run whose child environment is frozen, as every run copied from a reviewer profile is."""
    if "frozen" not in _cache:
        run = APP["prepare"](BASE / "task.json")
        APP["save"](run / "artifacts/verified_startup_environment.json", APP["OLD_GATE"]["child_env"]())
        _cache["frozen"] = run
    return _cache["frozen"]


def reviewer_source():
    """A synthetic stand-in for this installation's verified reviewer startup profile."""
    if "source" in _cache:
        return _cache["source"]
    if not APP["CLAUDE"] or not Path(APP["CLAUDE"]).is_file():
        raise RuntimeError("CONFIRM_CLAUDE_CODE_ON_THE_CONNECT_PAGE_BEFORE_THE_SELF_TEST")
    run = APP["prepare"](BASE / "task.json")
    APP["save"](run / "artifacts/verified_startup_environment.json", APP["OLD_GATE"]["child_env"]())
    (run / ".claude").mkdir()
    canary = run / "control/startup_canary.py"
    canary.write_text("raise SystemExit('SYNTHETIC FIXTURE CANARY')\n", encoding="utf-8")
    APP["save"](run / ".claude/settings.json", {"disableAllHooks": False, "hooks": {}})
    APP["save"](run / ".mcp.json", {"mcpServers": {"synthetic_denied_startup_canary": {
        "command": APP["PYTHON"], "args": ["-I", "-B", str(canary), "mcp"]}}})
    probe = run / "review_snapshot/startup_probe.txt"
    probe.write_text("SYNTHETIC READ-ONLY STARTUP PROBE\n", encoding="utf-8")
    probe.chmod(stat.S_IREAD)
    criteria = ("zero_tools", "zero_mcp", "no_hook_execution", "no_unexpected_file_changes", "process_containment")
    APP["save"](run / "artifacts/reviewer_startup_gate.json", {"verdict": "PASS",
        "criteria": dict.fromkeys(criteria, True), "initialization": {"tools": [], "mcp_servers_count": 0},
        "argv": APP["commands"](run)["claude"], "cwd": str(run / "review_snapshot"),
        "binary_sha256": hashlib.sha256(Path(APP["CLAUDE"]).read_bytes()).hexdigest(),
        "control_inventory": APP["inventory"](run / "control"),
        "canary_configuration": {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                                 for name in (".claude/settings.json", ".mcp.json")},
        "child_environment_sha256": hashlib.sha256(json.dumps(APP["env"](run), sort_keys=True).encode()).hexdigest(),
        "source": "SYNTHETIC_OFFLINE_TEST_FIXTURE_NEVER_A_LIVE_PROFILE"})
    APP["verified_startup_gate"](run)
    _cache["source"] = run
    return run


def patched():
    """Patches for test modules that build runs or validate model settings (start in setUpModule)."""
    from dani_agent import model_settings as M
    models = {role: {M.DEFAULTS[role + "_model"]} for role in ("codex", "claude")}
    return [patch("dani_agent.coordinator.reviewer_source", reviewer_source),
            patch.object(M, "verified_models", lambda role: models[role])]


def start(patches):
    reviewer_source()   # build it now, never lazily inside a test that mocks subprocess
    for item in patches:
        item.start()


def stop(patches):
    for item in patches:
        item.stop()
