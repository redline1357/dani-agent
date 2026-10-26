"""User-started host coordinator. Fixed workflows; no arbitrary queued shell commands."""
import hashlib
import json
from pathlib import Path
import runpy
import threading
import time
import uuid

from .state import POLICIES, Store, approval_scope, needs_approval
from .file_transfer import shared_reader

ROOT = Path(__file__).resolve().parent.parent
PROTO = ROOT / "prototype"
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
RUNS = PROTO / "runs"
# The original installation's verified reviewer profile; new installations create their own.
LEGACY_SOURCE = RUNS / "20261018T133831Z_6e840250"
APP = runpy.run_path(str(PROTO / "orchestrator.py"), run_name="coordinator_prototype")


def reload_app():
    """Re-read data/setup.json after the user confirms programs; every module shares this dict."""
    # update() only replaces values (the keys never change), so a concurrent reader never misses a key.
    APP.update(runpy.run_path(str(PROTO / "orchestrator.py"), run_name="coordinator_prototype"))


def reviewer_source():
    """The verified zero-tool reviewer startup profile that every fresh run copies (reviewer_startup.py)."""
    try:
        name = json.loads((DATA / "reviewer_profile.json").read_text(encoding="utf-8"))["run"]
    except (OSError, ValueError, KeyError, TypeError):
        name = None
    if type(name) is str and name and name.isprintable() and Path(name).name == name and not name.startswith("synthetic"):
        return RUNS / name
    if LEGACY_SOURCE.is_dir():
        return LEGACY_SOURCE
    raise RuntimeError("CONNECT_CLAUDE_REVIEWER_CHECK_REQUIRED")


def fingerprint():
    paths = list((ROOT / "dani_agent").glob("*.py")) + [ROOT / "DaniAgent.py", ROOT / "DaniDashboard.py"]
    paths += [PROTO / name for name in ("orchestrator.py", "process.py", "diagnostics.py", "host_launcher.py",
        "codex_exec_diagnostic.py", "prepare_live_run.py", "run_checks.py", "reviewer_startup.py")]
    paths += list(PROTO.glob('test_phase2*.py')) + [PROTO / 'test_review_fixes.py', PROTO / 'test_products.py', PROTO / 'test_dashboard.py',
                                                   PROTO / 'fixture_runs.py', PROTO / 'test_delivery_guards.py',
                                                   PROTO / 'test_milestone_batches.py']   # shapes what the offline tests prove
    paths += sorted(PROTO.glob('test_attachment*.py'))
    parser_manifest = ROOT / 'vendor/pdf_parser_manifest.json'
    if parser_manifest.is_file():
        paths.append(parser_manifest)   # Approved parser bytes are bound to these verified per-file hashes.
    # Loaded at runtime via runpy: the Job containment API and the child environment source.
    paths += [ROOT / 'phase0_security/final/process_checks.py', ROOT / 'phase0_security/final/claude_gate_check.py']
    values = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return hashlib.sha256(json.dumps({"sources": values, "policies": POLICIES}, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    from .apply_journal import save
    save(path, value)


def read_json(path):
    with shared_reader(path) as stream:
        return json.load(stream)


def prepare_exec_run(model=None):
    """A fresh read-only toy run; its one Codex session doubles as the probe of `model`."""
    from .model_settings import model_name
    model = model_name(model or APP["TOY_CODEX_MODEL"])
    preparer = runpy.run_path(str(PROTO / "prepare_live_run.py"), run_name="coordinator_prepare")
    run = preparer["prepare"](reviewer_source())
    APP["save"](run / "artifacts/exec_diagnostic_plan.json", {
        "approval_status": "SEPARATE_COORDINATOR_APPROVAL_REQUIRED", "run": str(run), "model": model,
        "argv": APP["commands"](run, exec_diagnostic=True, model=model)["codex"], "timeout_seconds": 60,
        "source_hashes": {name: hashlib.sha256((PROTO / name).read_bytes()).hexdigest()
            for name in ("orchestrator.py", "process.py", "diagnostics.py", "codex_exec_diagnostic.py")}})
    return run


def import_evidence(store):
    """Only user-approved real attempts; exclude mocked test-run results."""
    count = 0
    for path in [*(PROTO / "runs").glob("*/result.json"), *APP["WORK_RUNS"].glob("*/result.json")]:
        run = path.parent
        approved = any((run / "artifacts" / name).is_file() for name in (
            "project_owner_live_approval.json", "exec_diagnostic_approval.json", "local_live_approval.json"))
        if not approved:
            continue
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
            if value.get("scope") not in ("LIVE_INTEGRATION", "LIVE_CODEX_EXEC_DIAGNOSTIC", "LIVE_COLLABORATION", "LIVE_CORRECTION_VALIDATION", "LIVE_PROJECT_DEVELOPMENT", "LIVE_MILESTONE_REVIEW"):
                continue
            counts = value.get("agent_sessions", {"codex": value.get("codex_sessions", 0), "claude": value.get("claude_sessions", 0)})
            summary = {"run": run.name, "verdict": value["verdict"], "reason": value.get("reason"),
                "scope": value["scope"], "agent_sessions": counts, "result_path": str(path),
                "subscription_quota_used": None, "token_usage": value.get("usage")}
            # Older timeout evidence lives in containment files, not report output.
            files = sorted((run / "artifacts").rglob("*.containment.json"))
            timeouts = [json.loads(item.read_text()) for item in files]
            summary["timeouts"] = [item["elapsed_seconds"] for item in timeouts if item.get("termination_reason") == "TIMEOUT"]
            store.import_evidence(path, hashlib.sha256(raw).hexdigest(), summary)
            count += 1
        except (OSError, ValueError, KeyError):
            continue
    return count


def integration_ready(store):
    if any(task["kind"] == "codex_diagnostic" and task["state"] == "completed"
            and (task["result"] or {}).get("verdict") == "PASS" for task in store.tasks()):
        return True
    return any(item.get("scope") == "LIVE_CODEX_EXEC_DIAGNOSTIC" and item.get("verdict") == "PASS" for item in store.evidence())


def queue_diagnostic(store, result):
    # A newer source-bound preflight supersedes an unapproved diagnostic. Keep
    # its evidence, but do not offer an obsolete executable plan for approval.
    for task in store.tasks():
        if (result.get("run") and task["kind"] == "codex_diagnostic" and task["state"] == "awaiting_approval"
                and task["payload"].get("run") != result["run"]):
            store.cancel_pending(task["id"])
    if result.get("run") and not any(task["kind"] == "codex_diagnostic" and task["payload"].get("run") == result["run"]
            for task in store.tasks()):
        payload = {"run": result["run"]}
        if result.get("model"):
            payload["model"] = result["model"]
        store.add("codex_diagnostic", "Read-only Codex diagnostic: separate quota approval required", payload)


def human_decision(store, task_id, accepted, *, independently_tested=False):
    task = store.get(task_id)
    if task["state"] != "awaiting_acceptance" or task["kind"] not in ("collaboration", "correction_validation", "project_development"):
        raise ValueError("TASK_NOT_AWAITING_ACCEPTANCE")
    result = task["result"]
    run = APP["approved_run"](result["run"])
    if (APP["git"](run, "rev-parse", "HEAD") != result["candidate_sha"]
            or APP["inventory"](run / "repo") != result["candidate_inventory"]):
        raise RuntimeError("REVIEWED_REVISION_CHANGED_ACCEPTANCE_BLOCKED")
    store.decide(task_id, result["candidate_sha"], accepted, independently_tested=independently_tested)
    # SQLite is authoritative. Preserve the original result/report, publish the
    # human decision separately rather than rewriting prior evidence.
    task = store.get(task_id)
    atomic_json(DATA / "jobs" / task_id / "human_decision.json", task["result"]["human_decision"])


def prepare_correction_validation(store):
    """Create the fixed fixture and one approval request; never consumes quota."""
    from . import correction_fixture as fixture
    for task in store.tasks():
        if task['kind'] == 'correction_validation' and task['state'] == 'awaiting_approval':
            try:
                run = APP['approved_run'](task['payload']['run'])
                if json.loads((run / 'artifacts/correction_plan.json').read_text())['fingerprint'] == fingerprint():
                    return task['id']
            except (OSError, ValueError, KeyError):
                pass
            store.cancel_pending(task['id'], reason='PREPARED_SOURCE_CHANGED_BEFORE_APPROVAL')
            for check in store.tasks():
                if check['kind'] == 'correction_preflight' and check['state'] == 'queued' and check['payload'].get('run') == task['payload'].get('run'):
                    store.cancel_pending(check['id'], reason='PREPARED_SOURCE_CHANGED_BEFORE_EXECUTION')
    preparer = runpy.run_path(str(PROTO / 'prepare_live_run.py'), run_name='correction_fixture_preparation')
    run = preparer['prepare'](reviewer_source(), template=fixture.TEMPLATE)
    APP['save'](run / 'artifacts/correction_plan.json', {'fingerprint': fingerprint(), 'template': fixture.TEMPLATE,
        'limits': POLICIES['correction_validation'], 'initial_developer_argv': APP['commands'](run)['codex'],
        'test_argv': APP['commands'](run)['tests'], 'reviewer_argv': APP['commands'](run)['claude'],
        'objective': fixture.OBJECTIVE, 'initial_step': 'Docstring only, preserve seeded behavior',
        'review_requirement': 'Independent actual finding, never a fabricated finding',
        'acceptance': 'Weak tests pass; independent oracle fails before fix and passes afterward; exact SHA re-reviewed'})
    payload = {'project_name': 'Automatic correction validation', 'template': fixture.TEMPLATE, 'run': str(run)}
    preflight = store.add('correction_preflight', 'Validate seeded fixture under MXC (zero model calls)', payload)
    task_id = store.add('correction_validation', fixture.OBJECTIVE, payload)
    store.record(task_id, 'approval_requested', {'preflight_task': preflight, 'limits': POLICIES['correction_validation'],
        'subscriptions_only': True, 'api_fallback': False, 'model_upgrades': False})
    return task_id


def correction_preflight_ready(store, task):
    return any(other['kind'] == 'correction_preflight' and other['state'] == 'completed'
        and (other['result'] or {}).get('verdict') == 'PASS'
        and (other['result'] or {}).get('fingerprint') == fingerprint()
        and (other['result'] or {}).get('run') == task['payload'].get('run') for other in store.tasks())


def check_correction_plan(task):
    from . import correction_fixture as fixture
    from .collaboration import submission
    submission(task['objective'], task['payload'])
    run = APP['approved_run'](task['payload']['run'])
    plan = json.loads((run / 'artifacts/correction_plan.json').read_text())
    if (task['kind'] != 'correction_validation' or plan.get('template') != fixture.TEMPLATE
            or plan.get('fingerprint') != fingerprint() or plan.get('limits') != POLICIES['correction_validation']
            or plan.get('initial_developer_argv') != APP['commands'](run)['codex']
            or plan.get('reviewer_argv') != APP['commands'](run)['claude']):
        raise RuntimeError('PREPARED_CORRECTION_PLAN_SOURCE_CHANGED')
    if (run / 'result.json').exists() or (run / 'artifacts/collaboration_attempt.json').exists():
        raise RuntimeError('USED_CORRECTION_RUN_NO_REPLAY')
    return run


def checkpoint_project():
    """Authorized source checkpoint, only from the trusted normal-host worker."""
    import subprocess
    import re
    context = APP['PROCESS']['DIAGNOSTICS']['execution_context']()
    if context != {'appcontainer': False, 'elevated': False}:
        raise RuntimeError('NORMAL_NON_ELEVATED_HOST_REQUIRED_FOR_PROJECT_CHECKPOINT')
    # This is the new application repository, never a user-selected project.
    if not (ROOT / '.git/config').is_file() or (ROOT / '.gitattributes').exists() or (ROOT / '.git/info/attributes').exists():
        raise RuntimeError('PROJECT_GIT_CONFIGURATION_REQUIRES_REVIEW')
    config = (ROOT / '.git/config').read_text(encoding='utf-8')
    if re.findall(r'(?m)^\s*\[([^\]]+)\]', config) != ['core']:
        raise RuntimeError('PROJECT_GIT_CONFIGURATION_REQUIRES_REVIEW')
    empty = DATA / 'empty_project_git'
    empty.mkdir(exist_ok=True)
    if any(empty.iterdir()):
        raise RuntimeError('PROJECT_GIT_HOOK_DIRECTORY_NOT_EMPTY')
    environment = APP['codex_env'](PROTO / 'coordinator_environment')
    environment.update(GIT_CONFIG_GLOBAL=str(empty / 'absent'), GIT_CONFIG_SYSTEM=str(empty / 'absent'))
    base = [APP['GIT'], '-c', 'core.hooksPath=' + str(empty), '-c', 'core.fsmonitor=false',
        '-c', 'core.attributesFile=' + str(empty / 'absent'), '-c', 'commit.gpgsign=false',
        '-c', 'user.name=Dani Agent', '-c', 'user.email=dani-agent@example.invalid']
    def git(*args, allow_one=False):
        proc = subprocess.run(base + list(args), cwd=ROOT, env=environment, capture_output=True, encoding='utf-8', timeout=HELPER_SECONDS,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode not in ((0, 1) if allow_one else (0,)):
            raise RuntimeError('PROJECT_GIT_CHECKPOINT_FAILED')
        return proc
    if git('diff', '--cached', '--quiet', allow_one=True).returncode:
        raise RuntimeError('PROJECT_GIT_ALREADY_HAS_STAGED_CHANGES')
    paths = ['.gitignore', 'DaniAgent.py', 'Start-Dani-Agent.cmd'] + [p.name for p in ROOT.glob('*.md')]
    paths += [str(p.relative_to(ROOT)) for folder in (ROOT / 'dani_agent', PROTO) for p in folder.glob('*.py')]
    git('add', '--', *paths)
    if git('diff', '--cached', '--quiet', allow_one=True).returncode:
        git('commit', '-m', 'Checkpoint approved Dani Agent source and documentation')
    return {'revision': git('rev-parse', 'HEAD').stdout.strip(), 'remote_or_push': False}


def check_diagnostic_plan(task):
    run = APP["approved_run"](task["payload"].get("run", ""))
    plan = json.loads((run / "artifacts/exec_diagnostic_plan.json").read_text())
    sources = {name: hashlib.sha256((PROTO / name).read_bytes()).hexdigest()
        for name in ("orchestrator.py", "process.py", "diagnostics.py", "codex_exec_diagnostic.py")}
    if plan.get("source_hashes") != sources or plan.get("argv") != APP["commands"](
            run, exec_diagnostic=True, model=plan.get("model", APP["TOY_CODEX_MODEL"]))["codex"]:
        raise RuntimeError("STALE_DIAGNOSTIC_PLAN_RUN_NEW_MODEL_FREE_HOST_CHECK")


def partial_result(folder, reason):
    result = {"verdict": "BLOCKED", "reason": reason, "interrupted": True,
        "containment_after_interruption": "UNVERIFIED_UNLESS_RECORDED", "subscription_quota_used": None}
    try:
        status = read_json(folder / "status.json")
        if status.get("run"):
            run = APP["approved_run"](status["run"])
            result["run"] = str(run)
            path = run / "artifacts/workflow_progress.json"
            if path.is_file():
                saved = read_json(path)
                result.update(agent_sessions=saved["agent_sessions"], usage=saved["usage"], partial_cycles=saved["cycles"])
        journals = list(folder.glob("*.progress.jsonl"))
        if journals:
            result["progress"] = APP["PROCESS"]["last_progress"](max(journals, key=lambda path: path.stat().st_mtime))
    except (OSError, ValueError, KeyError):
        pass
    return result


def recover_saved_results(store):
    """Publish a fully finished worker after a UI crash; never replay the worker."""
    recovered = 0
    for task in store.tasks():
        if task["state"] != "running":
            continue
        folder = DATA / "jobs" / task["id"]
        try:
            result = json.loads((folder / "result.json").read_text())
            complete = False
            for path in folder.glob("*.process.json"):
                process = json.loads(path.read_text())
                containment = json.loads(path.with_name(path.name.replace(".process.json", ".containment.json")).read_text())
                complete = (process.get("exit_code") in (0, 1) and not process.get("error")
                    and all(containment.get(key) is True for key in ("assigned_before_resume", "job_queries_passed",
                        "job_empty_after_termination", "job_termination_succeeded", "all_observed_processes_stopped")))
                if complete:
                    break
            if not complete:
                continue
            with store.connection() as db:
                active = db.execute("SELECT id FROM runs WHERE task_id=? AND state='running'", (task["id"],)).fetchone()
            if active:
                state = "completed" if result.get("verdict") == "PASS" else (
                    "blocked" if result.get("verdict") == "BLOCKED" else "failed")
                if task["kind"] in ("collaboration", "correction_validation", "project_development"):
                    from .collaboration import result_state
                    state = result_state(result)
                store.finish(task["id"], active[0], state, result)
                if task["kind"] == "codex_preflight" and state == "completed":
                    queue_diagnostic(store, result)
                recovered += 1
        except (OSError, ValueError, KeyError):
            continue
    interrupted = 0
    for task in store.tasks():
        if task["state"] != "running":
            continue
        with store.connection() as db:
            active = db.execute("SELECT id FROM runs WHERE task_id=? AND state='running'", (task["id"],)).fetchone()
        if active:
            result = partial_result(DATA / "jobs" / task["id"], "COORDINATOR_RESTART_NO_AUTOMATIC_MODEL_REPLAY")
            store.finish(task["id"], active[0], "interrupted", result)
            interrupted += 1
    interrupted += store.recover()
    return {"published_results": recovered, "interrupted": interrupted}


def worker(store_path, task_id, job_dir):
    """Called by the trusted contained child, never by a provider-generated command."""
    if Path(store_path).resolve() != (DATA / "dani_agent.sqlite3").resolve():
        raise RuntimeError("INVALID_STATE_DATABASE")
    job_dir = Path(job_dir).resolve()
    if job_dir.parent != (DATA / "jobs").resolve() or job_dir.name != task_id:
        raise RuntimeError("INVALID_JOB_DIRECTORY")
    store = Store(store_path)
    task = store.get(task_id)
    if task["state"] != "running":
        raise RuntimeError("TASK_NOT_CLAIMED")
    approved_fingerprint = None
    if needs_approval(task['kind']):
        with store.connection() as db:
            approval = db.execute("SELECT * FROM approvals WHERE task_id=?", (task_id,)).fetchone()
            if (not approval or not approval["consumed"] or approval["fingerprint"] != fingerprint()
                    or json.loads(approval["policy"]) != approval_scope(task)):
                raise RuntimeError("EXPLICIT_CURRENT_APPROVAL_REQUIRED")
            approved_fingerprint = approval['fingerprint']
    # A consumed DB approval cannot start two workers, even after a crash or an
    # accidental duplicate child launch. Preserve the latch as audit evidence.
    APP["save"](job_dir / "worker_started.json", {"task_id": task_id, "fingerprint": fingerprint()})
    status_path = job_dir / "status.json"
    def status(phase, **extra):
        if approved_fingerprint is not None and fingerprint() != approved_fingerprint:
            raise RuntimeError('SOURCE_CHANGED_DURING_APPROVED_WORKFLOW')
        atomic_json(status_path, {"phase": phase, **extra})
    result = {"verdict": "BLOCKED", "reason": "NOT_FINISHED", "codex_sessions": 0, "claude_sessions": 0}
    try:
        kind = task["kind"]
        status("preparing")
        if kind in ("codex_preflight", "codex_diagnostic", "integration", "collaboration", "correction_preflight", "correction_validation"):
            host = runpy.run_path(str(PROTO / "host_launcher.py"), run_name="coordinator_host_requirement")
            host["require_host"](host["DIAGNOSTICS"]["execution_context"]())
        if kind.startswith('project_') or kind in ('milestone_review', 'reviewer_profile', 'claude_model_probe'):
            if APP['PROCESS']['DIAGNOSTICS']['execution_context']() != {'appcontainer': False, 'elevated': False}:
                raise RuntimeError('NORMAL_NON_ELEVATED_HOST_REQUIRED')
        if kind == "refresh_evidence":
            count = import_evidence(store)
            result.update(verdict="PASS", reason="EVIDENCE_IMPORTED", imported=count)
        elif kind == "offline_tests":
            status("offline_tests")
            # No time limit (the suite grows, and takes longer while other work runs): stopped only if it prints
            # nothing for SUITE_IDLE_SECONDS. The log in the job folder also shows the job is alive (job_activity).
            returncode = run_while_active([APP["PYTHON"], "-I", "-B", str(PROTO / "run_checks.py")],
                Path(job_dir) / "offline_tests.log", SUITE_IDLE_SECONDS,
                cwd=ROOT, env=APP["codex_env"](PROTO / "coordinator_environment"))
            if returncode == 3:   # run_checks.py: another self-test already holds the one-at-a-time lock
                result.update(verdict="BLOCKED", reason="SELF_TEST_ALREADY_RUNNING")
            elif returncode:
                result.update(verdict="FAIL", reason="OFFLINE_TESTS_FAILED")
            else:
                host = runpy.run_path(str(PROTO / "host_launcher.py"), run_name="coordinator_test_results")
                result.update(verdict="PASS", reason="OFFLINE_TESTS_PASSED", tests=host["verify_offline_tests"]())
        elif kind in ("codex_preflight", "codex_diagnostic"):
            diagnostic = runpy.run_path(str(PROTO / "codex_exec_diagnostic.py"), run_name="coordinator_exec_diagnostic")
            if kind == "codex_preflight":
                status("preparing_disposable_git_repository")
                run = prepare_exec_run(task["payload"].get("model"))
            else:
                run = APP["approved_run"](task["payload"].get("run", ""))
                if not any(other["kind"] == "codex_preflight" and other["state"] == "completed"
                        and (other["result"] or {}).get("run") == str(run) for other in store.tasks()):
                    raise RuntimeError("PASSING_HOST_PREFLIGHT_REQUIRED")
            status("model_free_checks" if kind == "codex_preflight" else "codex_diagnostic", run=str(run))
            code = diagnostic["run_diagnostic"](run, approved=kind == "codex_diagnostic")
            paths = sorted((run / "artifacts").glob("exec_diagnostic_*/report.json"), key=lambda path: path.stat().st_mtime)
            observed = json.loads(paths[-1].read_text()) if paths else {}
            result.update(observed, run=str(run))
            model = json.loads((run / "artifacts/exec_diagnostic_plan.json").read_text()).get("model", APP["TOY_CODEX_MODEL"])
            result["model"] = model
            if kind == "codex_preflight" and code == 2:
                result.update(verdict="PASS", reason="MODEL_FREE_HOST_READY_APPROVAL_REQUIRED_FOR_MODELS")
            if kind == "codex_diagnostic" and result.get("codex_sessions"):
                from .model_settings import record_probe
                record_probe("codex", model, result.get("verdict") == "PASS", str(run))
        elif kind == "reviewer_profile":
            from .connect import create_reviewer_profile
            result.update(create_reviewer_profile(), agent_sessions={"codex": 0, "claude": 1})
        elif kind == "claude_model_probe":
            from .connect import probe_claude
            result.update(probe_claude(task["payload"]["model"], job_dir), agent_sessions={"codex": 0, "claude": 1})
        elif kind == 'correction_preflight':
            from . import correction_fixture as fixture
            run = APP['approved_run'](task['payload']['run'])
            result['run'] = str(run)
            plan = json.loads((run / 'artifacts/correction_plan.json').read_text())
            if plan['fingerprint'] != fingerprint() or plan['limits'] != POLICIES['correction_validation']:
                raise RuntimeError('PREPARED_CORRECTION_PLAN_SOURCE_CHANGED')
            status('correction_fixture_preflight', run=str(run))
            host = runpy.run_path(str(PROTO / 'host_launcher.py'), run_name='correction_model_free_host')
            if not host['check'](run):
                raise RuntimeError('CORRECTION_SECURITY_PREFLIGHT_FAILED')
            before = {name: APP['inventory'](run / name) for name in ('repo', 'control', 'review_snapshot')}
            folder = run / 'artifacts/fixture_preflight'
            folder.mkdir()
            def unchanged():
                if any(APP['inventory'](run / name) != value for name, value in before.items()):
                    raise RuntimeError('FIXTURE_PREFLIGHT_MUTATED_INPUT')
            tests = APP['PROCESS']['run']({'argv': APP['commands'](run)['tests'], 'cwd': str(run / 'repo'),
                'env': APP['codex_env'](run), 'kind': 'plain'}, folder, timeout=30, watch=unchanged)
            argv = APP['commands'](run)['tests']
            oracle = APP['PROCESS']['run']({'argv': argv[:argv.index('--') + 1] + [APP['PYTHON'], '-I', '-B',
                str(run / 'control/acceptance_test.py'), str(run / 'repo/toy_math.py')], 'cwd': str(run / 'repo'),
                'env': APP['codex_env'](run), 'kind': 'plain'}, folder, timeout=30, watch=unchanged)
            unchanged()
            if (tests.get('error') or tests['exit_code'] != 0 or 'Ran 8 tests' not in tests['stdout'] or 'OK' not in tests['stdout']
                    or oracle.get('error') or oracle['exit_code'] != 1 or 'Ran 5 tests' not in oracle['stdout']
                    or 'FAILED (failures=1)' not in oracle['stdout'] or 'ValueError not raised' not in oracle['stdout']):
                raise RuntimeError('SEEDED_FIXTURE_TEST_GAP_NOT_VERIFIED')
            result.update(verdict='PASS', reason='WEAK_TESTS_PASS_SEEDED_BUG_CONFIRMED_BY_ORACLE',
                tests=tests, acceptance_oracle=oracle, fingerprint=fingerprint())
            APP['save'](run / 'artifacts/fixture_preflight.json', result)
        elif kind == 'project_scan':
            from .projects import discover
            summary = discover(task['payload']['path'])
            project_id = store.save_project(summary)
            result.update(verdict='PASS', reason='PROJECT_DISCOVERED_READ_ONLY', project_id=project_id, summary=summary)
        elif kind == 'project_prepare':
            from . import workspaces as workspace
            project = next(p['summary'] for p in store.projects() if p['id'] == task['payload']['project_id'])
            run = workspace.prepare(project, task['objective'], task['payload']['editable_paths'],
                task['payload']['test_id'], task['payload']['settings'])
            payload = {'run': str(run), 'project_id': task['payload']['project_id'], 'settings': task['payload']['settings']}
            check_id = store.add('project_preflight', 'Verify native isolated project security (no models)', payload)
            live_id = store.add('project_development', task['objective'], payload)
            store.record(live_id, 'approval_requested', {'preflight_task': check_id,
                'scope': approval_scope(store.get(live_id)), 'subscriptions_only': True})
            result.update(verdict='PASS', reason='PROJECT_WORKTREE_PREPARED_APPROVAL_REQUIRED', run=str(run),
                preflight_task=check_id, development_task=live_id)
        elif kind == 'project_validation_prepare':
            from .phase2_fixtures import create
            from .model_settings import DEFAULTS, validate
            queued = []
            settings = validate({**DEFAULTS, 'max_codex_sessions': 2, 'max_claude_sessions': 2, 'max_correction_cycles': 1})
            for fixture in create():
                project_id = store.save_project(fixture['summary'])
                queued.append(store.add('project_prepare', fixture['objective'], {'project_id': project_id,
                    'editable_paths': fixture['editable_paths'], 'test_id': fixture['test_id'], 'settings': settings}))
            result.update(verdict='PASS', reason='DISPOSABLE_VALIDATION_PREPARATIONS_QUEUED', tasks=queued, model_calls=0)
        elif kind == 'project_preflight':
            from .project_workflow import preflight, diagnose
            run = APP['approved_run'](task['payload']['run'])
            status('project_security_preflight', run=str(run))
            if task['payload'].get('diagnostic_only'):
                result.update(diagnose(run), run=str(run))
            else:
                host = runpy.run_path(str(PROTO / 'host_launcher.py'), run_name='project_offline_evidence')
                host['verify_offline_tests']()
                observed = preflight(run)
                result.update(observed, run=str(run), reason='PROJECT_SECURITY_READY_FOR_EXPLICIT_APPROVAL')
        elif kind == 'project_development':
            from .project_workflow import execute, check_ready
            run = APP['approved_run'](task['payload']['run'])
            plan = check_ready(run)
            if task['payload']['settings'] != plan['settings'] or task['objective'] != plan['objective']:
                raise RuntimeError('PROJECT_APPROVAL_PLAN_MISMATCH')
            APP['save'](run / 'artifacts/local_live_approval.json', {'task_id': task_id,
                'fingerprint': fingerprint(), 'scope': approval_scope(task), 'approval_source': 'DESKTOP_HUMAN_APPROVAL'})
            def project_event(name, value):
                if fingerprint() != approved_fingerprint:
                    raise RuntimeError('SOURCE_CHANGED_DURING_APPROVED_WORKFLOW')
                store.record(task_id, name, value)
            result.update(execute(run, progress=status, event=project_event), run=str(run))
        elif kind == 'project_apply':
            from .project_apply import apply
            result.update(apply(store, task['payload'], job_dir,
                watch=lambda: status('applying_accepted_revision')))
        elif kind == 'project_recover':
            from .project_apply import recover
            result.update(recover(store, task['payload'], job_dir,
                watch=lambda: status('recovering_interrupted_apply')))
        elif kind == 'product_build':
            from . import products
            product = products.load(task['payload']['product_id'])
            if (product['budget'] != task['payload']['budget']
                    or products.limits(product['budget']) != task['payload']['limits']):
                raise RuntimeError('PRODUCT_APPROVAL_BUDGET_MISMATCH')
            def product_event(name, value):
                if fingerprint() != approved_fingerprint:
                    raise RuntimeError('SOURCE_CHANGED_DURING_APPROVED_WORKFLOW')
                store.record(task_id, name, value)
            result.update(products.build(product['id'], task_id=task_id, status=status, event=product_event,
                                         mode=task['payload'].get('mode', 'build'),
                                         **({'approved_attachments_sha256': task['payload']['attachments_sha256']}
                                            if 'attachments_sha256' in task['payload'] else {})))
        elif kind == 'milestone_review':
            from .milestone_review import execute
            result.update(run=task['payload']['run'], scope='LIVE_MILESTONE_REVIEW')
            review_run = APP['approved_run'](task['payload']['run'])
            APP['save'](review_run / 'artifacts/local_live_approval.json', {'task_id': task_id,
                'fingerprint': fingerprint(), 'scope': approval_scope(task), 'approval_source': 'DESKTOP_HUMAN_APPROVAL'})
            def review_status(phase, **extra):
                if phase == 'claude_milestone_review':
                    result['agent_sessions'] = {'codex': 0, 'claude': extra.get('review_batch', 1)}
                    flags = APP['commands'](review_run)['claude']
                    flags = dict(zip(flags, flags[1:]))
                    extra.update(agent='Claude', model=flags.get('--model'), effort=flags.get('--effort'))
                status(phase, **extra)
            result.update(execute(task, job_dir, status=review_status))
        elif kind in ("integration", "collaboration", "correction_validation"):
            if not integration_ready(store):
                raise RuntimeError("REAL_CODEX_DIAGNOSTIC_HAS_NOT_PASSED")
            status("preparing_disposable_git_repository")
            preparer = runpy.run_path(str(PROTO / "prepare_live_run.py"), run_name="coordinator_integration_prepare")
            if kind == 'correction_validation':
                if not correction_preflight_ready(store, task):
                    raise RuntimeError('SEEDED_FIXTURE_PREFLIGHT_REQUIRED')
                run = check_correction_plan(task)
            else:
                run = preparer["prepare"](reviewer_source())
            result["run"] = str(run)
            status("integration_preflight", run=str(run))
            host = runpy.run_path(str(PROTO / "host_launcher.py"), run_name="coordinator_integration_host")
            if kind != 'correction_validation' and not host["check"](run):
                raise RuntimeError("INTEGRATION_SECURITY_PREFLIGHT_FAILED")
            if kind == 'correction_validation':
                APP['preflight'](run)
            status("codex_tests_claude_handoff", run=str(run))
            APP["save"](run / "artifacts/local_live_approval.json", {"task_id": task_id,
                "fingerprint": fingerprint(), "scope": approval_scope(task), "approval_source": "DESKTOP_HUMAN_APPROVAL"})
            if kind in ("collaboration", "correction_validation"):
                from .collaboration import execute
                def workflow_event(name, value):
                    if fingerprint() != approved_fingerprint:
                        raise RuntimeError("SOURCE_CHANGED_DURING_APPROVED_WORKFLOW")
                    store.record(task_id, name, value)
                observed = execute(run, task, progress=status,
                    event=workflow_event)
            else:
                observed = APP["pipeline"](run)
            result.update(observed, run=str(run))
        else:
            raise RuntimeError("UNKNOWN_WORKFLOW")
    except Exception as exc:
        result["reason"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        if getattr(exc, "evidence", None):
            result["failure_evidence"] = exc.evidence
    # Integration and collaboration reports count attempts in agent_sessions.
    if "agent_sessions" in result:
        result.update(codex_sessions=result["agent_sessions"]["codex"], claude_sessions=result["agent_sessions"]["claude"])
    APP["save"](job_dir / "result.json", result)
    status("finished", verdict=result["verdict"], reason=result["reason"], run=result.get("run"))
    return 0 if result["verdict"] == "PASS" else 1


# Quick helper commands get a generous hang guard only; nothing on the product path stops for time (2026-10-23).
HELPER_SECONDS = 300
SUITE_IDLE_SECONDS = 10 * 60


def run_while_active(argv, log_path, idle, **options):
    """Run a command with its output going to log_path; no time limit, but stop it (OFFLINE_TESTS_STALLED) once
    the log has not grown for `idle` seconds."""
    import subprocess
    with Path(log_path).open("wb") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW, **options)
        size, changed = -1, time.monotonic()
        while proc.poll() is None:
            time.sleep(1)
            current = Path(log_path).stat().st_size
            if current != size:
                size, changed = current, time.monotonic()
            elif time.monotonic() - changed >= idle:
                kill_tree(proc)
                raise RuntimeError("OFFLINE_TESTS_STALLED")
    return proc.returncode


def kill_tree(proc):
    """Stop a process and everything it started; a plain kill leaves its children running."""
    import subprocess
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=HELPER_SECONDS, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def job_activity(folder):
    """Changes only when a worker job makes progress: its status, the size of its logs, or what its journals and the
    journals of the sessions in its current run record (output so far, protocol milestones, phase). Never file times:
    every journal, the job's own wrapper journal included, is rewritten about twice a second while its process is
    alive, so a hung job would look busy forever (review 7d50d9b4)."""
    token = APP['PROCESS']['activity_token']
    journals = sorted(folder.glob('*.progress.jsonl'))
    sign = {}
    try:
        status = (folder / 'status.json').read_text(encoding='utf-8') if (folder / 'status.json').exists() else ''
        sign['status'] = status
        run = json.loads(status).get('run') if status else None
        if run:
            journals += sorted((APP['approved_run'](run) / 'artifacts').rglob('*.progress.jsonl'))
    except (OSError, ValueError, RuntimeError, KeyError, AttributeError):
        pass
    for path in sorted(folder.glob('*.log')):
        try:
            sign[str(path)] = path.stat().st_size
        except OSError:
            continue
    for path in journals:
        try:
            sign[str(path)] = token(path)
        except (OSError, ValueError):
            continue
    return json.dumps(sign, sort_keys=True)


class Coordinator:
    def __init__(self, store, *, offline_only=False):
        self.store = store
        self.offline_only = offline_only
        self.cancel_event = threading.Event()
        self.thread = None
        self.current = None
        self.switch = threading.Lock()   # current and cancel_event change together
        self.last_error = None
        try:
            self.host_context = APP["PROCESS"]["DIAGNOSTICS"]["execution_context"]()
        except OSError:
            self.host_context = {}

    def tick(self):
        if self.last_error:
            return
        if self.thread and self.thread.is_alive():
            return
        normal_host = self.host_context.get("appcontainer") is False and self.host_context.get("elevated") is False
        allowed = set(POLICIES) if normal_host else {"refresh_evidence", "offline_tests"}
        if self.offline_only:
            allowed &= {"refresh_evidence", "offline_tests"}   # even previously approved model tasks stay unconsumed
        if not integration_ready(self.store):
            allowed -= {"integration", "collaboration", "correction_validation", "project_development", "product_build"}
        with self.switch:   # cancel(task_id) waits until a claimed task is also current
            task = self.store.claim(fingerprint(), allowed)
            if task is None:
                return
            self.current = task
            self.cancel_event.clear()
        self.thread = threading.Thread(target=self._execute, args=(task,), daemon=True)
        self.thread.start()

    def _execute(self, task):
        folder = DATA / "jobs" / task["id"]
        result, state = {}, "blocked"
        try:
            folder.mkdir(parents=True, exist_ok=False)
            if self.cancel_event.is_set():
                raise RuntimeError('USER_CANCELLED')
            def watch():
                if self.cancel_event.is_set():
                    raise RuntimeError("USER_CANCELLED")
                if needs_approval(task['kind']) and fingerprint() != task['fingerprint']:
                    raise RuntimeError('SOURCE_CHANGED_DURING_APPROVED_WORKFLOW')
            # This outer job contains the trusted worker and all nested provider
            # jobs. Closing/crashing the UI also closes its kill-on-close handle.
            timeout = 1050 if task["kind"] == 'collaboration' else 660 if task['kind'] == 'correction_validation' else 1560 if task['kind'] == 'offline_tests' else 360 if task["kind"] == "integration" else 180
            idle, activity = None, None
            if task['kind'] in ('project_development', 'product_build', 'milestone_review', 'offline_tests'):
                # No time limit for product work: the job runs while anything in it shows activity.
                from .model_settings import JOB_IDLE_SECONDS  # model_settings imports this module
                timeout, idle, activity = None, JOB_IDLE_SECONDS, lambda: job_activity(folder)
            output = APP["PROCESS"]["run"]({"argv": [APP["PYTHON"], "-I", "-B", str(ROOT / "DaniAgent.py"),
                "--worker", task["id"], "--store", str(self.store.path), "--job-dir", str(folder)],
                "cwd": str(ROOT), "env": APP["codex_env"](PROTO / "coordinator_environment"), "kind": "plain"},
                folder, timeout=timeout, watch=watch, idle=idle, activity=activity)
            if not (folder / "result.json").exists():
                raise RuntimeError("WORKER_RESULT_MISSING")
            result = json.loads((folder / "result.json").read_text())
            state = "completed" if result["verdict"] == "PASS" and output["exit_code"] == 0 else (
                "blocked" if result["verdict"] == "BLOCKED" else "failed")
            if task["kind"] in ("collaboration", "correction_validation", "project_development") and output["exit_code"] in (0, 1):
                from .collaboration import result_state
                state = result_state(result)
        except Exception as exc:
            result = partial_result(folder, str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
            state = "cancelled" if str(exc) == "USER_CANCELLED" else "blocked"
            if getattr(exc, "evidence", None):
                result["failure_evidence"] = exc.evidence
            try:
                with (folder / "INTERRUPTED_REPORT.md").open("x", encoding="utf-8") as out:
                    out.write("# Dani Agent: interrupted\n\n" + result["reason"]
                        + "\n\nNo agent session will be replayed. Partial evidence remains in the saved run.\n")
            except (OSError, ValueError, KeyError):
                pass
        finally:
            try:
                self.store.finish(task["id"], task["run_id"], state, result)
                if task["kind"] == "codex_preflight" and state == "completed":
                    queue_diagnostic(self.store, result)
                import_evidence(self.store)
            except Exception as exc:
                self.last_error = "STATE_UPDATE_FAILED_" + type(exc).__name__
            finally:
                with self.switch:
                    self.current = None

    def cancel(self, task_id=None):
        """Cancel the running task; with task_id, only if that task is still the one running."""
        with self.switch:
            if task_id is None or (self.current or {}).get('id') == task_id:
                self.cancel_event.set()
                return True
        return False

    def progress(self):
        if self.last_error:
            return {"phase": self.last_error}
        if not self.current:
            host_ready = self.host_context.get("appcontainer") is False and self.host_context.get("elevated") is False
            return {"phase": "idle" if host_ready else "normal_non_elevated_host_required", "host_context": self.host_context}
        folder = DATA / "jobs" / self.current["id"]
        status_path = folder / "status.json"
        try:
            status = read_json(status_path) if status_path.exists() else {"phase": "starting"}
            paths = list(folder.glob("*.progress.jsonl"))
            if status.get("run"):
                run = APP["approved_run"](status["run"])
                inner = list((run / "artifacts").rglob("*.progress.jsonl"))
                paths = inner or paths
            if paths:
                latest = max(paths, key=lambda path: path.stat().st_mtime)
                status["process"] = APP["PROCESS"]["last_progress"](latest)
            return status
        except (OSError, ValueError):
            return {"phase": "progress_pending"}


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("COORDINATOR_ALREADY_RUNNING")

    def close(self):
        if self.stream:
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
            self.stream = None
