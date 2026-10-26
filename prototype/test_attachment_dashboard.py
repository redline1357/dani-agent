"""Real loopback HTTP upload tests with isolated storage and fake extraction/builds; zero model calls."""
import base64
from contextlib import ExitStack
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import dashboard as D, products as PR, attachments as A, attachment_ingest as I
from dani_agent import coordinator as C, model_settings as M
from dani_agent.state import Store


class AttachmentDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='attachment_http_', dir=ROOT / 'prototype')
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to((ROOT / 'prototype').resolve()))
        self.stack = ExitStack()
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.stack.enter_context(patch.object(PR, 'PRODUCTS', self.root / 'products'))
        self.stack.enter_context(patch.dict(PR.APP, {'WORK_RUNS': self.runs}))
        self.stack.enter_context(patch.object(PR, 'hide'))
        self.stack.enter_context(patch.object(PR.P, 'git', side_effect=lambda path, *args, **kw:
            'a' * 40 if args and args[0] == 'rev-parse' else ''))
        self.stack.enter_context(patch.object(M, 'auto_settings', return_value=dict(M.DEFAULTS)))
        self.stack.enter_context(patch.object(M, 'validate', side_effect=lambda value: dict(value)))
        self.extractor = self.stack.enter_context(patch.object(I, 'extract', side_effect=lambda name, raw: raw.decode('utf-8')))
        self.stack.enter_context(patch.object(D.Dashboard, 'storage_pending', return_value=False))
        self.store = Store(self.root / 'dani_agent.sqlite3')
        self.engine = Mock(offline_only=True, host_context={'appcontainer': False, 'elevated': False})
        self.dashboard = D.Dashboard(self.store, self.engine, 'synthetic-upload-token', previews=Mock())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), None)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = D.handler_class(self.dashboard, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.stack.close()
        self.assertTrue(self.root.is_relative_to((ROOT / 'prototype').resolve()))
        self.temporary.cleanup()

    def request(self, method, route, body=None, *, raw=None, cookie=True, token=True, origin=True, host=None, headers=None):
        values = {'Host': host or '127.0.0.1:' + str(self.port)}
        if cookie:
            values['Cookie'] = 'dani=synthetic-upload-token'
        if method == 'POST':
            values['Content-Type'] = 'application/json'
            if token:
                values['X-Dani-Token'] = 'synthetic-upload-token'
            if origin:
                values['Origin'] = self.origin if origin is True else origin
            if raw is None:
                raw = json.dumps({} if body is None else body).encode('utf-8')
        values.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, route, body=raw, headers=values)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), json.loads(data)
        finally:
            connection.close()

    def upload(self, text='Synthetic reference text.', name='requirements.txt'):
        status, _, result = self.request('POST', '/api/attachments', {
            'name': name, 'data_base64': base64.b64encode(text.encode('utf-8')).decode('ascii')})
        self.assertEqual(status, 200, result)
        return result

    def create(self, upload, description=''):
        status, _, product = self.request('POST', '/api/products', {
            'name': 'Attached synthetic product', 'kind': 'python', 'description': description,
            'max_steps': 1, 'attachments': [upload['id']]})
        self.assertEqual(status, 200, product)
        return product

    def test_mutating_upload_routes_require_cookie_token_local_host_and_same_origin(self):
        body = {'name': 'safe.txt', 'data_base64': base64.b64encode(b'Synthetic').decode()}
        for override in ({'cookie': False}, {'token': False}, {'origin': False},
                         {'origin': 'https://foreign.invalid'}, {'host': 'foreign.invalid'},
                         {'headers': {'X-Dani-Token': 'wrong'}}):
            with self.subTest(override=override):
                self.assertEqual(self.request('POST', '/api/attachments', body, **override)[0], 403)
                self.assertEqual(self.request('POST', '/api/attachments/' + 'a'*32 + '/remove', **override)[0], 403)
        self.extractor.assert_not_called()
        self.assertFalse((PR.meta_root() / 'uploads').exists())
        self.assertEqual(self.store.tasks(), [])
        self.engine.tick.assert_not_called()

    def test_upload_requires_exact_json_keys_and_strict_base64_without_calling_parser(self):
        valid = {'name': 'safe.txt', 'data_base64': 'U3ludGhldGlj'}
        invalid = [[], None, {'name': 'safe.txt'}, {**valid, 'path': '../outside'},
            {**valid, 'data_base64': 1}, {**valid, 'data_base64': '!!!!'},
            {**valid, 'data_base64': 'U3lu dGhldGlj'}, {**valid, 'data_base64': 'é'},
            {'name': 42, 'data_base64': 'U3ludGhldGlj'}]
        for value in invalid:
            with self.subTest(value=value):
                status, _, response = self.request('POST', '/api/attachments', raw=json.dumps(value).encode())
                self.assertEqual(status, 400, response)
                self.assertTrue(response['error'].startswith('ATTACHMENT_'), response)
        self.assertEqual(self.request('POST', '/api/attachments', raw=b'{bad')[0], 400)
        self.extractor.assert_not_called()

    def test_request_and_decoded_file_limits_are_enforced_before_extraction(self):
        for headers in ({'Content-Length': str(D.MAX_UPLOAD_BODY + 1)}, {'Content-Length': '-1'},
                        {'Content-Length': 'bad'}, {'Transfer-Encoding': 'chunked'}):
            with self.subTest(headers=headers):
                status, _, response = self.request('POST', '/api/attachments', raw=b'{}', headers=headers)
                self.assertEqual((status, response['error']), (413, 'REQUEST_SIZE_INVALID'))
        # Five and six raw bytes have the same eight-character base64 length: the decoded-size gate matters.
        with patch.object(A, 'MAX_FILE_BYTES', 5):
            for raw in (b'', b'123456', b'1234567'):
                status, _, response = self.request('POST', '/api/attachments', {
                    'name': 'safe.txt', 'data_base64': base64.b64encode(raw).decode()})
                self.assertEqual(status, 400, response)
                self.assertIn('FILE_SIZE', response['error'])
            self.assertEqual(self.request('POST', '/api/products', raw=b'{}',
                headers={'Content-Length': str(D.MAX_BODY + 1)})[0], 413)
        self.extractor.assert_not_called()

    def test_unsupported_and_path_like_filenames_never_reach_the_extractor(self):
        for name in ('run.exe', '../secret.txt', 'C:\\private\\secret.txt', '.env', 'CON.txt', 'bad\nname.txt'):
            with self.subTest(name=name):
                status, _, response = self.request('POST', '/api/attachments', {
                    'name': name, 'data_base64': base64.b64encode(b'Synthetic').decode()})
                self.assertEqual(status, 400, response)
                self.assertTrue(response['error'].startswith('ATTACHMENT_'))
                self.assertNotIn(name, response['error'])
        self.extractor.assert_not_called()

    def test_preview_is_authorized_json_with_literal_unicode_and_removal_is_confined(self):
        text = 'خرید <script>alert(1)</script>\nSynthetic reference.'
        uploaded = self.upload(text, 'سند.txt')
        route = '/api/attachments/' + uploaded['id']
        self.assertEqual(self.request('GET', route, cookie=False)[0], 403)
        self.assertEqual(self.request('GET', route, host='foreign.invalid')[0], 403)
        status, headers, shown = self.request('GET', route)
        self.assertEqual(status, 200)
        self.assertEqual(shown['text'], text)
        self.assertEqual(shown['text_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertTrue(headers['Content-Type'].startswith('application/json'))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(set(shown), A.FIELDS | {'text'})
        outside = self.root / 'outside.txt'
        outside.write_text('KEEP')
        self.assertEqual(self.request('GET', '/api/attachments/%2e%2e')[0], 400)
        self.assertEqual(self.request('POST', '/api/attachments/%2e%2e/remove')[0], 400)
        self.assertEqual(self.request('POST', route + '/remove')[0], 200)
        self.assertFalse((PR.meta_root() / 'uploads' / uploaded['id']).exists())
        self.assertEqual(outside.read_text(), 'KEEP')
        self.assertEqual(self.request('GET', route)[0], 400)

    def test_attachment_only_creation_binds_ownership_and_exact_pending_build_digest(self):
        uploaded = self.upload()
        product = self.create(uploaded)
        self.assertEqual(product['attachments'], [uploaded])
        self.assertIn('attached reference documents', product['description'])
        self.assertEqual(product['state'], 'awaiting_approval')
        task = self.store.get(product['build_task'])
        self.assertEqual(task['state'], 'awaiting_approval')
        self.assertEqual(task['payload']['attachments_sha256'], PR.attachments_digest(product))
        self.assertEqual(A.context(PR.meta_root() / 'uploads', [uploaded], owner=product['id']),
                         PR.reference_context(product))
        self.assertEqual(len(self.store.tasks()), 1)
        self.engine.tick.assert_not_called()
        before = {p.name: p.read_bytes() for p in (PR.meta_root() / 'uploads' / uploaded['id']).iterdir()}
        status, _, response = self.request('POST', '/api/attachments/' + uploaded['id'] + '/remove')
        self.assertEqual((status, response['error']), (400, 'ATTACHMENT_ALREADY_BOUND'))
        self.assertEqual({p.name: p.read_bytes() for p in (PR.meta_root() / 'uploads' / uploaded['id']).iterdir()}, before)

    def test_unused_upload_listing_is_authenticated_metadata_only_and_supports_reuse_and_removal(self):
        self.assertEqual(self.request('GET', '/api/attachments', cookie=False)[0], 403)
        self.assertEqual(self.request('GET', '/api/attachments', host='foreign.invalid')[0], 403)
        self.assertEqual(self.request('GET', '/api/attachments')[2], {'files': []})
        owned = self.upload('Synthetic owned reference.', 'owned.txt')
        self.create(owned)
        reusable = self.upload('Synthetic reference to reuse.', 'reuse.txt')
        removable = self.upload('Synthetic reference to remove.', 'remove.txt')
        status, headers, listing = self.request('GET', '/api/attachments')
        self.assertEqual(status, 200, listing)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(set(listing), {'files'})
        self.assertEqual({item['id'] for item in listing['files']}, {reusable['id'], removable['id']})
        self.assertTrue(all(set(item) == A.FIELDS for item in listing['files']))
        self.assertNotIn('Synthetic reference', json.dumps(listing))
        by_id = {item['id']: item for item in listing['files']}
        self.assertEqual(by_id[reusable['id']], reusable)
        product = self.create(by_id[reusable['id']])
        self.assertEqual(product['attachments'], [reusable])
        self.assertEqual(self.request('POST', '/api/attachments/' + by_id[removable['id']]['id'] + '/remove')[0], 200)
        self.assertEqual(self.request('GET', '/api/attachments')[2], {'files': []})
        self.assertEqual(self.request('GET', '/api/attachments/' + reusable['id'])[0], 200)
        self.assertEqual(self.request('GET', '/api/attachments/' + owned['id'])[0], 200)
        self.assertFalse((PR.meta_root() / 'uploads' / removable['id']).exists())
        self.assertEqual(len(self.store.tasks()), 2)
        self.engine.tick.assert_not_called()

    def test_creation_rejects_invalid_attachment_lists_without_a_draft_task(self):
        uploaded = self.upload()
        for refs in ('not a list', [uploaded['id'], uploaded['id']], ['0'*32], ['../outside']):
            with self.subTest(refs=refs):
                status, _, response = self.request('POST', '/api/products', {
                    'name': 'Synthetic', 'description': '', 'kind': 'python', 'attachments': refs})
                self.assertEqual(status, 400, response)
                self.assertTrue(response['error'].startswith('ATTACHMENT_'))
                self.assertEqual(self.store.tasks(), [])
        self.assertFalse((PR.meta_root() / 'uploads' / uploaded['id'] / 'owner.json').exists())

    def test_synthetic_worker_passes_approved_attachment_digest_to_build_without_running_it(self):
        product = self.create(self.upload())
        task_id = product['build_task']
        self.store.approve(task_id, 'synthetic-source')
        self.assertEqual(self.store.claim('synthetic-source')['id'], task_id)
        job = self.root / 'jobs' / task_id
        job.mkdir(parents=True)
        with patch.object(C, 'DATA', self.root), patch.object(C, 'fingerprint', return_value='synthetic-source'), \
                patch.object(PR, 'build', return_value={'verdict':'PASS', 'reason':'SYNTHETIC_BUILD_NOT_EXECUTED',
                    'agent_sessions': {'codex':0, 'claude':0}}) as build:
            self.assertEqual(C.worker(self.store.path, task_id, job), 0)
            self.assertEqual(build.call_args.args, (product['id'],))
            self.assertEqual(build.call_args.kwargs['approved_attachments_sha256'], PR.attachments_digest(product))
        self.assertEqual(json.loads((job / 'result.json').read_text())['agent_sessions'], {'codex':0, 'claude':0})

    def test_storage_recovery_and_parser_failure_fail_closed_without_private_error_details(self):
        with patch.object(self.dashboard, 'storage_pending', return_value=True):
            status, _, response = self.request('POST', '/api/attachments', {
                'name': 'safe.txt', 'data_base64': 'U3ludGhldGlj'})
            self.assertEqual((status, response['error']), (400, 'WORK_FOLDER_RECOVERY_PENDING'))
        self.extractor.assert_not_called()
        self.extractor.side_effect = RuntimeError('private document secret C:\\private\\file')
        status, _, response = self.request('POST', '/api/attachments', {
            'name': 'safe.txt', 'data_base64': 'U3ludGhldGlj'})
        self.assertEqual((status, response['error']), (400, 'ATTACHMENT_EXTRACTION_FAILED'))
        self.assertEqual(list((PR.meta_root() / 'uploads').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
