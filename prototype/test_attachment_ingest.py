"""Synthetic ingestion policies plus a harmless real Windows Job memory-cap check.

Fake runners never start Codex or a parser. The one native-process test starts only
trusted Python with a fixed synthetic allocation; no model, network or user files.
"""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import attachment_ingest as A

PROCESS = runpy.run_path(str(ROOT / 'prototype/process.py'), run_name='attachment_ingest_memory_tests')
CONTAINMENT_KEYS = ('assigned_before_resume', 'job_queries_passed', 'all_observed_processes_stopped',
                    'job_termination_succeeded', 'job_empty_after_termination')


class AttachmentIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='attachment_ingest_test_', dir=ROOT / 'prototype')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app_root = self.root / 'application'
        (self.app_root / 'dani_agent').mkdir(parents=True)
        (self.app_root / 'dani_agent/attachment_text.py').write_bytes(b'# trusted synthetic parser, never executed\n')
        self.work_runs = self.root / 'runs'
        self.work_runs.mkdir()
        self.codex = self.root / 'codex.exe'
        self.codex.write_bytes(b'NOT AN EXECUTABLE - FAKE RUNNERS ONLY')
        self.vendor = self.root / 'vendor'
        self.vendor.mkdir()
        self.app = {
            'CODEX': str(self.codex), 'PYTHON': sys.executable, 'PYTHON_HOME': str(Path(sys.executable).parent),
            'WORK_RUNS': self.work_runs, 'overrides': A.APP['overrides'],
            'codex_env': lambda run: {'SystemRoot': r'C:\Windows', 'CODEX_HOME': 'OLD_SYNTHETIC_HOME'},
            'PROCESS': {'DIAGNOSTICS': {'execution_context': lambda: {'appcontainer': False, 'elevated': False}}},
        }
        for name, value in [('ROOT', self.app_root), ('VENDOR', self.vendor), ('APP', self.app)]:
            patcher = patch.object(A, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def result(self, value=None, **overwrites):
        return {'exit_code': 0, 'error': None, 'stdout': json.dumps(value or {'ok': True, 'text': 'Synthetic text'}),
                'containment': {**{key: True for key in CONTAINMENT_KEYS}, 'job_memory_limit_bytes': A.MEMORY_BYTES},
                **overwrites}

    def reject(self, runner, expected='ATTACHMENT_EXTRACTION_FAILED', name='notes.txt'):
        with self.assertRaises(ValueError) as caught:
            A.extract(name, b'synthetic upload', runner=runner)
        self.assertEqual(str(caught.exception), expected)

    def manifest(self, *, version='6.21.2', package='pypdf', records=None):
        if records is None:
            data = b'__version__ = "6.21.2"\n# synthetic package; never imported\n'
            (self.vendor / 'pypdf').mkdir(exist_ok=True)
            (self.vendor / 'pypdf/__init__.py').write_bytes(data)
            records = {'pypdf/__init__.py': hashlib.sha256(data).hexdigest()}
        value = {'package': package, 'version': version, 'files': records}
        (self.vendor / 'pdf_parser_manifest.json').write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_plain_exact_mxc_argv_readonly_network_disabled_and_fresh_home(self):
        captured = []
        def runner(spec, directory, **options):
            captured.append(spec)
            run = Path(spec['cwd'])
            position = spec['argv'].index('sandbox')
            policy = tomllib.loads('\n'.join(spec['argv'][2:position:2]))
            self.assertEqual(spec['argv'][0], str(self.codex))
            self.assertEqual(spec['argv'][position:], ['sandbox', '--include-managed-config', '--permission-profile',
                'attachments', '-C', str(run), '--', sys.executable, '-I', '-B', str(run / 'parser.py'),
                str(run / 'input.bin'), 'notes.txt'])
            self.assertEqual(policy, {'windows': {'sandbox': 'mxc'},
                'features': {name: False for name in ('hooks', 'plugins', 'remote_plugin', 'multi_agent', 'skill_mcp_dependency_install')},
                'permissions': {'attachments': {'filesystem': {':minimal': 'read', str(Path.home()): 'deny',
                    str(Path(sys.executable).parent): 'read', str(run): 'read',
                    str(run / 'codex_home'): 'deny', str(run / 'artifacts'): 'deny'}, 'network': {'enabled': False}}}})
            self.assertEqual(spec['env'], {'SystemRoot': r'C:\Windows', 'CODEX_HOME': str(run / 'codex_home')})
            self.assertEqual(list((run / 'codex_home').iterdir()), [])
            self.assertEqual(directory, run / 'artifacts')
            self.assertEqual(spec['kind'], 'plain')
            self.assertEqual(spec['job_memory_limit_bytes'], 1024 ** 3)
            self.assertEqual(options['timeout'], 30)
            self.assertEqual((run / 'input.bin').read_bytes(), b'synthetic upload')
            options['watch']()
            return self.result()
        self.assertEqual(A.extract('notes.txt', b'synthetic upload', runner=runner), 'Synthetic text')
        self.assertEqual(A.extract('notes.txt', b'synthetic upload', runner=runner), 'Synthetic text')
        self.assertNotEqual(captured[0]['cwd'], captured[1]['cwd'])

    def test_pdf_appends_only_hash_verified_worker_local_package(self):
        self.manifest()
        (self.vendor / 'pypdf/not_manifested.py').write_bytes(b'not copied')
        def runner(spec, directory, **options):
            target = Path(spec['cwd']) / 'parser_dependencies'
            self.assertEqual(spec['argv'][-2:], ['notes.PDF', str(target)])
            self.assertEqual([p.relative_to(target).as_posix() for p in target.rglob('*.py')], ['pypdf/__init__.py'])
            self.assertEqual((target / 'pypdf/__init__.py').read_bytes(), (self.vendor / 'pypdf/__init__.py').read_bytes())
            return self.result()
        self.assertEqual(A.extract('notes.PDF', b'%PDF-synthetic', runner=runner), 'Synthetic text')

    def test_absent_dependency_and_tampering_stop_before_runner(self):
        def never(*args, **kwargs):
            self.fail('Runner must not launch after dependency rejection')
        self.reject(never, 'ATTACHMENT_PDF_SUPPORT_NOT_INSTALLED', 'notes.pdf')
        self.manifest()
        (self.vendor / 'pypdf/__init__.py').write_bytes(b'CHANGED SYNTHETIC PACKAGE')
        self.reject(never, 'ATTACHMENT_PDF_DEPENDENCY_CHANGED', 'notes.pdf')
        for changes in ({'version': '6.0.0'}, {'package': 'other'}, {'records': {}},
                        {'records': {'pypdf/../outside.py': '0' * 64}},
                        {'records': {'pypdf\\outside.py': '0' * 64}},
                        {'records': {'pypdf/__init__.dll': '0' * 64}}):
            with self.subTest(changes=changes):
                self.manifest(**changes)
                self.reject(never, 'ATTACHMENT_PDF_DEPENDENCY_INVALID', 'notes.pdf')

    def test_malformed_dependency_json_or_missing_file_is_sanitized(self):
        def never(*args, **kwargs):
            self.fail('No parser launch')
        for value in ('{"private":"unterminated', '[]'):
            (self.vendor / 'pdf_parser_manifest.json').write_text(value)
            self.reject(never, name='notes.pdf')
        self.manifest(records={'pypdf/absent.py': '0' * 64})
        self.reject(never, name='notes.pdf')

    def test_host_context_and_inputs_refuse_before_any_runner(self):
        def never(*args, **kwargs):
            self.fail('No launch')
        for context in ({'appcontainer': True, 'elevated': False}, {'appcontainer': False, 'elevated': True}, {}):
            self.app['PROCESS']['DIAGNOSTICS']['execution_context'] = lambda value=context: value
            self.reject(never, 'ATTACHMENT_NORMAL_HOST_REQUIRED')
        for value in (b'', b'x' * (A.MAX_FILE_BYTES + 1), 'not bytes'):
            with self.assertRaisesRegex(ValueError, '^ATTACHMENT_FILE_SIZE_INVALID$'):
                A.extract('notes.txt', value, runner=never)

    def test_every_containment_boolean_and_exact_memory_cap_are_mandatory(self):
        for key in CONTAINMENT_KEYS:
            for value in (False, 1, None):
                output = self.result()
                output['containment'][key] = value
                self.reject(lambda *args, result=output, **kwargs: result, 'ATTACHMENT_EXTRACTION_CONTAINMENT_FAILED')
        for memory in (None, 0, A.MEMORY_BYTES - 1):
            output = self.result()
            output['containment']['job_memory_limit_bytes'] = memory
            self.reject(lambda *args, result=output, **kwargs: result, 'ATTACHMENT_EXTRACTION_CONTAINMENT_FAILED')
        self.reject(lambda *args, **kwargs: self.result(error='TIMEOUT'), 'ATTACHMENT_EXTRACTION_CONTAINMENT_FAILED')

    def test_output_requires_single_json_success_valid_text_and_zero_exit(self):
        for stdout in ('private diagnostic text', '{}\n{}', '[]', 'null'):
            self.reject(lambda *args, value=stdout, **kwargs: self.result(stdout=value))
        for value in ({'ok': 1, 'text': 'not true'}, {'ok': True, 'text': ''}, {'ok': True, 'text': None},
                      {'ok': True, 'text': 'x' * (A.MAX_TEXT_CHARS + 1)}):
            self.reject(lambda *args, result=value, **kwargs: self.result(result))
        self.reject(lambda *args, **kwargs: self.result(exit_code=1))
        self.assertEqual(A.extract('notes.txt', b'input', runner=lambda *a, **k:
            self.result({'ok': True, 'text': 'x' * A.MAX_TEXT_CHARS})), 'x' * A.MAX_TEXT_CHARS)

    def test_only_fixed_parser_error_codes_are_public(self):
        self.reject(lambda *a, **k: self.result({'ok': False, 'error': 'ATTACHMENT_DOCX_UNSAFE'}, exit_code=1),
                    'ATTACHMENT_DOCX_UNSAFE')
        for code in ('private contents', 'ATTACHMENT_PrivateDocumentContents', 'ATTACHMENT_UNKNOWN_PRIVATE_TOKEN'):
            self.reject(lambda *args, value=code, **kwargs: self.result({'ok': False, 'error': value}, exit_code=1))
        def private_failure(*args, **kwargs):
            raise ValueError('ATTACHMENT_private document contents')
        self.reject(private_failure)

    def test_input_and_dependency_mutations_are_detected(self):
        for filename in ('input.bin', 'parser.py'):
            def mutate(spec, directory, **options):
                (Path(spec['cwd']) / filename).write_bytes(b'changed')
                return self.result()
            self.reject(mutate, 'ATTACHMENT_EXTRACTION_INPUT_CHANGED')
        self.manifest()
        def mutate_dependency(spec, directory, **options):
            (Path(spec['cwd']) / 'parser_dependencies/pypdf/__init__.py').write_bytes(b'changed')
            options['watch']()
            return self.result()
        self.reject(mutate_dependency, 'ATTACHMENT_EXTRACTION_INPUT_CHANGED', 'notes.pdf')

    def test_created_worker_copies_are_removed_after_success_and_failures(self):
        self.assertEqual(A.extract('notes.txt', b'synthetic upload', runner=lambda *a, **k: self.result()), 'Synthetic text')
        self.assertEqual(list(self.work_runs.iterdir()), [])
        self.reject(lambda *a, **k: self.result({'ok': False, 'error': 'ATTACHMENT_DOCX_UNSAFE'}, exit_code=1),
                    'ATTACHMENT_DOCX_UNSAFE')
        self.assertEqual(list(self.work_runs.iterdir()), [])
        self.reject(lambda *a, **k: self.result(error='TIMEOUT'), 'ATTACHMENT_EXTRACTION_CONTAINMENT_FAILED')
        self.assertEqual(list(self.work_runs.iterdir()), [])
        def creation_failure(*args, **kwargs):
            raise RuntimeError('SYNTHETIC_PROCESS_CREATION_FAILED')
        self.reject(creation_failure)
        self.assertEqual(list(self.work_runs.iterdir()), [])
        self.reject(lambda *a, **k: self.fail('missing dependency must not start'),
                    'ATTACHMENT_PDF_SUPPORT_NOT_INSTALLED', 'notes.pdf')
        self.assertEqual(list(self.work_runs.iterdir()), [])

    def test_cleanup_never_removes_a_preexisting_run_when_creation_fails(self):
        identity = 'a' * 32
        existing = self.work_runs / ('attachment_' + identity)
        existing.mkdir()
        sentinel = existing / 'keep.txt'
        sentinel.write_text('PREEXISTING_SYNTHETIC_DATA')
        with patch.object(A.uuid, 'uuid4', return_value=SimpleNamespace(hex=identity)):
            self.reject(lambda *a, **k: self.fail('existing run must not start'))
        self.assertEqual(sentinel.read_text(), 'PREEXISTING_SYNTHETIC_DATA')


class AttachmentJobMemoryTests(unittest.TestCase):
    def test_invalid_caps_rejected_before_files_or_processes(self):
        for memory in (True, 0, -1, '1024', 64 * 1024 * 1024 - 1, 4 * 1024 ** 3 + 1):
            with self.assertRaisesRegex(ValueError, '^INVALID_JOB_MEMORY_LIMIT$'):
                PROCESS['run']({'job_memory_limit_bytes': memory}, ROOT / 'not-created-by-memory-test')
        self.assertFalse((ROOT / 'not-created-by-memory-test').exists())

    def test_real_job_memory_cap_refuses_large_allocation_and_cleans_up(self):
        with tempfile.TemporaryDirectory(prefix='attachment_memory_test_', dir=ROOT / 'prototype') as directory:
            cap = 256 * 1024 * 1024
            code = ('import sys\ntry:\n    value=bytearray(512*1024*1024)\n'
                    'except MemoryError:\n    print("SYNTHETIC_JOB_MEMORY_REFUSED",flush=True)\n'
                    'else:\n    print("MEMORY_CAP_NOT_ENFORCED",flush=True);sys.exit(3)\n')
            result = PROCESS['run']({'argv': [sys.executable, '-I', '-B', '-c', code],
                'cwd': directory, 'env': {'SystemRoot': r'C:\Windows'}, 'kind': 'plain',
                'job_memory_limit_bytes': cap}, directory, timeout=15)
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['stdout'].strip(), 'SYNTHETIC_JOB_MEMORY_REFUSED')
            self.assertEqual(result['containment']['job_memory_limit_bytes'], cap)
            for key in CONTAINMENT_KEYS:
                self.assertIs(result['containment'][key], True)
            self.assertIsNone(result['containment']['termination_reason'])


if __name__ == '__main__':
    unittest.main()
