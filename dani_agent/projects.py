"""Read-only project discovery. Git plumbing only; no imports, scripts or hooks."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tomllib

from .test_completion import SUPPORTED

def git_binary():
    """The Git the user confirmed on the Connect page; shared by every Git call and refreshed on re-confirmation."""
    from .coordinator import APP
    return APP['GIT']


MAX_FILES, MAX_BYTES = 3000, 32 * 1024 * 1024
PROTECTED = {'.git', '.gitconfig', '.git-credentials', '.claude', '.codex', '.agents', '.aws', '.ssh', '.mcp.json', 'agents.md',
    'agents.override.md', 'claude.md', '.npmrc', '.pypirc', '.netrc', '.gitattributes', '.gitmodules'}
# Quick helper commands get a generous hang guard only; nothing on the product path stops for time (2026-10-23).
HELPER_SECONDS = 300
CONFIG = {'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock',
    'pyproject.toml', 'poetry.lock', 'uv.lock', 'requirements.txt', 'tsconfig.json', 'next.config.js',
    'next.config.mjs', 'next.config.ts'}


def validate_config(text):
    # Only inert local metadata is accepted. No hooks, include, filter, pager,
    # credential, command, worktree or alternate-repository configuration.
    allowed = {'core': {'repositoryformatversion', 'filemode', 'bare', 'logallrefupdates',
        'symlinks', 'ignorecase', 'autocrlf', 'safecrlf', 'quotepath', 'longpaths', 'precomposeunicode'},
        'remote': {'url', 'fetch', 'pushurl'}, 'branch': {'remote', 'merge', 'rebase'},
        'user': {'name', 'email', 'signingkey'}, 'init': {'defaultbranch'},
        # Set by `git worktree`; it makes Git also read config.worktree, which is validated too.
        'extensions': {'worktreeconfig'}}
    try:
        proc = subprocess.run([git_binary(), 'config', '--no-includes', '--file', '-', '--null', '--list'],
            input=text.encode('utf-8'), env=git_environment(), capture_output=True, timeout=HELPER_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode or len(proc.stdout)>131072:
            raise ValueError('config parse')
        for record in proc.stdout.split(b'\0'):
            if not record:
                continue
            key=record.split(b'\n',1)[0].decode('utf-8')
            family=key.split('.',1)[0]
            variable=key.rsplit('.',1)[-1]
            if family not in allowed or variable not in allowed[family]:
                raise ValueError('not allowlisted')
            if family in ('core','user','init','extensions') and key != family+'.'+variable:
                raise ValueError('unsupported subsection')
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise RuntimeError('PROJECT_GIT_CONFIG_REQUIRES_REVIEW') from None


def bounded_read(path, maximum=2 * 1024 * 1024):
    with Path(path).open('rb') as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise RuntimeError('PROJECT_WORKING_FILE_SIZE_LIMIT')
    return value


def relative(value):
    if type(value) is not str or not value or '\\' in value or ':' in value or any(ord(c) < 32 for c in value):
        raise ValueError('INVALID_PROJECT_RELATIVE_PATH')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') or p.endswith(('.', ' '))
            or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', p) for p in value.split('/')):
        raise ValueError('INVALID_PROJECT_RELATIVE_PATH')
    return path.as_posix()


def confidential(name):
    parts = relative(name).casefold().split('/')
    return any(p in PROTECTED or p.startswith('.env') or p.endswith(('.pem', '.key', '.pfx', '.p12'))
        or p in ('credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519') for p in parts)


def is_test_file(test_id, name):
    """Files each supported driver actually runs (unittest's default test*.py; Dani's node:test list)."""
    base = PurePosixPath(name).name
    if test_id == 'python-unittest':
        return base.startswith('test') and base.endswith('.py')
    if test_id == 'node-test':
        return base.endswith(('.test.js', '.test.mjs', '.test.cjs'))
    return False


def secret_file(name):
    """A tracked file that likely holds real credentials and blocks onboarding.

    Committed templates such as .env.example stay excluded from the snapshot
    (confidential), but do not block the project."""
    return any((p.startswith('.env') and not p.endswith(('.example', '.sample', '.template', '.dist')))
        or p.endswith(('.pem', '.key', '.pfx', '.p12')) or p in ('credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519')
        for p in relative(name).casefold().split('/'))


def protected(name):
    return confidential(name) or PurePosixPath(name).name.casefold() in CONFIG or PurePosixPath(name).name.startswith('.git')


def regular(path):
    info = path.lstat()
    if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or path.is_symlink() or (path.is_file() and info.st_nlink != 1):
        raise RuntimeError('PROJECT_LINK_OR_REPARSE_POINT')


def root_path(value):
    path = Path(value).absolute()
    for parent in (path, *path.parents):
        regular(parent)
    path = path.resolve()
    if not path.is_dir() or path == Path(path.anchor) or path == Path.home():
        raise ValueError('SELECT_A_PROJECT_DIRECTORY')
    return path


def git_environment():
    # Environment is deliberately independent of all provider authentication.
    environment = {k: v for k, v in os.environ.items() if k.casefold() in
        ('systemroot', 'windir', 'systemdrive', 'path', 'pathext', 'temp', 'tmp')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='0')
    return environment


def git(path, *args, binary=False, maximum=MAX_BYTES, input_data=None):
    environment=git_environment()
    command = [git_binary(), '--no-pager', '--no-optional-locks', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=' + os.devnull,
        '-c', 'commit.gpgsign=false', '-c', 'core.quotePath=false',
        '-c', 'user.name=Dani Agent', '-c', 'user.email=dani-agent@example.invalid', '-C', str(path), *args]
    proc = subprocess.run(command, env=environment, input=input_data, capture_output=True, timeout=HELPER_SECONDS,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    if proc.returncode:
        if b'dubious ownership' in proc.stderr:
            # e.g. .git created by an elevated process: owned by Administrators, not this user.
            raise RuntimeError('PROJECT_GIT_OWNED_BY_ANOTHER_ACCOUNT_FIX_OWNERSHIP')
        raise RuntimeError('PROJECT_GIT_COMMAND_FAILED')  # Never leak remote URLs/config/stderr.
    if len(proc.stdout) > maximum:
        raise RuntimeError('PROJECT_GIT_OUTPUT_LIMIT')
    return proc.stdout if binary else proc.stdout.decode('utf-8').strip()


def tracked(root, sha):
    rows, names, total = [], set(), 0
    for entry in git(root, 'ls-tree', '-r', '-l', '-z', sha, binary=True).split(b'\0'):
        if not entry:
            continue
        header, raw = entry.split(b'\t', 1)
        mode, kind, oid, size = header.split()
        name = relative(raw.decode('utf-8'))
        if name.casefold() in names or mode not in (b'100644', b'100755') or kind != b'blob':
            raise RuntimeError('PROJECT_LINK_SUBMODULE_OR_CASE_COLLISION')
        names.add(name.casefold())
        total += int(size)
        if len(names) > MAX_FILES or total > MAX_BYTES or int(size) > 2 * 1024 * 1024:
            raise RuntimeError('PROJECT_SNAPSHOT_SIZE_LIMIT')
        rows.append({'path': name, 'oid': oid.decode(), 'size': int(size)})
    return rows


def read_blob(root, oid):
    if not re.fullmatch('[0-9a-f]{40,64}', oid):
        raise ValueError('INVALID_GIT_OBJECT')
    return git(root, 'cat-file', 'blob', oid, binary=True)


def read_blobs(root, rows):
    raw = git(root, 'cat-file', '--batch', binary=True, maximum=MAX_BYTES + len(rows) * 128,
        input_data=('\n'.join(r['oid'] for r in rows) + '\n').encode())
    offset, output = 0, {}
    for row in rows:
        end = raw.index(b'\n', offset)
        oid, kind, size = raw[offset:end].split()
        size = int(size)
        if oid.decode() != row['oid'] or kind != b'blob' or size != row['size']:
            raise RuntimeError('PROJECT_OBJECT_BATCH_MISMATCH')
        output[row['path']] = raw[end + 1:end + 1 + size]
        offset = end + size + 2
    return output


def discover(value):
    root = root_path(value)
    dotgit = root / '.git'
    summary = {'path': str(root), 'name': root.name, 'git': False, 'eligible': False,
        'warnings': [], 'frameworks': [], 'languages': [], 'package_manager': None, 'test_options': []}
    if not dotgit.exists():
        summary['warnings'].append('Git repository required. No repository was initialized.')
        return summary
    regular(dotgit)
    if not dotgit.is_dir():
        summary['warnings'].append('Existing linked-worktree or submodule registration is not supported yet.')
        return summary
    # Read-only status is safe only with fsmonitor disabled. Refuse config indirection.
    for name in ('config', 'HEAD', 'objects', 'refs'):
        if (dotgit / name).exists():
            regular(dotgit / name)
    config = bounded_read(dotgit / 'config', 65536).decode('utf-8')
    validate_config(config)
    if (dotgit / 'config.worktree').exists():
        regular(dotgit / 'config.worktree')
        validate_config(bounded_read(dotgit / 'config.worktree', 65536).decode('utf-8'))
    for name in ('objects/info/alternates', 'info/attributes'):
        p = dotgit / name
        if p.exists():
            for parent in (p, *p.parents):
                if parent == dotgit:
                    break
                regular(parent)
            if p.read_bytes().strip():
                raise RuntimeError('PROJECT_GIT_INDIRECTION_REQUIRES_REVIEW')
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise RuntimeError('SELECT_GIT_TOP_LEVEL')
    sha = git(root, 'rev-parse', '--verify', 'HEAD')
    changes = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=all', binary=True)
    files = tracked(root, sha)
    available = {r['path']: r for r in files if not confidential(r['path'])}
    # Refuse secrets from Git objects; do not read their content into the application.
    excluded = [r['path'] for r in files if confidential(r['path'])]
    secret_names = [n for n in excluded if secret_file(n)]
    if secret_names:
        summary['warnings'].append('Tracked sensitive files require removal from the selected baseline before execution.')
    names = list(available)
    working_hashes = {}
    working_bytes = 0
    for name in names:
        path = root / name
        for parent in (path, *path.parents):
            if parent == root:
                break
            if not parent.exists():
                raise RuntimeError('SPARSE_OR_MISSING_TRACKED_PATH_UNSUPPORTED')
            regular(parent)
        content = bounded_read(path)
        working_bytes += len(content)
        if working_bytes > MAX_BYTES:
            raise RuntimeError('PROJECT_WORKING_FILE_SIZE_LIMIT')
        working_hashes[name] = hashlib.sha256(content).hexdigest()
    if any(n.endswith('.py') for n in names):
        summary['languages'].append('Python')
        summary['frameworks'].append('Python')
        if 'pyproject.toml' in available:
            try:
                py = tomllib.loads(read_blob(root, available['pyproject.toml']['oid']).decode())
                deps = ' '.join(py.get('project', {}).get('dependencies', []))
                summary['frameworks'] += [n for n in ('django', 'fastapi', 'flask', 'pytest') if n in deps.lower()]
            except (ValueError, TypeError):
                summary['warnings'].append('Could not parse pyproject.toml metadata.')
        summary['test_options'].append({'id': 'python-unittest', 'display': 'Python unittest discovery (isolated, sandboxed)', 'script': None})
        if any('pytest' in n for n in names) or 'pyproject.toml' in available:
            summary['test_options'].append({'id': 'python-pytest', 'display': 'pytest (no plugin autoload or conftest; must already be installed)', 'script': None})
    if 'package.json' in available:
        package = json.loads(read_blob(root, available['package.json']['oid']))
        deps = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
        summary['languages'] += ['TypeScript' if any(n.endswith(('.ts', '.tsx')) for n in names) or 'typescript' in deps else 'JavaScript']
        summary['frameworks'] += [label for dep, label in (('next', 'Next.js'), ('react', 'React')) if dep in deps]
        locks = [label for name, label in (('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('package-lock.json', 'npm'), ('bun.lock', 'bun')) if name in available]
        summary['package_manager'] = locks[0] if len(locks) == 1 else 'npm' if not locks else 'ambiguous'
        script = package.get('scripts', {}).get('test')
        if type(script) is str and 0 < len(script) <= 2000:
            summary['test_options'].append({'id': 'npm-test', 'display': 'npm --ignore-scripts --offline run test', 'script': script})
        summary['test_options'].append({'id': 'node-test', 'display': 'Node built-in tests: tracked *.test.mjs/js/cjs files', 'script': None})
    for option in summary['test_options']:
        # 0 means the task itself must add the first test file (see workspaces.prepare).
        option['baseline_test_files'] = sum(is_test_file(option['id'], n) for n in names)
    summary.update(git=True, original_sha=sha, dirty=bool(changes), changed_entry_count=len(changes.split(b'\0')) - 1,
        files=files, excluded=excluded, file_count=len(files), snapshot_bytes=sum(f['size'] for f in files),
        structure=names[:150], working_hashes=working_hashes, protected_files=[n for n in names if protected(n)])
    if changes:
        summary['warnings'].append('Uncommitted or untracked changes detected. Execution is blocked; nothing will be discarded.')
    if excluded:
        summary['warnings'].append('Agent/settings/credential configuration is excluded from the isolated snapshot.')
    if not any(o['id'] in SUPPORTED for o in summary['test_options']):
        summary['warnings'].append('No supported test driver (Python unittest or Node built-in node:test) was detected.')
    summary['eligible'] = not changes and not secret_names and any(o['id'] in SUPPORTED for o in summary['test_options'])
    # baseline_test_files is derived from 'files'; keep it out so digests of earlier scans stay comparable.
    digest_options = [{k: v for k, v in o.items() if k != 'baseline_test_files'} for o in summary['test_options']]
    summary['digest'] = hashlib.sha256(json.dumps({**{k: summary[k] for k in ('path', 'original_sha', 'files', 'excluded', 'working_hashes')},
        'test_options': digest_options}, sort_keys=True).encode()).hexdigest()
    return summary
