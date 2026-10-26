"""Bounded text-only attachment reader for a disposable MXC/Job worker.

No Office/PDF program is launched and no document relationship is followed.
DOCX visible hyperlink labels are retained; external hyperlink targets are ignored.
Images, formatting, headers, footers and embedded objects are not extracted.
The caller must provide process time/memory limits; this module is not a sandbox.
"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib
import io
import json
import logging
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import unicodedata
from urllib.parse import unquote, urlsplit
import warnings
import xml.etree.ElementTree as ET
import zipfile


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 25000
MAX_ZIP_MEMBERS = 256
MAX_ZIP_MEMBER_BYTES = 4 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ZIP_RATIO = 100
MAX_XML_DEPTH = 128
MAX_XML_ELEMENTS = 100000
MAX_PDF_PAGES = 100
MAX_PDF_STREAM_BYTES = 5 * 1024 * 1024
MAX_PDF_TOTAL_STREAM_BYTES = 20 * 1024 * 1024
PYPDF_VERSION = "6.21.2"
ERROR_CODES = frozenset('ATTACHMENT_' + suffix for suffix in (
    'BINARY_TEXT', 'CLI_USAGE', 'DOCX_INVALID', 'DOCX_LIMIT', 'DOCX_UNSAFE', 'EMPTY',
    'ENCODING_UNSUPPORTED', 'FILE_TOO_LARGE', 'INPUT_UNREADABLE', 'INVALID',
    'PDF_ENCRYPTED_UNSUPPORTED', 'PDF_INVALID', 'PDF_LIMIT', 'PDF_NO_TEXT_OCR_UNSUPPORTED',
    'PDF_SUPPORT_NOT_INSTALLED', 'PDF_TOO_MANY_PAGES', 'PDF_VENDOR_INVALID',
    'PDF_VERSION_UNSUPPORTED', 'TEXT_TOO_LARGE', 'TYPE_UNSUPPORTED',
    'FILE_SIZE_INVALID', 'NORMAL_HOST_REQUIRED', 'SANDBOX_SETUP_REQUIRED',
    'LINKED_PATH_REFUSED', 'ORDINARY_FILE_REQUIRED', 'ORDINARY_DIRECTORY_REQUIRED',
    'PDF_DEPENDENCY_INVALID', 'PDF_DEPENDENCY_CHANGED', 'EXTRACTION_INPUT_CHANGED',
    'EXTRACTION_CONTAINMENT_FAILED', 'EXTRACTION_FAILED', 'TEMP_CLEANUP_FAILED'))
WORD_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json"}


class _Rejected(ValueError):
    """Internal distinction from exceptions raised by document parsers."""


def _reject(code):
    raise _Rejected("ATTACHMENT_" + code)


def _decoded(raw):
    try:
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            _reject("ENCODING_UNSUPPORTED")
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")
    except UnicodeError:
        _reject("ENCODING_UNSUPPORTED")


def _checked_text(text):
    if len(text) > MAX_TEXT_CHARS:
        _reject("TEXT_TOO_LARGE")
    if any(unicodedata.category(c) in {"Cc", "Cs"} and c not in "\t\r\n" for c in text):
        _reject("BINARY_TEXT")
    if not text.strip():
        _reject("EMPTY")
    return text


def _xml(raw):
    text = _decoded(raw)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        _reject("DOCX_UNSAFE")
    root = ET.fromstring(text)
    count = 0
    pending = [(root, 1)]
    while pending:
        element, depth = pending.pop()
        count += 1
        if depth > MAX_XML_DEPTH or count > MAX_XML_ELEMENTS:
            _reject("DOCX_LIMIT")
        pending.extend((child, depth + 1) for child in element)
    return root


def _safe_member(info):
    name = info.filename
    pieces = name.rstrip("/").split("/")
    mode = info.external_attr >> 16
    if (not name or name != info.orig_filename or "\\" in name or ":" in name
            or name.startswith("/") or any(p in {"", ".", ".."} for p in pieces)
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
            or info.flag_bits & 1):
        _reject("DOCX_UNSAFE")
    lower = name.lower()
    if (any(p in {"embeddings", "activex"} for p in lower.split("/"))
            or "vbaproject" in lower or "vbadata" in lower
            or PurePosixPath(lower).suffix in {".exe", ".dll", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".mjs", ".jar"}):
        _reject("DOCX_UNSAFE")
    if (info.file_size > MAX_ZIP_MEMBER_BYTES or info.file_size < 0
            or info.file_size > max(1, info.compress_size) * MAX_ZIP_RATIO
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
        _reject("DOCX_LIMIT")


def _check_relationships(root, member_name, names):
    for rel in root:
        if rel.tag.rsplit("}", 1)[-1] != "Relationship":
            continue
        kind = rel.get("Type", "").lower().rsplit("/", 1)[-1]
        if kind in {"oleobject", "package", "attachedtemplate", "afchunk", "vbaproject", "control"}:
            _reject("DOCX_UNSAFE")
        # Hyperlinks are visible labels only; targets are never read or requested.
        if kind == "hyperlink":
            continue
        target = unquote(rel.get("Target", ""))
        uri = urlsplit(target)
        if (rel.get("TargetMode", "Internal").lower() != "internal"
                or uri.scheme or uri.netloc or "\\" in target or ":" in target
                or any(ord(c) < 32 for c in target)):
            _reject("DOCX_UNSAFE")
        # Resolve package-internal relative names lexically, never on the filesystem.
        parts = [] if uri.path.startswith("/") else member_name.split("/")[:-2]
        for part in uri.path.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    _reject("DOCX_UNSAFE")
                parts.pop()
            else:
                parts.append(part)
        if uri.path and "/".join(parts) not in names:
            _reject("DOCX_INVALID")


def _docx(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            _reject("DOCX_LIMIT")
        names = set()
        total = 0
        for info in infos:
            _safe_member(info)
            if info.filename.casefold() in names:
                _reject("DOCX_UNSAFE")
            names.add(info.filename.casefold())
            total += info.file_size
            if total > MAX_ZIP_TOTAL_BYTES:
                _reject("DOCX_LIMIT")
        names = {i.filename for i in infos}
        if not {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= names:
            _reject("DOCX_INVALID")
        document = None
        for info in infos:
            if info.is_dir():
                continue
            # Read every member with a bound to verify lengths/CRC, without extracting files.
            with archive.open(info) as stream:
                member = stream.read(MAX_ZIP_MEMBER_BYTES + 1)
            if len(member) != info.file_size or len(member) > MAX_ZIP_MEMBER_BYTES:
                _reject("DOCX_LIMIT")
            if not info.filename.lower().endswith((".xml", ".rels")):
                continue
            xml = _xml(member)
            if info.filename.lower().endswith(".rels"):
                _check_relationships(xml, info.filename, names)
            for element in xml.iter():
                local = element.tag.rsplit("}", 1)[-1]
                if local in {"OLEObject", "altChunk", "object", "control"}:
                    _reject("DOCX_UNSAFE")
                if local in {"Default", "Override"}:
                    content_type = element.get("ContentType", "").lower()
                    if any(word in content_type for word in ("macroenabled", "vbaproject", "oleobject", "activex")):
                        _reject("DOCX_UNSAFE")
            if info.filename == "word/document.xml":
                document = xml
        if document is None or document.tag not in {"{" + ns + "}document" for ns in WORD_NAMESPACES}:
            _reject("DOCX_INVALID")
        chunks = []
        length = 0
        pending = [(document, False)]
        while pending:
            element, ending = pending.pop()
            namespace, _, local = element.tag[1:].partition("}") if element.tag.startswith("{") else ("", "", element.tag)
            value = ""
            if namespace in WORD_NAMESPACES:
                if not ending and local == "t":
                    value = element.text or ""
                elif not ending and local == "tab":
                    value = "\t"
                elif (not ending and local in {"br", "cr"}) or (ending and local == "p"):
                    value = "\n"
            if value:
                length += len(value)
                # One terminal paragraph separator is removed after extraction.
                if length > MAX_TEXT_CHARS + 1:
                    _reject("TEXT_TOO_LARGE")
                chunks.append(value)
            if not ending:
                pending.append((element, True))
                pending.extend((child, False) for child in reversed(element))
        text = "".join(chunks)
        return text[:-1] if text.endswith("\n") else text


class _DiscardOutput(io.TextIOBase):
    def write(self, text):
        return len(text)


@contextmanager
def _quiet_parser():
    # Only used inside the disposable, single-request worker (no shared UI logger).
    disabled = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with warnings.catch_warnings(), redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
            warnings.simplefilter("ignore")
            yield
    finally:
        logging.disable(disabled)


def _pdf_module():
    try:
        module = importlib.import_module("pypdf")
    except ImportError:
        _reject("PDF_SUPPORT_NOT_INSTALLED")
    if getattr(module, "__version__", None) != PYPDF_VERSION:
        _reject("PDF_VERSION_UNSUPPORTED")
    return module


def _pdf(raw):
    if not raw.startswith(b"%PDF-"):
        _reject("PDF_INVALID")
    with _quiet_parser():
        library = _pdf_module()
        # Public pypdf 6.21.2 limits. External image decoder execution is disabled.
        with library.apply_configuration(
                maximum_declared_stream_length=MAX_PDF_STREAM_BYTES,
                array_based_stream_maximum_output_length=MAX_PDF_STREAM_BYTES,
                zlib_maximum_output_length=MAX_PDF_STREAM_BYTES,
                lzw_maximum_output_length=MAX_PDF_STREAM_BYTES,
                run_length_maximum_output_length=MAX_PDF_STREAM_BYTES,
                brotli_maximum_output_length=MAX_PDF_STREAM_BYTES,
                image_maximum_buffer_size=MAX_PDF_STREAM_BYTES,
                stream_decoding_work_maximum_length=MAX_PDF_TOTAL_STREAM_BYTES,
                stream_parsing_work_maximum_count=250000,
                page_tree_maximum_entries=1000, page_tree_maximum_depth=32,
                xform_maximum_invocations_per_extraction=1000,
                jbig2dec_binary=None, disable_legacy_handling=True):
            try:
                reader = library.PdfReader(io.BytesIO(raw), strict=True)
                if reader.is_encrypted:
                    _reject("PDF_ENCRYPTED_UNSUPPORTED")
                if len(reader.pages) > MAX_PDF_PAGES:
                    _reject("PDF_TOO_MANY_PAGES")
                chunks = []
                length = 0
                stream_bytes = 0
                for page in reader.pages:
                    contents = page.get_contents()
                    if contents is not None:
                        stream_bytes += len(contents.get_data())
                        if stream_bytes > MAX_PDF_TOTAL_STREAM_BYTES:
                            _reject("PDF_LIMIT")
                    part = page.extract_text() or ""
                    length += len(part) + (1 if chunks else 0)
                    if length > MAX_TEXT_CHARS:
                        _reject("TEXT_TOO_LARGE")
                    chunks.append(part)
                text = "\n".join(chunks)
                if not text.strip():
                    _reject("PDF_NO_TEXT_OCR_UNSUPPORTED")
                return text
            except library.errors.LimitReachedError:
                _reject("PDF_LIMIT")


def extract_text(name, raw_bytes):
    """Return complete extracted text, or ValueError with a fixed public error code."""
    extension = ""
    try:
        if not isinstance(name, str) or not isinstance(raw_bytes, bytes):
            _reject("INVALID")
        if len(raw_bytes) > MAX_FILE_BYTES:
            _reject("FILE_TOO_LARGE")
        if not raw_bytes:
            _reject("EMPTY")
        extension = PurePosixPath(name.replace("\\", "/")).suffix.lower()
        if extension in TEXT_EXTENSIONS:
            text = _decoded(raw_bytes)
        elif extension == ".docx":
            text = _docx(raw_bytes)
        elif extension == ".pdf":
            text = _pdf(raw_bytes)
        else:
            _reject("TYPE_UNSUPPORTED")
        return _checked_text(text)
    except _Rejected as error:
        raise ValueError(str(error)) from None
    except Exception:
        code = {".docx": "DOCX_INVALID", ".pdf": "PDF_INVALID"}.get(extension, "INVALID")
        raise ValueError("ATTACHMENT_" + code) from None


def _vendor_path(value):
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or root.is_junction() or not root.is_dir():
        _reject("PDF_VENDOR_INVALID")
    package = root / "pypdf"
    if not (package / "__init__.py").is_file() or package.is_symlink() or package.is_junction():
        _reject("PDF_VENDOR_INVALID")
    # The parent supplies an independently verified, worker-local trusted package copy.
    sys.path.insert(0, str(root.resolve()))


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) not in {2, 3}:
            _reject("CLI_USAGE")
        if len(args) == 3:
            _vendor_path(args[2])
        path = Path(args[0])
        if path.is_symlink() or path.is_junction() or not path.is_file():
            _reject("INPUT_UNREADABLE")
        with path.open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        result = {"ok": True, "text": extract_text(args[1], raw)}
    except ValueError as error:
        code = str(error)
        result = {"ok": False, "error": code if re.fullmatch(r"ATTACHMENT_[A-Z_]+", code) else "ATTACHMENT_INVALID"}
    except Exception:
        result = {"ok": False, "error": "ATTACHMENT_INPUT_UNREADABLE"}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
