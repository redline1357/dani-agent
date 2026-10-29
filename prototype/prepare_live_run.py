"""Prepare a fresh disposable run with an equivalent, previously verified reviewer profile."""
import hashlib
import json
from pathlib import Path
import runpy
import stat

BASE = Path(__file__).resolve().parent


def prepare(source, *, template="clamp-v1", parent=None):
    app = runpy.run_path(str(BASE / "orchestrator.py"), run_name="successor_preparation")
    source = app["approved_run"](source)
    gate = app["verified_startup_gate"](source)
    if app["inventory"](source / "repo") != json.loads((source / "control/baseline.json").read_text())["inventory"]:
        raise RuntimeError("SOURCE_TOY_REPOSITORY_CHANGED_STOP")
    # Live runs go to the private work folder, never under this source tree: the preflight refuses any
    # AGENTS.md above a run, and the repository root has one (2026-10-29: every source review was blocked).
    run = app["prepare"](BASE / "task.json", template=template, parent=app["WORK_RUNS"] if parent is None else parent)
    def relocate(value):
        if isinstance(value, str):
            return value.replace(str(source), str(run))
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, dict):
            return {relocate(key): relocate(item) for key, item in value.items()}
        return value
    if app["commands"](run)["claude"] != relocate(gate["argv"]):
        raise RuntimeError("REVIEWER_PROFILE_CHANGED_STOP")
    controls = ["reviewer_settings.json", "empty_mcp.json", "review_schema.json", "developer_schema.json"]
    if template == "clamp-v1":
        controls.append("task.json")
    for name in controls:
        if (run / "control" / name).read_bytes() != (source / "control" / name).read_bytes():
            raise RuntimeError("REVIEWER_CONTROL_CHANGED_STOP")
    (run / "control/startup_canary.py").write_bytes((source / "control/startup_canary.py").read_bytes())
    (run / ".claude").mkdir()
    for name in (".claude/settings.json", ".mcp.json"):
        app["save"](run / name, relocate(json.loads((source / name).read_text())))
    snapshot = run / "review_snapshot/startup_probe.txt"
    snapshot.write_bytes((source / "review_snapshot/startup_probe.txt").read_bytes())
    snapshot.chmod(stat.S_IREAD)
    app["save"](run / "artifacts/verified_startup_environment.json",
                relocate(json.loads((source / "artifacts/verified_startup_environment.json").read_text())))
    new_gate = {**gate, "argv": app["commands"](run)["claude"], "cwd": str(run / "review_snapshot"),
        "control_inventory": app["inventory"](run / "control"),
        "canary_configuration": {name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in gate["canary_configuration"]},
        "child_environment_sha256": hashlib.sha256(json.dumps(app["env"](run), sort_keys=True).encode()).hexdigest(),
        "source": "Equivalent profile transfer, not a new runtime diagnostic",
        "source_run": str(source),
        "source_gate_sha256": hashlib.sha256((source / "artifacts/reviewer_startup_gate.json").read_bytes()).hexdigest(),
        "verification_mode": "UNCHANGED_PROFILE_WITH_DISPOSABLE_ROOT_PATH_RELOCATION_ONLY" if template == "clamp-v1" else
            "UNCHANGED_EXECUTABLE_REVIEWER_PROFILE_WITH_TRUSTED_FIXTURE_DATA"}
    app["save"](run / "artifacts/reviewer_startup_gate.json", new_gate)
    app["verified_startup_gate"](run)
    return run


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    print(prepare(parser.parse_args().source))
