"""Synthetic attachment-parser checks; PDF-library integration is explicit, never skipped.

Run optional real-library checks only when the approved vendor copy exists:
  python -I -B prototype/test_attachment_text.py --pdf-integration ABSOLUTE_VENDOR
The normal suite checks dependency refusal and parser policies with labelled stubs;
those tests are not evidence that PDF-library extraction has been exercised.
"""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import attachment_text as A

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def document(body):
    return ('<?xml version="1.0"?><w:document xmlns:w="' + W + '"><w:body>' + body
            + '</w:body></w:document>').encode()


def docx(body='<w:p><w:r><w:t>Hello</w:t></w:r></w:p>', extras=(), *, xml=None,
         compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=compression) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="' + REL + '"><Relationship Id="rId1" Type="'
                         + OFFICE_REL + 'officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', document(body) if xml is None else xml)
        for name, value in extras:
            archive.writestr(name, value)
    return buffer.getvalue()


def rel(kind, target, mode='External'):
    return ('<Relationships xmlns="' + REL + '"><Relationship Id="rId2" Type="' + OFFICE_REL + kind
            + '" Target="' + target + '" TargetMode="' + mode + '"/></Relationships>')


class AttachmentTextCase(unittest.TestCase):
    def rejected(self, name, raw, code):
        with self.assertRaises(ValueError) as caught:
            A.extract_text(name, raw)
        self.assertEqual(str(caught.exception), 'ATTACHMENT_' + code)

    def test_all_text_extensions_and_supported_encodings(self):
        text = 'Æble,漢字\nCount\t2\r\n'
        for extension in ('.txt', '.MD', '.csv', '.json'):
            for encoding in ('utf-8', 'utf-8-sig', 'utf-16'):
                with self.subTest(extension=extension, encoding=encoding):
                    self.assertEqual(A.extract_text('input' + extension, text.encode(encoding)), text)
        self.assertEqual(A.extract_text('big.txt', b'\xfe\xff' + text.encode('utf-16-be')), text)

    def test_limits_are_explicit_and_text_never_truncated(self):
        self.assertEqual(A.extract_text('a.txt', b'a' * A.MAX_TEXT_CHARS), 'a' * A.MAX_TEXT_CHARS)
        self.rejected('a.txt', b'a' * (A.MAX_TEXT_CHARS + 1), 'TEXT_TOO_LARGE')
        self.rejected('a.txt', b'a' * (A.MAX_FILE_BYTES + 1), 'FILE_TOO_LARGE')

    def test_empty_unsupported_and_invalid_types(self):
        for raw in (b'', b' \t\r\n'):
            self.rejected('a.txt', raw, 'EMPTY')
        self.rejected('a.exe', b'Hello', 'TYPE_UNSUPPORTED')
        self.rejected('a.doc', b'Hello', 'TYPE_UNSUPPORTED')
        self.rejected('a.txt', 'not bytes', 'INVALID')
        self.rejected(None, b'Hello', 'INVALID')

    def test_rejects_binary_controls_and_unsupported_encoding(self):
        for raw in (b'Hello\0', b'Hello\x1b', b'Hello\x7f', 'Hi\x85'.encode()):
            self.rejected('a.txt', raw, 'BINARY_TEXT')
        for raw in (b'\xffbad', 'hello'.encode('utf-32'), b'\xff\xfe\x01'):
            self.rejected('a.txt', raw, 'ENCODING_UNSUPPORTED')

    def test_docx_paragraph_tabs_breaks_table_and_unicode(self):
        body = '<w:p><w:r><w:t>Æble</w:t><w:tab/><w:t>2</w:t><w:br/><w:t>ready</w:t></w:r></w:p>'
        body += '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        self.assertEqual(A.extract_text('a.docx', docx(body)), 'Æble\t2\nready\nCell')

    def test_docx_hyperlink_label_is_data_and_image_bytes_ignored(self):
        body = '<w:p><w:hyperlink><w:r><w:t>Website</w:t></w:r></w:hyperlink></w:p>'
        data = docx(body, extras=[('word/_rels/document.xml.rels', rel('hyperlink', 'https://example.invalid/?secret=private')),
                                 ('word/media/image.png', b'not interpreted or opened')])
        self.assertEqual(A.extract_text('a.docx', data), 'Website')

    def test_docx_internal_relationships_do_not_access_filesystem(self):
        data = docx(extras=[('word/_rels/document.xml.rels', rel('image', 'media/a.png', 'Internal')),
                           ('word/media/a.png', b'anything')])
        self.assertEqual(A.extract_text('a.docx', data), 'Hello')
        self.rejected('a.docx', docx(extras=[('word/_rels/document.xml.rels', rel('image', '../../private.txt', 'Internal'))]), 'DOCX_UNSAFE')
        self.rejected('a.docx', docx(extras=[('word/_rels/document.xml.rels', rel('image', 'https://example.invalid/a.png'))]), 'DOCX_UNSAFE')

    def test_docx_forbids_macros_ole_and_external_templates(self):
        for name in ('word/vbaProject.bin', 'word/embeddings/a.bin', 'word/activeX/a.xml', 'word/run.exe'):
            with self.subTest(name=name):
                self.rejected('a.docx', docx(extras=[(name, b'not executed')]), 'DOCX_UNSAFE')
        for kind in ('attachedTemplate', 'oleObject', 'package'):
            self.rejected('a.docx', docx(extras=[('word/_rels/document.xml.rels', rel(kind, 'https://example.invalid/'))]), 'DOCX_UNSAFE')
        self.rejected('a.docx', docx('<w:p><w:object/></w:p>'), 'DOCX_UNSAFE')

    def test_docx_rejects_traversal_absolute_links_and_duplicates(self):
        for name in ('../outside.txt', '/outside.txt', 'C:/outside.txt', 'word\\x.txt', 'word/./x.txt'):
            # ZipInfo otherwise normalizes backslashes while making the fixture on Windows.
            entry = zipfile.ZipInfo('placeholder')
            entry.filename = name
            self.rejected('a.docx', docx(extras=[(entry, b'x')]), 'DOCX_UNSAFE')
        link = zipfile.ZipInfo('word/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.rejected('a.docx', docx(extras=[(link, b'../outside')]), 'DOCX_UNSAFE')
        self.rejected('a.docx', docx(extras=[('WORD/DOCUMENT.XML', b'x')]), 'DOCX_UNSAFE')

    def test_docx_rejects_dtd_entities_in_all_xml_including_utf16(self):
        attack = '<!DOCTYPE document [<!ENTITY x "sensitive">]><document>&x;</document>'
        for encoding in ('utf-8', 'utf-16'):
            self.rejected('a.docx', docx(extras=[('docProps/core.xml', attack.encode(encoding))]), 'DOCX_UNSAFE')

    def test_docx_member_count_size_total_and_ratio_limits(self):
        self.rejected('a.docx', docx(extras=[('word/a' + str(i), b'x') for i in range(A.MAX_ZIP_MEMBERS)]), 'DOCX_LIMIT')
        self.rejected('a.docx', docx(extras=[('word/big', b'x' * (A.MAX_ZIP_MEMBER_BYTES + 1))]), 'DOCX_LIMIT')
        self.rejected('a.docx', docx(extras=[('word/bomb', b'x' * 100000)], compression=zipfile.ZIP_DEFLATED), 'DOCX_LIMIT')
        # Lower only the aggregate limit to exercise it without creating a >5MiB upload.
        with patch.object(A, 'MAX_ZIP_TOTAL_BYTES', 1000):
            self.rejected('a.docx', docx(extras=[('word/more', b'z' * 800)]), 'DOCX_LIMIT')

    def test_docx_depth_and_complete_text_limit(self):
        self.rejected('a.docx', docx('<w:p>' * 130 + '</w:p>' * 130), 'DOCX_LIMIT')
        text = 'x' * A.MAX_TEXT_CHARS
        self.assertEqual(A.extract_text('a.docx', docx('<w:p><w:r><w:t>' + text + '</w:t></w:r></w:p>')), text)
        self.rejected('a.docx', docx('<w:p><w:r><w:t>' + text + 'x</w:t></w:r></w:p>'), 'TEXT_TOO_LARGE')

    def test_docx_invalid_archives_and_xml_do_not_leak_parser_details(self):
        for raw in (b'private malformed zip', docx(xml=b'<private-secret>'), docx(xml=b'<root/>')):
            self.rejected('a.docx', raw, 'DOCX_INVALID')

    def test_pdf_absent_wrong_version_and_bad_header_are_explicit(self):
        with patch.object(A.importlib, 'import_module', side_effect=ModuleNotFoundError('private path')):
            self.rejected('a.pdf', b'%PDF-1.7\n', 'PDF_SUPPORT_NOT_INSTALLED')
        with patch.object(A.importlib, 'import_module', return_value=SimpleNamespace(__version__='0.0')):
            self.rejected('a.pdf', b'%PDF-1.7\n', 'PDF_VERSION_UNSUPPORTED')
        self.rejected('a.pdf', b'not a PDF', 'PDF_INVALID')

    def test_pdf_policies_with_stub_are_not_real_library_integration(self):
        class LimitReached(Exception):
            pass

        def library(parts=('hello',), encrypted=False, error=None):
            pages = [SimpleNamespace(get_contents=lambda: None, extract_text=lambda value=p: value) for p in parts]
            def reader(*args, **kwargs):
                if error:
                    raise error
                return SimpleNamespace(is_encrypted=encrypted, pages=pages)
            def configuration(**kwargs):
                self.assertIsNone(kwargs['jbig2dec_binary'])
                self.assertLessEqual(kwargs['zlib_maximum_output_length'], A.MAX_PDF_STREAM_BYTES)
                return nullcontext()
            return SimpleNamespace(PdfReader=reader, apply_configuration=configuration, errors=SimpleNamespace(LimitReachedError=LimitReached))

        with patch.object(A, '_pdf_module', return_value=library(('one', 'two'))):
            self.assertEqual(A.extract_text('a.pdf', b'%PDF-1.7\n'), 'one\ntwo')
        for lib, code in [(library(encrypted=True), 'PDF_ENCRYPTED_UNSUPPORTED'),
                          (library([''] * 101), 'PDF_TOO_MANY_PAGES'),
                          (library(['']), 'PDF_NO_TEXT_OCR_UNSUPPORTED'),
                          (library(['x' * (A.MAX_TEXT_CHARS + 1)]), 'TEXT_TOO_LARGE'),
                          (library(error=LimitReached('private')), 'PDF_LIMIT'),
                          (library(error=RuntimeError('private contents')), 'PDF_INVALID')]:
            with patch.object(A, '_pdf_module', return_value=lib):
                self.rejected('a.pdf', b'%PDF-1.7\n', code)

    def test_cli_returns_one_json_without_trace_or_path_leak(self):
        with tempfile.TemporaryDirectory(prefix='attachment_text_test_', dir=ROOT / 'prototype') as directory:
            path = Path(directory) / 'private-name.txt'
            path.write_bytes(b'hello')
            for args, expected, status in [([str(path), 'notes.txt'], {'ok': True, 'text': 'hello'}, 0),
                    ([str(path), 'private.exe'], {'ok': False, 'error': 'ATTACHMENT_TYPE_UNSUPPORTED'}, 1),
                    ([str(path / 'missing'), 'private.txt'], {'ok': False, 'error': 'ATTACHMENT_INPUT_UNREADABLE'}, 1),
                    ([], {'ok': False, 'error': 'ATTACHMENT_CLI_USAGE'}, 1)]:
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(A.main(args), status)
                self.assertEqual(json.loads(stdout.getvalue()), expected)
                self.assertEqual(len(stdout.getvalue().splitlines()), 1)
                self.assertEqual(stderr.getvalue(), '')


def synthetic_pdf(text='Hello PDF', pages=1, image=False):
    """Small valid PDF fixture generator, not a PDF parser."""
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream = ('BT /F1 12 Tf 72 720 Td (' + escaped + ') Tj ET').encode('ascii')
    if image:
        stream += b'\nq 1 0 0 1 10 10 cm /Im1 Do Q'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Count %d /Kids [%s] >>' % (pages, ' '.join('%d 0 R' % (5 + i) for i in range(pages)))).encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               ('<< /Length %d >>\nstream\n' % len(stream)).encode() + stream + b'\nendstream']
    image_resource = (' /XObject << /Im1 %d 0 R >>' % (5 + pages)).encode() if image else b''
    objects.extend([b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >>'
                    + image_resource + b' >> /Contents 4 0 R >>'] * pages)
    if image:
        objects.append(b'<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB '
                       b'/BitsPerComponent 8 /Length 3 >>\nstream\n\xff\x00\x00\nendstream')
    output = bytearray(b'%PDF-1.7\n')
    offsets = [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(('%d 0 obj\n' % i).encode() + value + b'\nendobj\n')
    xref = len(output)
    output.extend(('xref\n0 %d\n0000000000 65535 f \n' % len(offsets)).encode())
    for offset in offsets[1:]:
        output.extend(('%010d 00000 n \n' % offset).encode())
    output.extend(('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(offsets), xref)).encode())
    return bytes(output)


def pdf_integration_suite(vendor_path):
    """Explicit opt-in: absence/version mismatch is failure, never a skipped pass."""
    A._vendor_path(vendor_path)

    class RealPDFCase(unittest.TestCase):
        def test_real_text_and_multiple_pages(self):
            self.assertEqual(A.extract_text('synthetic.pdf', synthetic_pdf()), 'Hello PDF')
            self.assertEqual(A.extract_text('synthetic.pdf', synthetic_pdf('Page', 2)), 'Page\nPage')
            self.assertEqual(A.extract_text('image-and-text.pdf', synthetic_pdf('Text layer', image=True)).strip(), 'Text layer')

        def test_page_limit_and_no_text(self):
            with self.assertRaisesRegex(ValueError, '^ATTACHMENT_PDF_TOO_MANY_PAGES$'):
                A.extract_text('many.pdf', synthetic_pdf(pages=101))
            with self.assertRaisesRegex(ValueError, '^ATTACHMENT_PDF_NO_TEXT_OCR_UNSUPPORTED$'):
                A.extract_text('image-only.pdf', synthetic_pdf('', image=True))

        def test_encryption_and_text_size_and_malformed(self):
            library = A._pdf_module()
            writer = library.PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.encrypt('synthetic-password')
            encrypted = io.BytesIO()
            writer.write(encrypted)
            with self.assertRaisesRegex(ValueError, '^ATTACHMENT_PDF_ENCRYPTED_UNSUPPORTED$'):
                A.extract_text('locked.pdf', encrypted.getvalue())
            with self.assertRaisesRegex(ValueError, '^ATTACHMENT_TEXT_TOO_LARGE$'):
                A.extract_text('large.pdf', synthetic_pdf('x' * (A.MAX_TEXT_CHARS + 1)))
            with self.assertRaisesRegex(ValueError, '^ATTACHMENT_PDF_INVALID$'):
                A.extract_text('broken.pdf', b'%PDF-1.7\nprivate malformed input')

    return unittest.defaultTestLoader.loadTestsFromTestCase(RealPDFCase)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--pdf-integration':
        result = unittest.TextTestRunner(verbosity=2).run(pdf_integration_suite(sys.argv[2]))
        raise SystemExit(0 if result.wasSuccessful() else 1)
    unittest.main()
