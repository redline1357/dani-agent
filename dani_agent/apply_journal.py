"""Durable write-ahead evidence; pending transfers block further application."""
import hashlib
import json
import os
import ctypes
from ctypes import wintypes
from pathlib import Path
import uuid
import time

from . import projects as P
from .file_transfer import shared_reader

TERMINAL = {'APPLIED', 'ROLLED_BACK', 'RECOVERED'}
IMMUTABLE = ('version', 'project_path', 'candidate_sha', 'original_sha', 'task_id', 'files')


def valid(value):
    return (type(value) is dict and type(value.get('state')) is str and value.get('state') in TERMINAL | {'PREPARED','APPLYING','RECOVERY_REQUIRED'}
        and type(value.get('project_path')) is str and type(value.get('candidate_sha')) is str
        and type(value.get('files')) is dict and type(value.get('created_directories')) is dict)


def digest(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def write(path, content):
    with Path(path).open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    # Unique staging files retain interrupted updates; none is replayed.
    with temp.open('xb') as stream:
        stream.write(json.dumps(value, indent=2).encode())
        stream.flush()
        os.fsync(stream.fileno())
    move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    for attempt in range(3):
        if move(str(temp), str(path), 0x1 | 0x8):  # replace + write-through
            break
        code = ctypes.get_last_error()
        if code not in (5,32,33) or attempt==2:
            raise ctypes.WinError(code)
        # A local file reader/antivirus may briefly hold a non-delete-sharing
        # handle. This bounded journal I/O retry never retries an agent session.
        time.sleep(.01*(attempt+1))


def register(data, path, journal):
    """Immutable baseline outside jobs: lost job evidence cannot clear a block."""
    path, data = Path(path).resolve(), Path(data).resolve()
    if path.parent.parent != data / 'jobs':
        return  # Unit fixtures outside production jobs have their own evidence.
    registry = data / 'apply_registry'
    registry.mkdir(exist_ok=True)
    P.regular(registry)
    record_path = registry / (path.parent.name + '.json')
    if record_path.exists():
        raise RuntimeError('RECOVERY_BASELINE_ALREADY_REGISTERED')
    save(record_path, {'journal': str(path), 'baseline': journal})


def load(data, path):
    path = Path(path)
    registry = Path(data) / 'apply_registry' / (path.parent.name + '.json')
    original, raw = None, b'MISSING_OR_UNREADABLE'
    try:
        P.regular(path)
        with shared_reader(path) as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw)>2 * 1024 * 1024:
            raise ValueError('record size')
        original = json.loads(raw)
        if not valid(original):
            original = None
    except (OSError, ValueError, TypeError, RuntimeError):
        original = None
    if original is not None and registry.exists():
        # A valid-looking journal (e.g. edited or restored to a terminal state) must still describe
        # the registered apply; only state/progress fields legitimately change after registration.
        try:
            P.regular(registry)
            baseline = json.loads(P.bounded_read(registry))['baseline']
            if any(original.get(k) != baseline.get(k) for k in IMMUTABLE):
                original = None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            original = None
    if original is not None:
        return original, digest(raw)
    P.regular(registry)
    baseline_raw = P.bounded_read(registry)
    record = json.loads(baseline_raw)
    if record['journal'] != str(path.resolve()):
        raise RuntimeError('RECOVERY_REGISTRY_PATH_MISMATCH')
    value = record['baseline']
    if not valid(value):
        raise RuntimeError('RECOVERY_BASELINE_CORRUPT')
    value['state'] = 'RECOVERY_REQUIRED'
    return value, digest(raw + baseline_raw)


def pending(data):
    data = Path(data)
    output, registered = [], set()
    for record_path in sorted((data / 'apply_registry').glob('*.json')):
        try:
            P.regular(record_path)
            record = json.loads(P.bounded_read(record_path))
            path = Path(record['journal'])
            if path.resolve().parent.parent != (data / 'jobs').resolve():
                raise ValueError('registry path')
            registered.add(path.resolve())
            value, _ = load(data, path)
            if value['state'] not in TERMINAL:
                output.append({'journal': str(path.resolve()), 'project_path': value['project_path'],
                    'state': value['state'], 'candidate_sha': value['candidate_sha']})
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            output.append({'journal': str(record_path), 'project_path': None, 'state': 'RECOVERY_REGISTRY_CORRUPT',
                'error': 'Restore retained recovery evidence; all file applications are blocked.'})
    for path in sorted((data / 'jobs').glob('*/apply_journal.json')):
        if path.resolve() in registered:
            continue
        try:
            value, _ = load(data, path)
            if value['state'] not in TERMINAL:
                output.append({'journal': str(path.resolve()), 'project_path': value['project_path'],
                    'state': value['state'], 'candidate_sha': value['candidate_sha']})
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            output.append({'journal': str(path.resolve()), 'project_path': None, 'state': 'RECOVERY_JOURNAL_CORRUPT',
                'error': 'Journal unreadable; all file applications are blocked pending evidence recovery.'})
    return output


def ensure_clear(data, root):
    if any(p['project_path'] is None or Path(p['project_path']).resolve() == Path(root).resolve() for p in pending(data)):
        raise RuntimeError('INTERRUPTED_APPLY_REQUIRES_EXPLICIT_RECOVERY')


def target(root, name):
    target = root / P.relative(name)
    P.root_path(root)
    for path in (target, *target.parents):
        if path == root:
            break
        if path.exists():
            P.regular(path)
    return target


def cleanup_directories(root, directories):
    for name, inode in reversed(list(directories.items())):
        path = target(root, name)
        if path.exists() and path.stat().st_ino == inode and not any(path.iterdir()):
            path.rmdir()
