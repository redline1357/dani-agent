"""First-run Connect page: find and confirm the user's own Claude Code and Codex CLIs, check their
subscription sign-ins, choose and create the private work folder, and report the models each plan lists.

Nothing here reads or stores credentials: sign-in stays inside each CLI. Version, sign-in and model-list
checks are model-free; the setup checks that use a session are approved tasks (state.POLICIES) run by the worker.
"""
import ctypes
import datetime
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys

from . import coordinator as C, projects as P
from .coordinator import APP, DATA, PROTO, atomic_json, reload_app

TOOLS = ('codex', 'claude', 'python', 'git')
SYSTEM = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
PROVIDER_OVERRIDES = ('ANTHROPIC_', 'CLAUDE_CODE_USE_', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_HOME',
                      'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN')
CLAUDE_ALIASES = ('sonnet', 'opus', 'haiku')   # Claude Code's documented --model aliases
INSTALL = {
    'codex': 'Install the Codex app (Microsoft Store / openai.com/codex) and sign in with ChatGPT.',
    'claude': 'Install Claude Code with the native Windows installer (docs.claude.com/en/docs/claude-code/setup).',
    'python': 'Install Python 3.12 from python.org for all users (outside your user folder, e.g. C:\\Program Files).',
    'git': 'Install Git for Windows (git-scm.com).'}


def child_env():
    # Without SystemDrive, PowerShell writes "%SystemDrive%\ProgramData\..." caches into its working folder.
    return {**APP['OLD_GATE']['child_env'](), 'SystemDrive': os.environ.get('SystemDrive') or SYSTEM.drive}


def quiet(argv, timeout=15, environment=None):
    try:
        return subprocess.run(argv, env=environment or child_env(), cwd=str(DATA), capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None


def version_of(path):
    p = quiet([str(path), '--version'])
    lines = p.stdout.strip().splitlines() if p and p.returncode == 0 else []
    return lines[0].strip()[:120] if lines else None


def under_profile(path):
    return Path(path).resolve().is_relative_to(Path.home().resolve())


def candidates(name):
    found = []
    if name == 'codex':
        p = quiet([str(SYSTEM / 'WindowsPowerShell/v1.0/powershell.exe'), '-NoProfile', '-NonInteractive', '-Command',
                   '(Get-AppxPackage -Name OpenAI.Codex).InstallLocation'], timeout=30)
        found += [Path(line.strip()) / 'app/resources/codex.exe' for line in (p.stdout.splitlines() if p else []) if line.strip()]
        npm = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules/@openai/codex'
        found += sorted(npm.glob('vendor/*/codex/codex.exe')) if npm.is_dir() else []
    elif name == 'claude':
        found.append(Path.home() / '.local/bin/claude.exe')
    elif name == 'python':
        found += [Path(sys.base_prefix) / 'python.exe']
        for root in (Path(os.environ.get('ProgramFiles', r'C:\Program Files')), Path('C:/')):
            found += sorted(root.glob('Python3*/python.exe'), reverse=True)
    elif name == 'git':
        found.append(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git/cmd/git.exe')
    which = shutil.which(name + '.exe')
    found += [Path(which)] if which else []
    unique = []
    for path in found:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path.is_file() and path.suffix.lower() == '.exe' and path not in unique:
            unique.append(path)
    return unique


BROAD = {r'c:\windows', r'c:\windows\system32', r'c:\program files', r'c:\program files (x86)', r'c:\programdata',
         r'c:\users'}


def own_folder(name, path):
    """The sandbox may read the folder of a confirmed Python or Git, so it must hold only that program."""
    folder = path.parent
    if folder == Path(folder.anchor) or str(folder).casefold() in BROAD:
        return False
    return (folder / 'Lib' / 'os.py').is_file() if name == 'python' else (folder.parent / 'mingw64').is_dir() or (folder / 'git-core').is_dir() or (folder.parent / 'usr').is_dir()


def problem(name, path, version):
    """Why this program cannot be used, or None."""
    if not version:
        return 'Does not run or report a version.'
    if name == 'codex' and not version.startswith('codex-cli '):
        return 'Not the Codex CLI.'
    if name == 'claude' and '(Claude Code)' not in version:
        return 'Not Claude Code.'
    if name == 'python':
        try:
            major, minor = (int(x) for x in version.split()[1].split('.')[:2])
        except (IndexError, ValueError):
            return 'Not Python.'
        if (major, minor) < (3, 12):
            return 'Python 3.12 or newer is required.'
        if under_profile(path):
            return 'Installed inside your user folder, which the sandbox cannot read. Install it for all users.'
        if not own_folder(name, path):
            return 'Pick python.exe from its own installation folder (the one that contains Lib).'
    if name == 'git' and not version.startswith('git version'):
        return 'Not Git.'
    if name == 'git' and under_profile(path):
        return 'Installed inside your user folder, which the sandbox cannot read. Install Git for Windows for all users.'
    if name == 'git' and not own_folder(name, path):
        return 'Pick git.exe from its Git for Windows installation folder.'
    return None


def discover():
    out = {}
    for name in TOOLS:
        rows = []
        for path in candidates(name):
            version = version_of(path)
            rows.append({'path': str(path), 'version': version, 'problem': problem(name, path, version)})
        out[name] = rows
    return out


def confirm(name, path):
    """The user picked this program. Record its exact path, version and hash; any later change stops runs."""
    if name not in TOOLS or type(path) is not str or not path:
        raise ValueError('INVALID_PROGRAM')
    path = Path(path)
    if not path.is_absolute() or path.suffix.lower() != '.exe' or not path.is_file():
        raise ValueError('PROGRAM_MUST_BE_AN_EXISTING_EXE')
    path = path.resolve()
    version = version_of(path)
    reason = problem(name, path, version)
    if reason:
        raise ValueError(reason)
    setup = APP['load_setup']()
    tools = dict(setup.get('tools', {}))
    tools[name] = {'path': str(path), 'version': version, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    atomic_json(APP['SETUP_FILE'], {**setup, 'tools': tools})
    reload_app()
    if name in ('codex', 'claude'):
        from .model_settings import import_legacy_evidence
        import_legacy_evidence()
    return tools[name]


def store_package(path):
    """(name, publisher id) of the Microsoft Store package a ...\\WindowsApps\\<package>\\app\\resources\\codex.exe
    belongs to, or None. Package folders are Name_Version_Arch_ResourceId_PublisherId; Windows checks that every
    version of a package is signed by the same publisher."""
    parts = Path(path).parts
    if (len(parts) < 5 or parts[-5].casefold() != 'windowsapps'
            or [part.casefold() for part in parts[-3:]] != ['app', 'resources', 'codex.exe']):
        return None
    fields = parts[-4].split('_')
    if len(fields) != 5 or not fields[0] or not fields[4].isalnum():
        return None
    return fields[0].casefold(), fields[4].casefold(), Path(*parts[:-4])


def same_installation(name, old, new):
    """The new program is an update of the one the user confirmed: the same file (Claude Code and npm update in
    place), or for Codex the same Store package from the same publisher in the same WindowsApps folder."""
    if str(Path(old)).casefold() == str(Path(new)).casefold():
        return True
    return name == 'codex' and store_package(old) is not None and store_package(old) == store_package(new)


def program_changed(name):
    """The confirmed program is gone or reports another version (it updated itself)."""
    tool = APP['TOOLS'].get(name)
    return bool(tool) and (not Path(tool['path']).is_file() or version_of(tool['path']) != tool['version'])


def programs_changed():
    return any(program_changed(name) for name in ('codex', 'claude'))


def follow_updates():
    """Codex (through the Microsoft Store) and Claude Code update themselves; the confirmed path then disappears or
    reports a new version, and every build would stop until the user confirmed again by hand. An update of the
    same installation that passes the same checks as the Connect page is confirmed by itself and recorded; anything
    else still waits for the Connect page. Returns the names confirmed again."""
    updated = []
    for name in ('codex', 'claude'):
        if not program_changed(name):
            continue
        old = APP['TOOLS'][name]
        options = ([Path(old['path'])] if Path(old['path']).is_file() else []) + candidates(name)
        for path in options:
            if not same_installation(name, old['path'], path):
                continue
            version = version_of(path)
            if problem(name, path, version):
                continue
            confirm(name, str(path))
            setup = APP['load_setup']()
            history = list(setup.get('updated_by_itself', []))[-19:] + [{'program': name, 'from': old['version'],
                'to': version, 'from_path': old['path'], 'to_path': str(path.resolve()), 'at': datetime.datetime.now(datetime.timezone.utc).isoformat()}]
            atomic_json(APP['SETUP_FILE'], {**setup, 'updated_by_itself': history})
            reload_app()
            updated.append(name)
            break
    return updated


def sign_in_status():
    """Model-free. Keeps only the sign-in method and plan, never email, organisation or tokens."""
    environment = child_env()
    codex = claude = None
    if APP['CODEX']:
        p = quiet([APP['CODEX'], 'login', 'status'], environment=environment)
        text = (p.stdout + p.stderr).lower() if p else ''
        method = 'chatgpt' if 'chatgpt' in text else 'api_key' if 'api key' in text else 'none'
        codex = {'signed_in': bool(p) and p.returncode == 0 and method != 'none', 'method': method,
                 'ok': bool(p) and p.returncode == 0 and method == 'chatgpt'}
    if APP['CLAUDE']:
        p = quiet([APP['CLAUDE'], 'auth', 'status', '--json'], environment=environment)
        try:
            value = json.loads(p.stdout) if p else {}
        except ValueError:
            value = {}
        value = value if type(value) is dict else {}
        plan = value.get('subscriptionType') if type(value.get('subscriptionType')) is str else None
        claude = {'signed_in': value.get('loggedIn') is True, 'method': value.get('authMethod'), 'plan': plan,
                  'ok': value.get('loggedIn') is True and value.get('authMethod') == 'claude.ai' and bool(plan)}
    overrides = sorted(k for k, v in os.environ.items() if v and (k.upper().startswith(PROVIDER_OVERRIDES[:2])
        or k.upper() in PROVIDER_OVERRIDES[2:]))
    in_windows = [name for name in overrides if windows_setting(name)]
    return {'codex': codex, 'claude': claude, 'environment_overrides': overrides,
            'environment_in_windows': in_windows, 'environment_inherited': [n for n in overrides if n not in in_windows]}


def windows_setting(name):
    """Set in Windows' own environment settings (user or system), rather than by the program that started us."""
    import winreg
    for hive, key in ((winreg.HKEY_CURRENT_USER, 'Environment'),
                      (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
        try:
            with winreg.OpenKey(hive, key) as handle:
                winreg.QueryValueEx(handle, name)
                return True
        except OSError:
            continue
    return False


def start_login(name):
    """Opens the CLI's own sign-in in a new console window; the user finishes it in the browser."""
    argv = {'codex': [APP['CODEX'], 'login'], 'claude': [APP['CLAUDE'], 'auth', 'login', '--claudeai']}.get(name)
    if not argv or not argv[0]:
        raise ValueError('CONFIRM_THE_PROGRAM_FIRST')
    subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    return {'started': True}


# ---- private work folder ---------------------------------------------------------------------
def work_root():
    return APP['WORK_RUNS'].parent


def user_sid():
    p = quiet([str(SYSTEM / 'whoami.exe'), '/user', '/fo', 'csv', '/nh'])
    sid = p.stdout.strip().split(',')[-1].strip('"') if p and p.returncode == 0 else ''
    if not sid.startswith('S-1-5-'):
        raise RuntimeError('CURRENT_USER_SID_UNAVAILABLE')
    return sid


ACL_SCRIPT = ('$a = Get-Acl -LiteralPath $env:DANI_WORK_ROOT; $s = [Security.Principal.SecurityIdentifier]; '
    '@{protected = $a.AreAccessRulesProtected; owner = $a.GetOwner($s).Value; rules = @($a.GetAccessRules($true, $true, $s) | '
    'ForEach-Object { @{sid = $_.IdentityReference.Value; allow = ($_.AccessControlType -eq "Allow"); inherited = $_.IsInherited} })} '
    '| ConvertTo-Json -Compress -Depth 4')


def work_root_status(root=None):
    root = root or work_root()
    if not root.exists():
        return {'path': str(root), 'exists': False, 'private': False}
    info = root.lstat()
    if info.st_file_attributes & 0x400 or not root.is_dir():   # FILE_ATTRIBUTE_REPARSE_POINT
        return {'path': str(root), 'exists': True, 'private': False, 'reason': 'NOT_A_PLAIN_FOLDER'}
    p = quiet([str(SYSTEM / 'WindowsPowerShell/v1.0/powershell.exe'), '-NoProfile', '-NonInteractive', '-Command', ACL_SCRIPT],
              timeout=30, environment={**child_env(), 'DANI_WORK_ROOT': str(root)})
    try:
        acl = json.loads(p.stdout)
        sid = user_sid()
        rules = acl['rules']
        private = (acl['protected'] is True and acl['owner'] == sid and any(r['sid'] == sid for r in rules)
                   and all(r['allow'] is True and r['inherited'] is False and r['sid'] in (sid, 'S-1-5-18') for r in rules))
    except (AttributeError, TypeError, ValueError, KeyError, RuntimeError):
        return {'path': str(root), 'exists': True, 'private': False, 'reason': 'PERMISSIONS_UNREADABLE'}
    ready = private and all((root / name).is_dir() for name in ('runs', 'products'))
    return {'path': str(root), 'exists': True, 'private': private, 'ready': ready,
            'reason': None if private else 'FOLDER_EXISTS_BUT_OTHER_ACCOUNTS_HAVE_ACCESS'}


def lock_work_root(root=None):
    """Create the folder atomically with a protected ACL: full control for this user and SYSTEM only.

    An existing folder is never re-permissioned: if it is not already private the user must decide."""
    root = root or work_root()
    if not root.exists():
        sddl = 'O:' + user_sid() + 'D:P(A;OICI;FA;;;' + user_sid() + ')(A;OICI;FA;;;SY)'
        descriptor = ctypes.c_void_p()
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(ctypes.c_wchar_p(sddl), 1,
                ctypes.byref(descriptor), None):
            raise RuntimeError('SECURITY_DESCRIPTOR_FAILED')
        class Attributes(ctypes.Structure):
            _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p), ('bInheritHandle', wintypes.BOOL)]
        try:
            attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
            if not kernel.CreateDirectoryW(ctypes.c_wchar_p(str(root)), ctypes.byref(attributes)):
                raise RuntimeError('WORK_FOLDER_CREATE_FAILED_WINERROR_' + str(ctypes.get_last_error()))
        finally:
            kernel.LocalFree(descriptor)
    status = work_root_status(root)
    if not status['private']:
        raise RuntimeError('WORK_FOLDER_NOT_PRIVATE_' + str(status.get('reason')))
    for name in ('runs', 'products'):
        (root / name).mkdir(exist_ok=True)   # inherits the protected ACL
    return work_root_status(root)


def check_work_root(value):
    """A folder the sandbox can use and that only this user can open; refuse anything else with a reason."""
    if type(value) is not str or not value.strip():
        raise ValueError('TYPE_A_FOLDER_PATH')
    root = Path(value.strip())
    if not root.is_absolute() or not root.drive or root.drive.startswith('\\\\'):
        raise ValueError('USE_A_FULL_PATH_ON_A_LOCAL_DRIVE_E_G_D_DANIAGENT')
    if root == Path(root.anchor):
        raise ValueError('PICK_A_FOLDER_NOT_A_WHOLE_DRIVE')
    for parent in (root, *root.parents):   # no junction or link on the way in
        if parent.exists() or parent.is_symlink():
            P.regular(parent)
    root = root.resolve()
    if under_profile(root):
        raise ValueError('MUST_BE_OUTSIDE_YOUR_USER_FOLDER_THE_SANDBOX_CANNOT_ENTER_IT')
    windows = Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve()
    for forbidden in (PROTO.parent.resolve(), windows, Path(windows.anchor) / 'Program Files', Path(windows.anchor) / 'Program Files (x86)',
                      Path(windows.anchor) / 'ProgramData'):
        if root.is_relative_to(forbidden):
            raise ValueError('NOT_INSIDE_DANI_AGENT_WINDOWS_OR_PROGRAM_FOLDERS')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    drive = kernel.GetDriveTypeW(ctypes.c_wchar_p(root.anchor))
    if drive == 1:   # DRIVE_NO_ROOT_DIR
        raise ValueError('THAT_DRIVE_DOES_NOT_EXIST_ON_THIS_PC')
    if drive != 3:   # DRIVE_FIXED
        raise ValueError('MUST_BE_ON_A_LOCAL_DRIVE_NOT_USB_OR_NETWORK')
    name = ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumeInformationW(ctypes.c_wchar_p(root.anchor), None, 0, None, None, None, name, 64) or name.value != 'NTFS':
        raise ValueError('MUST_BE_ON_AN_NTFS_DRIVE')
    if root.exists() and any(root.iterdir()) and not work_root_status(root).get('private'):
        raise ValueError('PICK_AN_EMPTY_OR_NEW_FOLDER')
    return root


def move_work_root(value, store):
    """Use another private work folder; existing products and runs move into it (no build may be running)."""
    if any(t['state'] in ('awaiting_approval', 'queued', 'running') for t in store.tasks()
           if t['kind'] not in ('codex_diagnostic',)):
        raise ValueError('WAIT_UNTIL_NO_BUILD_OR_CHECK_IS_WAITING_OR_RUNNING')
    if recover_work_root_move() == 'INCOMPLETE':
        raise RuntimeError('WORK_FOLDER_MOVE_RECOVERY_REQUIRED')
    root = check_work_root(value)
    old = work_root().resolve() if work_root().exists() else work_root()
    if root == old:
        return work_root_status()
    if root.is_relative_to(old) or old.is_relative_to(root):
        raise ValueError('THE_NEW_FOLDER_CANNOT_CONTAIN_OR_BE_INSIDE_THE_OLD_ONE')
    lock_work_root(root)
    moves = []
    for name in ('runs', 'products'):
        source = old / name
        for child in sorted(source.iterdir()) if source.is_dir() else []:
            P.regular(child)
            if (root / name / child.name).exists():   # every clash is found before anything moves
                raise RuntimeError('WORK_FOLDER_ALREADY_HAS_' + name.upper() + '_' + child.name)
            moves.append((child, root / name / child.name))
    # The journal says what is moving where until the new folder is saved in the settings, so a failure or a crash
    # at any point is finished or undone, never left half done (CODEX_REVIEW_3 F6; recover_work_root_move()).
    atomic_json(move_journal(), {'old': str(old), 'new': str(root), 'same_drive': same_drive(old, root),
                               'moves': [[str(source), str(target)] for source, target in moves]})
    if same_drive(old, root):
        done = []
        try:   # a rename on one drive moves each item whole, or not at all
            for source, target in moves:
                os.rename(source, target)
                done.append((source, target))
            publish_work_root(root)
        except BaseException:
            for source, target in reversed(done):   # put back what moved, so nothing is split between folders
                try:
                    os.rename(target, source)
                except OSError:
                    pass
            if all(Path(source).exists() for source, _ in moves):
                move_journal().unlink(missing_ok=True)   # fully undone; otherwise the next start finishes undoing
            raise
    else:
        copied = []
        try:   # another drive: copy everything and check it before anything is deleted or switched
            for source, target in moves:
                copied.append(target)
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=True)
                else:
                    shutil.copy2(source, target)
                if file_sizes(source) != file_sizes(target):
                    raise RuntimeError('WORK_FOLDER_COPY_DID_NOT_MATCH_' + source.name)
            publish_work_root(root)
        except BaseException:
            for target in copied:   # the originals were never touched
                try:
                    remove_tree(target)
                except OSError:
                    pass
            if not any(target.exists() for target in copied):
                move_journal().unlink(missing_ok=True)   # otherwise recovery still needs the copied targets
            raise
    # Publication is durable now. A reload failure must not undo files while setup.json still names the new
    # folder. Leave its journal for recovery and keep the published data intact.
    activate_work_root(root)
    left = []
    if not same_drive(old, root):
        for source, _ in moves:   # the new folder is complete and in use; the old copies are only leftovers now
            try:
                remove_tree(source)
            except OSError:
                left.append(str(source))
    move_journal().unlink(missing_ok=True)
    return {**work_root_status(), 'left_in_old_folder': left}


def move_journal():
    """Next to the settings it protects (tests point SETUP_FILE at a temporary folder)."""
    return Path(APP['SETUP_FILE']).parent / 'work_root_move.json'


def publish_work_root(root):
    """Switch Dani Agent to the new work folder: the one step that makes a move final."""
    setup = APP['load_setup']()
    atomic_json(APP['SETUP_FILE'], {**setup, 'work_root': str(root)})


def activate_work_root(root):
    """Refresh runtime state after durable publication; failures retain the move's recovery journal."""
    from . import products as PR
    reload_app()
    PR.PRODUCTS = Path(root) / 'products'


def recover_work_root_move():
    """At start, before anything reads the products: finish or undo a work-folder move that a crash interrupted.
    If the settings already name the new folder, the move is final: leftover copies in the old folder go. If not,
    everything goes back where it was: renamed items are renamed back; copies on another drive are removed (their
    originals were never touched). Returns what was done, or None."""
    if not move_journal().exists():
        return None
    journal = json.loads(move_journal().read_text(encoding='utf-8'))
    published = Path(str(APP['load_setup']().get('work_root') or '')).resolve() == Path(journal['new']).resolve()
    if published:
        activate_work_root(Path(journal['new']))   # failure keeps both data and journal for a later recovery
    for source, target in journal['moves']:
        source, target = Path(source), Path(target)
        try:
            if published and not journal['same_drive'] and target.exists() and source.exists():
                remove_tree(source)
            elif not published and journal['same_drive'] and target.exists() and not source.exists():
                os.rename(target, source)
            elif not published and not journal['same_drive'] and target.exists() and source.exists():
                remove_tree(target)
        except OSError:
            return 'INCOMPLETE'   # the journal stays; the next start tries again
    move_journal().unlink()
    return 'FINISHED' if published else 'UNDONE'


def same_drive(a, b):
    return Path(a).anchor.casefold() == Path(b).anchor.casefold()


def file_sizes(path):
    path = Path(path)
    if path.is_file():
        return {'': path.stat().st_size}
    return {p.relative_to(path).as_posix(): p.stat().st_size for p in path.rglob('*') if p.is_file()}


def remove_tree(path):
    """Delete a folder (or file), including Git's read-only object files."""
    path = Path(path)
    def writable(function, name, _):
        os.chmod(name, stat.S_IWRITE)
        function(name)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onexc=writable)
    elif path.exists():
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


# ---- models ------------------------------------------------------------------------------------
def models():
    """What each plan offers, read from the CLIs themselves (no test prompts), and what a session refused."""
    from . import model_settings as M
    value = {role: {'available': M.ranked_models(role), 'refused': M.refused_models(role)} for role in ('codex', 'claude')}
    value['claude']['labels'] = M.CLAUDE_LABELS
    value['claude']['needs_credits'] = M.claude_credit_models()
    return value


def self_test_current():
    try:
        runpy.run_path(str(PROTO / 'host_launcher.py'))['verify_offline_tests']()
        return True
    except (RuntimeError, OSError, ValueError):
        return False


def check_state(tasks, kinds, passed):
    """passed / queued / running / failed / waiting (prerequisites not met yet)."""
    if passed:
        return 'passed'
    latest = next((t for t in tasks if t['kind'] in kinds), None)   # tasks() is newest first
    if latest and latest['state'] in ('awaiting_approval', 'queued', 'running'):
        return 'running' if latest['state'] == 'running' else 'queued'
    if latest and latest['state'] in ('failed', 'blocked', 'cancelled', 'interrupted'):
        return 'failed'
    return 'waiting'


_profile_checks = {}


def reviewer_profile_state():
    """'current', 'missing', or 'stale': Claude Code, its child environment or the reviewer command changed
    since the zero-tool startup check, so every run copied from it would stop. Only a new approved check fixes it."""
    try:
        source = C.reviewer_source()
        gate = source / 'artifacts/reviewer_startup_gate.json'
        key = (str(source), APP['CLAUDE'], APP['TOOLS'].get('claude', {}).get('sha256'), gate.stat().st_mtime_ns)
    except RuntimeError:
        return 'missing'
    except OSError:
        return 'stale'
    if key not in _profile_checks:   # hashing Claude Code is slow; the key changes whenever it could matter
        try:
            APP['verified_startup_gate'](source)
            _profile_checks[key] = 'current'
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            _profile_checks[key] = 'stale'
    return _profile_checks[key]


def create_reviewer_profile():
    """Worker only, approved: one zero-tool Claude startup that this installation's runs are copied from."""
    runpy.run_path(str(PROTO / 'host_launcher.py'))['verify_offline_tests']()
    report = runpy.run_path(str(PROTO / 'reviewer_startup.py'))['execute'](profile=True)
    if report.get('verdict') == 'PASS':
        run = Path(report['run'])
        atomic_json(DATA / 'reviewer_profile.json', {'run': run.name,
            'gate_sha256': hashlib.sha256((run / 'artifacts/reviewer_startup_gate.json').read_bytes()).hexdigest()})
        return {'verdict': 'PASS', 'reason': 'REVIEWER_PROFILE_VERIFIED_ZERO_TOOLS', 'run': str(run)}
    return {'verdict': 'FAIL', 'reason': report.get('error') or 'REVIEWER_STARTUP_CHECK_FAILED', 'run': report.get('run')}


def probe_claude(model, directory):
    """Worker only, approved: one tiny Claude session with the reviewer's zero-tool profile and `model`."""
    from . import model_settings as M
    M.model_name(model)
    runpy.run_path(str(PROTO / 'host_launcher.py'))['verify_offline_tests']()
    run = runpy.run_path(str(PROTO / 'prepare_live_run.py'))['prepare'](C.reviewer_source())
    APP['preflight'](run)
    APP['verified_startup_gate'](run)
    argv = APP['commands'](run)['claude']
    argv[argv.index('--model') + 1] = model
    out = APP['PROCESS']['run']({'argv': argv, 'cwd': str(run / 'review_snapshot'), 'env': APP['env'](run),
        'kind': 'claude-review', 'startup_inventory_verified': True,
        'prompt': 'Model availability check only. Reply with exactly: DANI_MODEL_OK'}, directory, timeout=60)
    finals = [e for e in out.get('events', []) if e.get('type') == 'result' and e.get('subtype') == 'success' and not e.get('is_error')]
    passed = (out.get('exit_code') == 0 and not out.get('error') and len(finals) == 1
              and out.get('initialization') == {'tools': [], 'mcp_servers_count': 0}
              and 'DANI_MODEL_OK' in str(finals[0].get('result')))
    M.record_probe('claude', model, passed, str(run))
    return {'verdict': 'PASS' if passed else 'FAIL', 'run': str(run), 'model': model,
            'reason': 'MODEL_RESPONDED' if passed else 'MODEL_PROBE_FAILED_' + str(out.get('error') or out.get('exit_code'))}


# ---- overview ----------------------------------------------------------------------------------
def status(store, login=None, folder=None):
    from . import model_settings as M
    from .coordinator import integration_ready
    tools = {name: {**APP['TOOLS'][name], 'ok': Path(APP['TOOLS'][name]['path']).is_file()} if name in APP['TOOLS'] else None
             for name in TOOLS}
    self_test = self_test_current()
    reviewer = reviewer_profile_state()
    try:
        automatic = M.auto_settings()
    except ValueError:
        automatic = None
    tasks = store.tasks()
    host = integration_ready(store)
    checks = {'self_test': check_state(tasks, ('offline_tests',), self_test),
              'claude_startup': check_state(tasks, ('reviewer_profile',), reviewer == 'current'),
              'codex_sandbox': check_state(tasks, ('codex_preflight', 'codex_diagnostic'), host)}
    login = login or {}
    # What is still open, in the page's order. The page tells "connect your accounts" (the first three need the
    # user) from "Dani Agent is checking itself" (the three checks it runs by itself after an update).
    parts = (('programs', all(tools[n] and tools[n]['ok'] for n in TOOLS)),
             ('sign_in', (login.get('codex') or {}).get('ok') is True and (login.get('claude') or {}).get('ok') is True
                         and not login.get('environment_overrides')),
             ('folder', (folder or {}).get('ready') is True), ('self_test', bool(self_test)),
             ('claude_startup', reviewer == 'current'), ('codex_sandbox', bool(host)), ('models', bool(automatic)))
    missing = [name for name, good in parts if not good]
    return {'tools': tools, 'install': INSTALL, 'login': login, 'folder': folder, 'self_test': self_test,
            'reviewer_profile': reviewer, 'codex_host_check': host, 'checks': checks, 'models': models(),
            'everyday': {role: automatic[role + '_model'] for role in ('codex', 'claude')} if automatic else None,
            'ready': not missing, 'missing': missing}
