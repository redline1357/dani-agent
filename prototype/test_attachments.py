"""Bounded private attachments with synthetic extractors; no parsers or model sessions."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import attachments as A


class AttachmentTests(unittest.TestCase):
    OWNER = '012345abcdef'
    OTHER = 'fedcba543210'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='attachments_', dir=ROOT / 'prototype')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'uploads'

    def upload(self, name='reference.txt', raw=b'Synthetic original', text='Synthetic reference text.'):
        return A.upload(self.root, name, raw, extractor=Mock(return_value=text))

    def bound(self, **kwargs):
        meta = self.upload(**kwargs)
        refs = A.references(self.root, [meta['id']])
        A.bind(self.root, refs, self.OWNER)
        return meta, refs

    @contextmanager
    def product_creation(self):
        from dani_agent import products as PR
        products = self.base / 'products'
        self.root = products / '.dani' / 'uploads'
        runs = self.base / 'runs'
        runs.mkdir()
        with patch.object(PR, 'PRODUCTS', products), patch.dict(PR.APP, {'WORK_RUNS': runs}), \
                patch.object(PR, 'budget', return_value={'max_steps': 1, 'settings': {}}), \
                patch.object(PR.T, 'files', return_value={'main.py': 'pass\n'}), \
                patch.object(PR.P, 'git', return_value='synthetic-revision'):
            yield PR

    def test_upload_uses_only_supplied_extractor_and_uuid_storage_names(self):
        raw, text = b'Synthetic PDF bytes', 'متن مرجع\nQuotation: "hello".'
        extractor = Mock(return_value=text)
        meta = A.upload(self.root, 'سند.pdf', raw, extractor=extractor)
        extractor.assert_called_once_with('سند.pdf', raw)
        self.assertEqual(set(meta), A.FIELDS)
        self.assertRegex(meta['id'], '^[0-9a-f]{32}$')
        folder = self.root / meta['id']
        self.assertEqual({p.name for p in folder.iterdir()}, {'original.bin', 'text.txt', 'metadata.json'})
        self.assertEqual((folder / 'original.bin').read_bytes(), raw)
        self.assertEqual(meta['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(meta['text_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(A.preview(self.root, meta['id']), {**meta, 'text': text})

    def test_path_control_reserved_and_unsupported_names_stop_before_extraction(self):
        for name in ('../secret.txt', 'folder/reference.txt', 'folder\\reference.txt', 'C:reference.txt',
                     'CON.txt', 'LPT1.md', 'COM¹.txt', 'con .txt', 'a?.txt', '.hidden.txt',
                     'name.txt ', 'name.txt.', 'a\x00.txt', 'a\n.txt', 'a\u2028.txt', 'x' * 121 + '.txt', 'program.exe'):
            extractor = Mock()
            with self.subTest(name=repr(name)), self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_'):
                A.upload(self.root, name, b'x', extractor=extractor)
            extractor.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_original_size_type_and_missing_extractor_are_bounded(self):
        for raw in (b'', b'x' * (A.MAX_FILE_BYTES + 1), 'not bytes'):
            extractor = Mock()
            with self.subTest(kind=type(raw).__name__), self.assertRaisesRegex(A.AttachmentError, 'FILE_SIZE_LIMIT'):
                A.upload(self.root, 'name.txt', raw, extractor=extractor)
            extractor.assert_not_called()
        with self.assertRaisesRegex(A.AttachmentError, 'EXTRACTOR_REQUIRED'):
            A.upload(self.root, 'name.txt', b'x', extractor=None)

    def test_extraction_failure_never_echoes_content_and_keeps_no_document(self):
        extractor = Mock(side_effect=RuntimeError('private document text C:\\private\\secret'))
        with self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_EXTRACTION_FAILED$'):
            A.upload(self.root, 'name.pdf', b'private original', extractor=extractor)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_extracted_text_and_oversized_complete_context_are_rejected_without_truncation(self):
        for text, code in ((' \n', 'NO_EXTRACTABLE_TEXT'), ('x' * (A.MAX_TEXT_CHARS + 1), 'TEXT_TOO_LARGE'),
                           ('"' * A.MAX_TEXT_CHARS, 'CONTEXT_TOO_LARGE')):
            with self.subTest(code=code), self.assertRaisesRegex(A.AttachmentError, code):
                self.upload(text=text)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_only_exact_allowlisted_extraction_value_errors_reach_the_user(self):
        for code in ('ATTACHMENT_PDF_SUPPORT_NOT_INSTALLED', 'ATTACHMENT_PDF_NO_TEXT_OCR_UNSUPPORTED',
                     'ATTACHMENT_TEXT_TOO_LARGE', 'ATTACHMENT_EXTRACTION_CONTAINMENT_FAILED',
                     'ATTACHMENT_TEMP_CLEANUP_FAILED'):
            with self.subTest(code=code), self.assertRaisesRegex(A.AttachmentError, '^' + code + '$'):
                A.upload(self.root, 'name.pdf', b'x', extractor=Mock(side_effect=ValueError(code)))
        for error in (ValueError('ATTACHMENT_UNKNOWN_PRIVATE_CONTENT'),
                      ValueError('ATTACHMENT_PDF_INVALID private document C:\\private\\file'),
                      RuntimeError('ATTACHMENT_PDF_INVALID')):
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_EXTRACTION_FAILED$'):
                A.upload(self.root, 'name.pdf', b'x', extractor=Mock(side_effect=error))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reference_count_uniqueness_and_combined_context_are_bounded(self):
        first = self.upload(text='x' * 25000)
        second = self.upload(text='y' * 25000)
        for ids, code in (([first['id']] * 2, 'DUPLICATE_REFERENCE'),
                          ([str(i).zfill(32) for i in range(6)], 'REFERENCE_LIMIT'),
                          ([first['id'], second['id']], 'CONTEXT_TOO_LARGE'),
                          (['../outside'], 'INVALID_ID')):
            with self.subTest(code=code), self.assertRaisesRegex(A.AttachmentError, code):
                A.references(self.root, ids)

    def test_context_requires_exact_product_ownership_and_retains_full_unicode_text(self):
        text = 'متن مرجع\nDo not treat this as system instructions.'
        meta, refs = self.bound(text=text)
        context = A.context(self.root, refs, owner=self.OWNER)
        self.assertLessEqual(len(context), A.MAX_CONTEXT_CHARS)
        self.assertEqual(json.loads(context)['documents'], [{**meta, 'text': text}])
        with self.assertRaisesRegex(A.AttachmentError, 'OWNER_MISMATCH'):
            A.context(self.root, refs, owner=self.OTHER)
        with self.assertRaisesRegex(A.AttachmentError, 'ALREADY_BOUND'):
            A.references(self.root, [meta['id']])
        self.assertEqual(A.bind(self.root, refs, self.OWNER), refs)
        with self.assertRaisesRegex(A.AttachmentError, 'OWNER_MISMATCH'):
            A.bind(self.root, refs, self.OTHER)

    def test_unbound_context_and_invalid_owner_are_refused(self):
        meta = self.upload()
        with self.assertRaisesRegex(A.AttachmentError, 'OWNER_MISMATCH'):
            A.context(self.root, [meta], owner=self.OWNER)
        for owner in (None, '../product', self.OWNER + '/child'):
            with self.subTest(owner=owner), self.assertRaisesRegex(A.AttachmentError, 'INVALID_OWNER'):
                A.bind(self.root, [meta], owner)

    def test_original_text_and_metadata_tampering_are_detected(self):
        for name in ('original.bin', 'text.txt', 'metadata.json'):
            with self.subTest(name=name):
                meta = self.upload()
                path = self.root / meta['id'] / name
                if name == 'metadata.json':
                    altered = {**meta, 'id': '0' * 32}
                    path.write_text(json.dumps(altered))
                else:
                    path.write_bytes(b'changed')
                with self.assertRaisesRegex(A.AttachmentError, 'HASH_MISMATCH|RECORD_MISMATCH'):
                    A.references(self.root, [meta['id']])

    def test_self_consistent_replacement_cannot_change_a_product_reference(self):
        meta, refs = self.bound()
        folder = self.root / meta['id']
        replacement = 'Different document'
        changed = {**meta, 'characters': len(replacement), 'text_sha256': hashlib.sha256(replacement.encode()).hexdigest()}
        (folder / 'text.txt').write_text(replacement, encoding='utf-8')
        (folder / 'metadata.json').write_text(json.dumps(changed))
        marker = json.loads((folder / 'owner.json').read_text())
        marker['text_sha256'] = changed['text_sha256']
        (folder / 'owner.json').write_text(json.dumps(marker))
        with self.assertRaisesRegex(A.AttachmentError, 'REFERENCE_MISMATCH'):
            A.context(self.root, refs, owner=self.OWNER)

    def test_changed_reference_metadata_is_refused_before_any_ownership_write(self):
        first, second = self.upload(), self.upload()
        forged = {**second, 'name': 'other.txt'}
        with self.assertRaisesRegex(A.AttachmentError, 'REFERENCE_MISMATCH'):
            A.bind(self.root, [first, forged], self.OWNER)
        self.assertFalse((self.root / first['id'] / 'owner.json').exists())

    def test_partial_bind_failure_rolls_back_only_markers_created_by_that_attempt(self):
        first, second, third = self.upload(), self.upload(), self.upload()
        A.bind(self.root, [first], self.OWNER)
        original_marker = (self.root / first['id'] / 'owner.json').read_bytes()
        write = A.atomic_new
        def failing(path, data):
            if path == self.root / third['id'] / 'owner.json':
                raise OSError('private failure')
            return write(path, data)
        with patch.object(A, 'atomic_new', side_effect=failing), self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_STORAGE_FAILED$'):
            A.bind(self.root, [first, second, third], self.OWNER)
        self.assertEqual((self.root / first['id'] / 'owner.json').read_bytes(), original_marker)
        self.assertFalse((self.root / second['id'] / 'owner.json').exists())
        self.assertFalse((self.root / third['id'] / 'owner.json').exists())
        self.assertEqual(A.references(self.root, [second['id'], third['id']]), [second, third])

    def test_exclusive_publication_cannot_overwrite_an_existing_marker(self):
        target = self.base / 'marker.json'
        target.write_bytes(b'existing owner')
        with self.assertRaises(OSError):
            A.atomic_new(target, b'new owner')
        self.assertEqual(target.read_bytes(), b'existing owner')
        self.assertEqual({p.name for p in self.base.iterdir()}, {'marker.json'})

    def test_failed_upload_removes_only_its_incomplete_record(self):
        kept, _ = self.bound()
        write = A.atomic_new
        def failing(path, data):
            if path.name == 'text.txt':
                raise OSError('private failure')
            return write(path, data)
        with patch.object(A, 'atomic_new', side_effect=failing), self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_STORAGE_FAILED$'):
            self.upload()
        self.assertEqual({p.name for p in self.root.iterdir()}, {kept['id']})
        self.assertTrue((self.root / kept['id'] / 'owner.json').is_file())

    def test_concurrent_products_cannot_claim_the_same_reference(self):
        from concurrent.futures import ThreadPoolExecutor
        meta = self.upload()
        gate = threading.Barrier(2)
        def claim(owner):
            gate.wait(timeout=5)
            try:
                A.bind(self.root, [meta], owner)
                return owner
            except A.AttachmentError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, (self.OWNER, self.OTHER)))
        self.assertEqual(outcomes.count('ATTACHMENT_OWNER_MISMATCH'), 1)
        winner = next(value for value in outcomes if value != 'ATTACHMENT_OWNER_MISMATCH')
        self.assertTrue(A.context(self.root, [meta], owner=winner))

    def test_create_save_failure_before_publication_leaves_upload_reusable(self):
        with self.product_creation() as PR:
            meta = self.upload()
            original = (self.root / meta['id'] / 'original.bin').read_bytes()
            with patch.object(PR, 'save', side_effect=OSError('SYNTHETIC_SAVE_FAILED')):
                with self.assertRaisesRegex(OSError, 'SYNTHETIC_SAVE_FAILED'):
                    PR.create('Synthetic', 'Synthetic requirements.', product_kind='python', attachments=[meta['id']])
            self.assertFalse((self.root / meta['id'] / 'owner.json').exists())
            self.assertEqual(A.references(self.root, [meta['id']]), [meta])
            self.assertEqual((self.root / meta['id'] / 'original.bin').read_bytes(), original)
            # A subsequent create can claim the same document; the resulting product stays protected.
            product = PR.create('Synthetic', 'Synthetic requirements.', product_kind='python', attachments=[meta['id']])
            self.assertTrue(A.context(self.root, product['attachments'], owner=product['id']))
            with self.assertRaisesRegex(A.AttachmentError, 'ALREADY_BOUND'):
                A.remove(self.root, meta['id'])

    def test_create_save_failure_after_publication_never_releases_product_evidence(self):
        with self.product_creation() as PR:
            for partial in (False, True):
                with self.subTest(partial=partial):
                    meta = self.upload()
                    save = PR.save
                    saved = []
                    def publish_then_fail(product):
                        saved.append(product)
                        if partial:
                            (PR.folder(product['id']) / 'product.json').write_text('{partial record')
                        else:
                            save(product)
                        raise OSError('SYNTHETIC_POST_PUBLICATION_FAILURE')
                    with patch.object(PR, 'save', side_effect=publish_then_fail):
                        with self.assertRaisesRegex(OSError, 'SYNTHETIC_POST_PUBLICATION_FAILURE'):
                            PR.create('Published', 'Synthetic requirements.', product_kind='python', attachments=[meta['id']])
                    owner = saved[0]['id']
                    self.assertTrue(A.context(self.root, [meta], owner=owner))
                    self.assertFalse(A._rollback_unpublished(self.root, [meta], owner))
                    with self.assertRaisesRegex(A.AttachmentError, 'ALREADY_BOUND'):
                        A.remove(self.root, meta['id'])

    def test_failed_create_rollback_cannot_release_another_products_marker(self):
        meta, refs = self.bound()
        (self.root.parent / self.OTHER).mkdir()
        with self.assertRaisesRegex(A.AttachmentError, 'OWNER_MISMATCH'):
            A._rollback_unpublished(self.root, refs, self.OTHER)
        self.assertTrue(A.context(self.root, refs, owner=self.OWNER))

    def test_draft_quotas_include_extracted_storage_and_do_not_count_owned_evidence(self):
        with patch.object(A, 'MAX_DRAFT_RECORDS', 1):
            first = self.upload()
            with self.assertRaisesRegex(A.AttachmentError, 'DRAFT_STORAGE_LIMIT'):
                self.upload()
            A.bind(self.root, [first], self.OWNER)
            self.upload()   # a bound product's reference is retained without consuming draft slots
        with patch.object(A, 'MAX_DRAFT_BYTES', 100):
            with self.assertRaisesRegex(A.AttachmentError, 'DRAFT_STORAGE_LIMIT'):
                A.upload(self.base / 'bytes', 'small.txt', b'x', extractor=lambda *args: 'x' * 101)

    def test_draft_recovery_returns_only_unused_metadata_without_private_text(self):
        owned, _ = self.bound(name='owned.txt', text='Private owned document text')
        first = self.upload(name='first.txt', raw=b'Private first original', text='Private first extracted text')
        second = self.upload(name='second.txt', raw=b'Private second original', text='Private second extracted text')
        recovered = A.drafts(self.root)
        self.assertEqual(recovered, sorted([first, second], key=lambda meta: meta['id']))
        self.assertTrue(all(set(meta) == A.FIELDS for meta in recovered))
        serialized = json.dumps(recovered)
        for private in (owned['id'], 'owned.txt', 'Private', str(self.root), 'original.bin', 'text.txt'):
            self.assertNotIn(private, serialized)
        self.assertTrue(A.context(self.root, [owned], owner=self.OWNER))

    def test_draft_recovery_needs_no_existing_storage_and_rejects_linked_ancestors(self):
        absent = self.root / 'not-created'
        self.assertEqual(A.drafts(absent), [])
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        self.assertEqual(A.drafts(self.root), [])
        actual = Path.lstat
        def linked(path):
            if path == self.base:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_nlink=1)
            return actual(path)
        with patch.object(Path, 'lstat', linked), self.assertRaisesRegex(A.AttachmentError, 'LINK_REFUSED'):
            A.drafts(absent)

    def test_recovered_drafts_can_be_reused_or_explicitly_removed(self):
        first, second = self.upload(), self.upload()
        recovered = {meta['id']: meta for meta in A.drafts(self.root)}
        refs = A.references(self.root, [first['id']])
        self.assertEqual(refs, [recovered[first['id']]])
        A.bind(self.root, refs, self.OWNER)
        self.assertTrue(A.context(self.root, refs, owner=self.OWNER))
        A.remove(self.root, second['id'])
        self.assertEqual(A.drafts(self.root), [])
        with self.assertRaisesRegex(A.AttachmentError, 'ALREADY_BOUND'):
            A.remove(self.root, first['id'])

    def test_draft_recovery_fails_closed_on_content_or_metadata_tampering(self):
        for name in ('original.bin', 'text.txt', 'metadata.json'):
            with self.subTest(name=name):
                root = self.base / name.replace('.', '_')
                meta = A.upload(root, 'reference.txt', b'original', extractor=lambda *args: 'reference text')
                path = root / meta['id'] / name
                if name == 'metadata.json':
                    path.write_text(json.dumps({**meta, 'id': '0' * 32}), encoding='utf-8')
                else:
                    path.write_bytes(b'Private tampered content')
                with self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_(HASH_MISMATCH|RECORD_MISMATCH)$'):
                    A.drafts(root)

    def test_draft_recovery_enforces_record_and_storage_caps(self):
        first, second = self.upload(), self.upload()
        with patch.object(A, 'MAX_DRAFT_RECORDS', 1):
            with self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_DRAFT_STORAGE_LIMIT$'):
                A.drafts(self.root)
            A.bind(self.root, [first], self.OWNER)
            self.assertEqual(A.drafts(self.root), [second])
        stored_bytes = sum(path.stat().st_size for path in (self.root / second['id']).iterdir())
        with patch.object(A, 'MAX_DRAFT_BYTES', stored_bytes):
            self.assertEqual(A.drafts(self.root), [second])
        with patch.object(A, 'MAX_DRAFT_BYTES', stored_bytes - 1):
            with self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_DRAFT_STORAGE_LIMIT$'):
                A.drafts(self.root)

    def test_owned_documents_cannot_be_removed_and_unbound_delete_is_exact(self):
        kept, _ = self.bound()
        removed = self.upload()
        with self.assertRaisesRegex(A.AttachmentError, 'ALREADY_BOUND'):
            A.remove(self.root, kept['id'])
        self.assertEqual(A.remove(self.root, removed['id']), {'removed': removed['id']})
        self.assertFalse((self.root / removed['id']).exists())
        self.assertTrue((self.root / kept['id'] / 'original.bin').is_file())
        for item in ('..', '../' + kept['id'], '0' * 31, kept['id'] + '/text.txt'):
            with self.subTest(item=item), self.assertRaisesRegex(A.AttachmentError, 'INVALID_ID'):
                A.remove(self.root, item)

    def test_missing_or_invalid_records_fail_with_fixed_codes_only(self):
        meta = self.upload()
        (self.root / meta['id'] / 'metadata.json').write_text('private invalid metadata {')
        for item in (meta['id'], '0' * 32):
            with self.subTest(item=item), self.assertRaisesRegex(A.AttachmentError, '^ATTACHMENT_[A-Z_]+$'):
                A.preview(self.root, item)

    def test_hardlinked_original_is_refused_without_touching_other_copy(self):
        meta = self.upload()
        original = self.root / meta['id'] / 'original.bin'
        alias = self.base / 'outside.bin'
        try:
            os.link(original, alias)
        except PermissionError:
            self.skipTest('Restricted executor forbids hardlink creation; run on normal Windows host.')
        with self.assertRaisesRegex(A.AttachmentError, 'LINK_REFUSED'):
            A.preview(self.root, meta['id'])
        with self.assertRaisesRegex(A.AttachmentError, 'LINK_REFUSED'):
            A.remove(self.root, meta['id'])
        with self.assertRaisesRegex(A.AttachmentError, 'LINK_REFUSED'):
            A.drafts(self.root)
        self.assertEqual(alias.read_bytes(), b'Synthetic original')

    def test_reparse_ancestor_is_refused_before_extraction(self):
        actual = Path.lstat
        def lstat(path):
            if path == self.base:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_nlink=1)
            return actual(path)
        extractor = Mock()
        with patch.object(Path, 'lstat', lstat), self.assertRaisesRegex(A.AttachmentError, 'LINK_REFUSED'):
            A.upload(self.root, 'reference.txt', b'x', extractor=extractor)
        extractor.assert_not_called()

    def test_empty_reference_lists_need_no_storage_or_owner(self):
        self.assertEqual(A.references(self.root, []), [])
        self.assertEqual(A.context(self.root, []), '')
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
