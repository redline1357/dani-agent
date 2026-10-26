"""Fixed, non-secret diagnostic vocabulary and read-only Windows token checks."""
import ctypes
import re
from ctypes import wintypes
from pathlib import Path

ERROR_PHRASES = {
    "MXC_HOST_SYSTEMDRIVE_MISSING": "MXC requires nonempty Unicode host SystemDrive",
    "MXC_EXECUTOR_UNAVAILABLE": "native MXC is unavailable on this executor",
    "MXC_WINDOWS_BUILD_UNAVAILABLE": "native MXC is unavailable on this Windows build",
    "MXC_LOOPBACK_POLICY_UNSUPPORTED": "MXC cannot enforce allow_local_binding=false",
    "MXC_MANAGED_LOOPBACK_POLICY_UNSUPPORTED": "MXC cannot enforce managed networking with allow_local_binding=false",
    "MXC_PREPARATION_FAILED": "failed to prepare MXC sandbox",
    "MXC_WINDOWS_DIRECTORY_UNAVAILABLE": "cannot locate the Windows directory",
    "MXC_DENY_PATHS_UNSUPPORTED": "this Windows build cannot enforce native MXC deny paths",
    "MXC_POLICY_LAUNCH_BLOCKED": "launch_blocked_by_policy",
    "MXC_POLICY_NOT_ABSOLUTE": "MXC requires an absolute policy working directory",
    "MXC_UNRESOLVED_FILESYSTEM_PATH": "MXC policy contains an unresolved symbolic filesystem path",
    "ACCESS_DENIED": "Access is denied. (os error 5)",
    "AGENTS_MD_LOAD_FAILED": "failed to load AGENTS.md instructions",
    "BOOTSTRAP_CONFIG_FAILED": "failed to load bootstrap configuration",
    "CONFIG_FAILED": "failed to load configuration",
    "QUOTA_EXHAUSTED": "subscription quota exhausted",
}


def error_codes(text):
    codes = [code for code, phrase in ERROR_PHRASES.items() if phrase.lower() in text.lower()]
    # This diagnostic has a dynamic variable-name suffix in the executable.
    if "mxc requires nonempty unicode host" in text.lower() and "systemdrive" in text.lower():
        if "MXC_HOST_SYSTEMDRIVE_MISSING" not in codes:
            codes.append("MXC_HOST_SYSTEMDRIVE_MISSING")
    return codes


def error_detail(text, cwd=None):
    """Fixed vocabulary only: no raw error messages, paths, tokens or file contents."""
    lower = text.lower().replace("\\\\", "\\")
    contexts = {
        "CREATE_PROCESS": ("createprocess", "failed to spawn", "could not spawn"),
        "CREATE_DIRECTORY": ("create_dir", "create directory", "creating directory"),
        "OPEN_FILE": ("failed to open", "could not open", "open file"),
        "READ_FILE": ("failed to read", "could not read"),
        "WRITE_FILE": ("failed to write", "could not write"),
        "CANONICALIZE_PATH": ("canonicaliz", "canonicalis"),
        "SHELL_SNAPSHOT": ("shell_snapshot", "shell snapshot"),
        "ARG0_ALIAS_SETUP": ("arg0-windows", "path aliases", "arg0 temp"),
        "SESSION_INITIALIZATION": ("initialize session", "initialise session", "create thread", "thread/start"),
        "AGENTS_MD_DISCOVERY": ("agents.md instructions", "agents_md"),
        "MXC_PREPARATION": ("failed to prepare mxc", "prepare_mxc"),
        "GIT_OPERATION": ("git repository", "git workspace", "git status"),
        "STATE_DATABASE": ("state database", "sqlite", "state_5.sqlite"),
        "OUTPUT_SCHEMA": ("output schema", "output-schema", "developer_schema.json"),
        "SANDBOXED_FILESYSTEM_RUNTIME": ("codex_core::fs_client", "sandboxed_fs", "filesystem sandbox", "fs sandbox", "fs_server"),
        "EXEC_SERVER_RUNTIME": ("codex_core::exec_server", "exec server", "exec_server"),
        "MXC_RUNTIME_LOG": ("codex_mxc_sandbox", "codex_sandboxing"),
    }
    targets = {}
    try:
        home = Path.home()
    except RuntimeError:
        home = None  # Diagnostic readers must tolerate a minimal subprocess env.
    if home is not None:
        targets.update(CODEX_AUTH=str(home / ".codex/auth.json"), CODEX_CONFIG=str(home / ".codex/config.toml"),
            CODEX_RUNTIME_TEMP=str(home / ".codex/tmp"), CODEX_SHELL_SNAPSHOTS=str(home / ".codex/shell_snapshots"))
    if cwd:
        targets.update(TOY_REPOSITORY=str(Path(cwd)), TOY_GIT=str(Path(cwd) / ".git"))
    codes = error_codes(text)
    native = sorted({int(value) for value in re.findall(r"(?:os error |winerror[ =:]*)\(?([0-9]{1,5})", lower)})
    matched = [name for name, phrases in contexts.items() if any(phrase in lower for phrase in phrases)]
    paths = [name for name, path in targets.items() if path.lower() in lower or path.replace("\\", "/").lower() in lower]
    return {"codes": codes, "native_windows_errors": native, "operation_contexts": matched,
        "referenced_path_classes": paths, "underlying_operation_identified": False}


def execution_context():
    """No elevation or token modification: inspect this process's existing token."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        values = {}
        for name, information_class in (("appcontainer", 29), ("elevated", 20)):
            value, length = wintypes.DWORD(), wintypes.DWORD()
            if not advapi.GetTokenInformation(token, information_class, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            values[name] = bool(value.value)
        return values
    finally:
        kernel.CloseHandle(token)
