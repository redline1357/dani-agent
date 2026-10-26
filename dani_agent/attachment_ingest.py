"""Model-free document text extraction inside native MXC and a bounded Windows Job."""
import hashlib
import json
from pathlib import Path
import stat
import uuid

from .coordinator import APP, ROOT
from .attachment_text import ERROR_CODES, MAX_FILE_BYTES, MAX_TEXT_CHARS

EXTRACTION_SECONDS = 30
MEMORY_BYTES = 1024 ** 3
VENDOR = ROOT / 'vendor'


def ordinary(path, *, directory=False):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('ATTACHMENT_LINKED_PATH_REFUSED')
        if part == path and not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError('ATTACHMENT_ORDINARY_FILE_REQUIRED')
        if part == path and directory and not stat.S_ISDIR(info.st_mode):
            raise ValueError('ATTACHMENT_ORDINARY_DIRECTORY_REQUIRED')
    return path


def parser_dependencies(target):
    """Copy only the pinned, hash-verified parser files; never import a user plugin."""
    manifest_path = VENDOR / 'pdf_parser_manifest.json'
    if not manifest_path.is_file():
        return False
    ordinary(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('package') != 'pypdf' or manifest.get('version') != '6.21.2':
        raise ValueError('ATTACHMENT_PDF_DEPENDENCY_INVALID')
    records = manifest.get('files')
    if type(records) is not dict or not 1 <= len(records) <= 200:
        raise ValueError('ATTACHMENT_PDF_DEPENDENCY_INVALID')
    target.mkdir()
    total = 0
    for name, expected in records.items():
        path = Path(name)
        if (type(name) is not str or '\\' in name or ':' in name or path.is_absolute()
                or '..' in path.parts or not name.startswith('pypdf/') or path.suffix != '.py'):
            raise ValueError('ATTACHMENT_PDF_DEPENDENCY_INVALID')
        original = ordinary(VENDOR / path)
        total += original.stat().st_size
        if total > 8 * 1024 * 1024:
            raise ValueError('ATTACHMENT_PDF_DEPENDENCY_INVALID')
        data = original.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('ATTACHMENT_PDF_DEPENDENCY_CHANGED')
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return True


def extract(name, raw, *, runner=None):
    """No model request, credentials, network, project config or unrestricted parser fallback."""
    if type(name) is not str or type(raw) is not bytes or not 1 <= len(raw) <= MAX_FILE_BYTES:
        raise ValueError('ATTACHMENT_FILE_SIZE_INVALID')
    if APP['PROCESS']['DIAGNOSTICS']['execution_context']() != {'appcontainer': False, 'elevated': False}:
        raise ValueError('ATTACHMENT_NORMAL_HOST_REQUIRED')
    if not Path(APP['CODEX']).is_file() or not APP['WORK_RUNS'].is_dir():
        raise ValueError('ATTACHMENT_SANDBOX_SETUP_REQUIRED')
    run = APP['WORK_RUNS'] / ('attachment_' + uuid.uuid4().hex)
    created = False
    try:
        ordinary(APP['WORK_RUNS'], directory=True)
        run.mkdir()
        created = True
        (run / 'artifacts').mkdir()
        (run / 'control/empty_git').mkdir(parents=True)
        (run / 'codex_home').mkdir()
        source = ordinary(ROOT / 'dani_agent/attachment_text.py')
        (run / 'parser.py').write_bytes(source.read_bytes())
        (run / 'input.bin').write_bytes(raw)
        dependency_root = run / 'parser_dependencies'
        if name.lower().endswith('.pdf') and not parser_dependencies(dependency_root):
            raise ValueError('ATTACHMENT_PDF_SUPPORT_NOT_INSTALLED')
        if not dependency_root.exists():
            dependency_root.mkdir()
        fs = {':minimal': 'read', str(Path.home()): 'deny', APP['PYTHON_HOME']: 'read', str(run): 'read',
              str(run / 'codex_home'): 'deny', str(run / 'artifacts'): 'deny'}
        policy = {'windows.sandbox': 'mxc', 'features.hooks': False, 'features.plugins': False,
                  'features.remote_plugin': False, 'features.multi_agent': False,
                  'features.skill_mcp_dependency_install': False,
                  'permissions.attachments.filesystem': fs, 'permissions.attachments.network.enabled': False}
        argv = [APP['CODEX'], *APP['overrides'](policy), 'sandbox', '--include-managed-config',
                '--permission-profile', 'attachments', '-C', str(run), '--', APP['PYTHON'], '-I', '-B',
                str(run / 'parser.py'), str(run / 'input.bin'), name]
        if name.lower().endswith('.pdf'):
            argv.append(str(dependency_root))
        environment = APP['codex_env'](run)
        environment['CODEX_HOME'] = str(run / 'codex_home')
        before = {p.relative_to(run).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in [run / 'parser.py', run / 'input.bin', *dependency_root.rglob('*.py')]}
        def unchanged():
            for relative, digest in before.items():
                if hashlib.sha256(ordinary(run / relative).read_bytes()).hexdigest() != digest:
                    raise ValueError('ATTACHMENT_EXTRACTION_INPUT_CHANGED')
        out = (runner or APP['PROCESS']['run'])({'argv': argv, 'cwd': str(run), 'env': environment,
            'kind': 'plain', 'job_memory_limit_bytes': MEMORY_BYTES}, run / 'artifacts',
            timeout=EXTRACTION_SECONDS, watch=unchanged)
        unchanged()
        containment = out.get('containment', {})
        if (out.get('error') or containment.get('job_memory_limit_bytes') != MEMORY_BYTES or any(
                containment.get(key) is not True for key in ('assigned_before_resume', 'job_queries_passed',
                'all_observed_processes_stopped', 'job_termination_succeeded', 'job_empty_after_termination'))):
            raise ValueError('ATTACHMENT_EXTRACTION_CONTAINMENT_FAILED')
        value = json.loads(out.get('stdout', ''))
        if value.get('ok') is not True:
            code = value.get('error', '')
            if type(code) is str and code in ERROR_CODES:
                raise ValueError(code)
            raise ValueError('ATTACHMENT_EXTRACTION_FAILED')
        text = value.get('text')
        if out.get('exit_code') != 0 or type(text) is not str or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ValueError('ATTACHMENT_EXTRACTION_FAILED')
        return text
    except ValueError as exc:
        if str(exc) in ERROR_CODES:
            raise
        raise ValueError('ATTACHMENT_EXTRACTION_FAILED') from None
    except Exception:
        raise ValueError('ATTACHMENT_EXTRACTION_FAILED') from None
    finally:
        # Uploaded content is retained only in its approved upload store. Temporary parser copies and
        # stdout contain private text, so remove this newly-created worker tree after Job cleanup.
        if created:
            try:
                root = ordinary(APP['WORK_RUNS'], directory=True)
                if run.parent != root or not run.name.startswith('attachment_'):
                    raise ValueError('unexpected temporary path')
                ordinary(run, directory=True)
                items, pending = [], [run]
                while pending:
                    directory = ordinary(pending.pop(), directory=True)
                    for path in directory.iterdir():
                        ordinary(path, directory=path.is_dir())
                        items.append(path)
                        if path.is_dir():
                            pending.append(path)
                for path in sorted(items, key=lambda p: len(p.parts), reverse=True):
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
                run.rmdir()
            except Exception:
                raise ValueError('ATTACHMENT_TEMP_CLEANUP_FAILED') from None
