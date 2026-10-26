"""Explicit, exact-revision transfer. No agents, Git hooks, merge commands or reset."""
import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path

from .coordinator import APP, DATA
from . import projects as P
from .file_transfer import update_exact, temporary_lock
from . import apply_journal as J


def proposal(store, task_id):
    task = store.get(task_id)
    if task['kind'] != 'project_development' or task['state'] != 'accepted':
        raise ValueError('HUMAN_ACCEPTED_PROJECT_REVISION_REQUIRED')
    result = task['result']
    run = APP['approved_run'](result['run'])
    # Applying is intentionally independent of subsequent application code changes:
    # bind the immutable accepted plan, snapshot and exact revision instead.
    plan = json.loads((run / 'project_control/plan.json').read_text())
    J.ensure_clear(DATA, plan['project']['path'])
    if (P.git(run / 'repo', 'rev-parse', 'HEAD') != result['candidate_sha']
            or APP['inventory'](run / 'repo') != result['candidate_inventory']):
        raise RuntimeError('ACCEPTED_REVISION_CHANGED')
    if any(not (run / 'repo' / name).is_file() for name in result['changed_files']):
        raise RuntimeError('DELETED_FILES_REQUIRE_SEPARATE_MERGE_SUPPORT')
    current = P.discover(plan['project']['path'])
    if not current['eligible'] or current['digest'] != plan['project']['digest']:
        raise RuntimeError('ORIGINAL_CHANGED_APPLY_BLOCKED_NO_OVERWRITE')
    return {'task_id': task_id, 'run': str(run), 'candidate_sha': result['candidate_sha'],
        'original_sha': plan['project']['original_sha'], 'project_path': plan['project']['path'],
        'project_digest': current['digest'], 'changed_files': result['changed_files'],
        'candidate_inventory_sha256': hashlib.sha256(json.dumps(result['candidate_inventory'], sort_keys=True).encode()).hexdigest(),
        'operation': 'Apply exact reviewed file updates/additions under a temporary Git index lock; do not commit, push or deploy.'}


def apply(store, approved_payload, evidence_dir, *, watch=lambda: None):
    current = proposal(store, approved_payload['task_id'])
    if current != approved_payload:
        raise RuntimeError('APPLY_APPROVAL_SCOPE_CHANGED')
    run = APP['approved_run'](current['run'])
    plan = json.loads((run / 'project_control/plan.json').read_text())
    root = P.root_path(current['project_path'])
    originals, updates = {}, {}
    for name in current['changed_files']:
        P.relative(name)
        if P.protected(name) or name not in plan['editable_paths']:
            raise RuntimeError('PROTECTED_FILE_APPLY_REFUSED')
        target = root / name
        for p in (target, *target.parents):
            if p == root:
                break
            if p.exists():
                P.regular(p)
        originals[name] = target.read_bytes() if target.is_file() else None
        expected = plan['project']['working_hashes'].get(name)
        if (hashlib.sha256(originals[name]).hexdigest() if originals[name] is not None else None) != expected:
            raise RuntimeError('ORIGINAL_FILE_CHANGED_APPLY_REFUSED')
        source = run / 'repo' / name
        updates[name] = source.read_bytes() if source.is_file() else None
    evidence_dir = Path(evidence_dir)
    backups = evidence_dir / 'original_files'
    backups.mkdir()
    APP['save'](evidence_dir / 'apply_plan.json', current)
    for index, (name, content) in enumerate(originals.items()):
        if content is not None:
            J.write(backups / str(index), content)
    journal_path = evidence_dir / 'apply_journal.json'
    journal = {'version': 1, 'state': 'PREPARED', 'project_path': str(root),
        'candidate_sha': current['candidate_sha'], 'original_sha': current['original_sha'],
        'task_id': current['task_id'], 'created_directories': {}, 'directory_intents': [],
        'files': {name: {'before': J.digest(originals[name]), 'after': J.digest(updates[name]),
            'backup': str(index) if originals[name] is not None else None} for index, name in enumerate(originals)}}
    for name in updates:
        parent = (root / name).parent
        while parent != root and not parent.exists():
            intent = parent.relative_to(root).as_posix()
            if intent not in journal['directory_intents']:
                journal['directory_intents'].append(intent)
            parent = parent.parent
    J.register(DATA, journal_path, journal)
    J.save(journal_path, journal)
    applied = []

    def replace(name, content, expected):
        target = J.target(root, name)
        actual = target.read_bytes() if target.is_file() else None
        if actual != expected:
            raise RuntimeError('CONCURRENT_PROJECT_EDIT_NO_OVERWRITE')
        if content is None:
            update_exact(target, expected, None)
        else:
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                # Intent is persisted before mkdir, including in the independent
                # immutable baseline registry. Unknown ownership needs approval.
                intent = parent.relative_to(root).as_posix()
                if intent not in journal['directory_intents']:
                    journal['directory_intents'].append(intent)
                J.save(journal_path, journal)
                parent.mkdir()  # No exist_ok: a concurrent creator is not ours.
                journal['created_directories'][parent.relative_to(root).as_posix()] = parent.stat().st_ino
                J.save(journal_path, journal)
            update_exact(target, expected, content)

    current_name = None
    locks = ExitStack()
    try:
        locks.enter_context(temporary_lock(root / '.git/index.lock'))
        fresh = P.discover(root)
        if not fresh['eligible'] or fresh['digest'] != current['project_digest']:
            raise RuntimeError('ORIGINAL_CHANGED_BEFORE_APPLY')
        for name in updates:
            watch()
            current_name = name
            journal.update(state='APPLYING', writing=name)
            J.save(journal_path, journal)
            replace(name, updates[name], originals[name])
            applied.append(name)
        journal.update(state='APPLIED', writing=None)
        J.save(journal_path, journal)
    except Exception as exc:
        rollback_errors = []
        # Both concurrent-edit checks (and CREATE_NEW finding an existing file, winerror 80) fire before
        # any byte is written: that file holds the human's edit, not ours, so it needs no rollback.
        untouched = (str(exc) == 'CONCURRENT_PROJECT_EDIT_NO_OVERWRITE' if isinstance(exc, RuntimeError)
            else isinstance(exc, OSError) and getattr(exc, 'winerror', None) == 80)
        attempted = applied + ([current_name] if current_name is not None and current_name not in applied
            and not untouched else [])
        for name in reversed(attempted):
            try:
                target = root / name
                actual = target.read_bytes() if target.is_file() else None
                if actual == originals[name]:
                    continue
                if actual != updates[name]:
                    raise RuntimeError('CONCURRENT_OR_PARTIAL_EDIT_REQUIRES_BACKUP_RECOVERY')
                replace(name, originals[name], actual)
            except Exception:
                rollback_errors.append(name)
        cause = str(exc) if isinstance(exc,RuntimeError) and str(exc).replace('_','').isalnum() and str(exc).isupper() else type(exc).__name__
        outcome = 'APPLY_FAILED_ROLLBACK_INCOMPLETE' if rollback_errors else 'APPLY_FAILED_ORIGINALS_RESTORED'
        try:
            APP['save'](evidence_dir / 'rollback.json', {'restored': not rollback_errors, 'manual_recovery_files': rollback_errors,
                'backup_order': list(originals), 'cause': cause, 'native_winerror': getattr(exc,'winerror',None)})
            J.cleanup_directories(root, journal['created_directories'])
            journal.update(state='RECOVERY_REQUIRED' if rollback_errors else 'ROLLED_BACK', writing=None)
            J.save(journal_path, journal)
        except Exception:
            # The journal still blocks further applies (PREPARED/APPLYING), so explicit recovery stays
            # mandatory; report that rather than masking the rollback outcome with a raw OS error.
            raise RuntimeError(outcome + '_JOURNAL_NOT_UPDATED_RECOVERY_REQUIRED') from exc
        raise RuntimeError(outcome)
    finally:
        locks.close()
    store.record(current['task_id'], 'human_approved_changes_applied', {'candidate_sha': current['candidate_sha'],
        'files': applied, 'evidence': str(evidence_dir), 'committed': False})
    return {'verdict': 'PASS', 'reason': 'ACCEPTED_FILES_APPLIED_TO_ORIGINAL_WORKTREE', 'candidate_sha': current['candidate_sha'],
        'changed_files': applied, 'committed': False, 'deployed': False, 'backup': str(backups)}


def recovery_proposal(journal_path):
    path = Path(journal_path).resolve()
    if path.parent.parent != (DATA / 'jobs').resolve() or path.name != 'apply_journal.json':
        raise RuntimeError('INVALID_RECOVERY_JOURNAL')
    journal, journal_hash = J.load(DATA, path)
    if journal['state'] in J.TERMINAL:
        raise RuntimeError('RECOVERY_ALREADY_FINISHED')
    root = P.root_path(journal['project_path'])
    files = {}
    for name, record in journal['files'].items():
        if P.protected(name):
            raise RuntimeError('PROTECTED_RECOVERY_PATH')
        target = J.target(root, name)
        actual = target.read_bytes() if target.is_file() else None
        if record['backup'] is not None:
            backup = path.parent / 'original_files' / record['backup']
            if backup.parent != path.parent / 'original_files':
                raise RuntimeError('INVALID_BACKUP_PATH')
            P.regular(backup)
            if J.digest(backup.read_bytes()) != record['before']:
                raise RuntimeError('RECOVERY_BACKUP_CHANGED')
        files[name] = {'observed': J.digest(actual), 'restore': record['before'],
            'possibly_concurrent_or_partial': J.digest(actual) not in (record['before'], record['after'])}
    directories = {}
    for name in set(journal.get('directory_intents', [])) | set(journal['created_directories']):
        directory = J.target(root, name)
        if directory.exists():
            if not directory.is_dir():
                raise RuntimeError('RECOVERY_DIRECTORY_REPLACED_BY_FILE')
            inode = directory.stat().st_ino
            directories[name] = {'inode': inode, 'entries_sha256': J.digest(json.dumps(sorted(p.name for p in directory.iterdir())).encode()),
                'confirmed_created_by_apply': journal['created_directories'].get(name) == inode}
        else:
            directories[name] = None
    return {'journal': str(path), 'journal_sha256': journal_hash, 'directories': directories,
        'project_path': str(root), 'original_sha': P.git(root, 'rev-parse', 'HEAD'), 'files': files,
        'operation': 'Restore backed-up originals, replacing only exact displayed current contents; may discard partial or human edits. Remove only approved inode-matching empty intent directories, including unconfirmed ownership. No models or Git commit.'}


def recover(store, approved_payload, evidence_dir, *, watch=lambda: None):
    if recovery_proposal(approved_payload['journal']) != approved_payload:
        raise RuntimeError('RECOVERY_SCOPE_CHANGED_NO_OVERWRITE')
    path = Path(approved_payload['journal'])
    journal, _ = J.load(DATA, path)
    root = P.root_path(journal['project_path'])
    evidence_dir = Path(evidence_dir)
    recovery_backups = evidence_dir / 'before_recovery'
    recovery_backups.mkdir()
    if path.exists():
        P.regular(path)
        J.write(evidence_dir / 'journal_before_recovery.bin', P.bounded_read(path))
    with temporary_lock(root / '.git/index.lock'):
        if recovery_proposal(path) != approved_payload:
            raise RuntimeError('RECOVERY_SCOPE_CHANGED_NO_OVERWRITE')
        for index, (name, record) in enumerate(journal['files'].items()):
            watch()
            target = J.target(root, name)
            actual = target.read_bytes() if target.is_file() else None
            if J.digest(actual) != approved_payload['files'][name]['observed']:
                raise RuntimeError('RECOVERY_CURRENT_FILE_CHANGED')
            if actual is not None:
                J.write(recovery_backups / str(index), actual)
            content = (path.parent / 'original_files' / record['backup']).read_bytes() if record['backup'] is not None else None
            if J.digest(content) != record['before']:
                raise RuntimeError('RECOVERY_BACKUP_CHANGED')
            if actual != content:
                update_exact(target, actual, content)
        approved_dirs = {name: value['inode'] for name, value in approved_payload['directories'].items() if value is not None}
        approved_dirs = dict(sorted(approved_dirs.items(), key=lambda row: len(Path(row[0]).parts)))
        J.cleanup_directories(root, approved_dirs)
        journal.update(state='RECOVERED', writing=None)
        J.save(path, journal)
    store.record(journal['task_id'], 'interrupted_apply_recovered', {'journal': str(path), 'backup': str(recovery_backups)})
    return {'verdict': 'PASS', 'reason': 'INTERRUPTED_APPLY_RECOVERED', 'backup': str(recovery_backups), 'model_calls': 0}
