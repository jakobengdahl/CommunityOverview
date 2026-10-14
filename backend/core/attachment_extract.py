"""Bounded text extraction from node-attachment bytes.

Given the bytes of a stored attachment and the content type the upload path
sniffed from those bytes (ADR 0008 §5), return its text as a list of segments,
each with a locator (page, sheet, slide, paragraph, table or line) that a
citation can point at, plus a status:

``ok``               text was extracted (possibly ``truncated``)
``no_text_layer``    a PDF whose pages carry no extractable text (e.g. a scan)
``unsupported_type`` the content type has no extractor (images: no OCR)
``too_large``        the input, or an archive's declared contents, exceed a cap
``timeout``          extraction ran past the wall-clock limit and was killed
``failed``           the file could not be parsed

The input is untrusted, so the parsing runs in a child interpreter (this file
run as a script) that the parent kills at the wall-clock limit and that runs
under address-space and CPU rlimits where the platform has them. ZIP-based
formats are checked against declared member sizes and compression ratios
before anything is decompressed, and XML is parsed without DTDs, entity
expansion or network access. Dispatch is by the sniffed content type alone:
there is no file name parameter, so an extension cannot pick the parser.

The module is pure: it reads no settings and touches no storage. It must keep
importing only the standard library at module level, because the child runs
it outside the ``backend`` package.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass, field

STATUS_OK = "ok"
STATUS_NO_TEXT_LAYER = "no_text_layer"
STATUS_UNSUPPORTED_TYPE = "unsupported_type"
STATUS_TOO_LARGE = "too_large"
STATUS_TIMEOUT = "timeout"
STATUS_FAILED = "failed"
STATUSES = frozenset(
    {
        STATUS_OK,
        STATUS_NO_TEXT_LAYER,
        STATUS_UNSUPPORTED_TYPE,
        STATUS_TOO_LARGE,
        STATUS_TIMEOUT,
        STATUS_FAILED,
    }
)

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ODT = "application/vnd.oasis.opendocument.text"
ODS = "application/vnd.oasis.opendocument.spreadsheet"
ODP = "application/vnd.oasis.opendocument.presentation"
TEXT = "text/plain"
CSV = "text/csv"
SUPPORTED_CONTENT_TYPES = frozenset({PDF, DOCX, XLSX, PPTX, ODT, ODS, ODP, TEXT, CSV})

_MiB = 1024 * 1024


@dataclass(frozen=True)
class ExtractionLimits:
    timeout_seconds: float = 30.0
    max_input_bytes: int = 25 * _MiB
    # Characters of text returned per file, across all segments.
    max_chars: int = 1_000_000
    max_zip_members: int = 2_000
    # Sum of the uncompressed sizes the archive's central directory declares.
    max_zip_uncompressed_bytes: int = 100 * _MiB
    max_zip_ratio: float = 100.0
    # Members (and archives) smaller than this skip the ratio test: small XML
    # parts legitimately compress far beyond any sane bomb threshold.
    zip_ratio_floor_bytes: int = _MiB
    # Address-space cap for the child process; None leaves it unlimited.
    memory_limit_bytes: int | None = 1024 * _MiB


@dataclass(frozen=True)
class Segment:
    text: str
    locator: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    segments: tuple[Segment, ...] = ()
    truncated: bool = False
    # A short machine-readable reason for a non-ok status; never file content.
    detail: str | None = None


def extract(
    data: bytes, content_type: str, limits: ExtractionLimits | None = None
) -> ExtractionResult:
    """Extract the text of ``data``, parsed as the sniffed ``content_type``."""
    limits = limits or ExtractionLimits()
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype not in SUPPORTED_CONTENT_TYPES:
        return ExtractionResult(STATUS_UNSUPPORTED_TYPE, detail=ctype or "missing")
    if len(data) > limits.max_input_bytes:
        return ExtractionResult(STATUS_TOO_LARGE, detail="input_bytes")
    return _run_worker(data, ctype, limits, _worker_argv())


def _worker_argv() -> list[str]:
    # -P keeps this file's directory off sys.path, so sibling modules in
    # backend/core cannot shadow the parser libraries the child imports.
    return [sys.executable, "-P", os.path.abspath(__file__)]


def _run_worker(
    data: bytes, ctype: str, limits: ExtractionLimits, argv: list[str]
) -> ExtractionResult:
    header = json.dumps({"content_type": ctype, "limits": asdict(limits)}).encode()
    payload = len(header).to_bytes(4, "big") + header + data
    try:
        proc = subprocess.run(
            argv,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=limits.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ExtractionResult(STATUS_TIMEOUT, detail="wall_clock")
    if proc.returncode != 0:
        return ExtractionResult(STATUS_FAILED, detail=f"worker_exit_{proc.returncode}")
    try:
        return _from_wire(json.loads(proc.stdout), limits)
    except (ValueError, KeyError, TypeError):
        return ExtractionResult(STATUS_FAILED, detail="worker_output")


def _to_wire(result: ExtractionResult) -> dict:
    return {
        "status": result.status,
        "segments": [{"text": s.text, "locator": s.locator} for s in result.segments],
        "truncated": result.truncated,
        "detail": result.detail,
    }


def _from_wire(obj: dict, limits: ExtractionLimits) -> ExtractionResult:
    status = obj["status"]
    if status not in STATUSES:
        raise ValueError("unknown status")
    segments = []
    for raw in obj["segments"]:
        text, locator = raw["text"], raw["locator"]
        if not isinstance(text, str) or not isinstance(locator, dict):
            raise TypeError("malformed segment")
        segments.append(Segment(text, locator))
    if sum(len(s.text) for s in segments) > limits.max_chars:
        raise ValueError("worker exceeded the character cap")
    detail = obj.get("detail")
    return ExtractionResult(
        status,
        tuple(segments),
        bool(obj["truncated"]),
        detail if isinstance(detail, str) else None,
    )


# --- Everything below runs in the child process. ---------------------------


class _TooLarge(Exception):
    pass


class _Refused(Exception):
    pass


class _Collector:
    """Accumulates segments until the per-file character budget runs out."""

    def __init__(self, max_chars: int):
        self.remaining = max_chars
        self.segments: list[Segment] = []
        self.truncated = False

    def add(self, text: str, locator: dict) -> bool:
        """Add a segment; return False once no more text will be accepted."""
        if self.truncated:
            return False
        if not text or not text.strip():
            return True
        # Lone surrogates (seen in some PDF text) cannot cross the UTF-8 pipe.
        text = text.encode("utf-8", "replace").decode("utf-8")
        if len(text) > self.remaining:
            text = text[: self.remaining]
            self.truncated = True
        self.remaining -= len(text)
        if text:
            self.segments.append(Segment(text, dict(locator)))
        return not self.truncated


def _bounded_join(parts, sep: str, limit: int) -> str:
    """Join lazily produced ``parts``, stopping once ``limit`` chars are passed.

    The collector truncates exactly; this only stops a huge sheet or table from
    being materialised in full when the budget can take a fraction of it.
    """
    buf = []
    size = 0
    for part in parts:
        buf.append(part)
        size += len(part) + len(sep)
        if size > limit:
            break
    return sep.join(buf)


def _extract_in_process(
    data: bytes, ctype: str, limits: ExtractionLimits
) -> ExtractionResult:
    out = _Collector(limits.max_chars)
    try:
        status = _EXTRACTORS[ctype](data, out, limits)
    except _TooLarge as exc:
        return ExtractionResult(STATUS_TOO_LARGE, detail=str(exc))
    except _Refused as exc:
        return ExtractionResult(STATUS_FAILED, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — any parser error is a failed file
        return ExtractionResult(STATUS_FAILED, detail=type(exc).__name__)
    return ExtractionResult(status, tuple(out.segments), out.truncated)


# --- Plain text --------------------------------------------------------------

_TEXT_BLOCK_LINES = 50


def _extract_text(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    # ADR 0008 §5 text rule: strict UTF-8 (BOM allowed), no NUL byte.
    if b"\x00" in data:
        raise _Refused("nul_byte")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _Refused("not_utf8") from None
    block: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not block:
                start = number
            block.append(line)
            if len(block) < _TEXT_BLOCK_LINES:
                continue
        if block and not out.add("\n".join(block), {"line": start}):
            return STATUS_OK
        block = []
    if block:
        out.add("\n".join(block), {"line": start})
    return STATUS_OK


# --- PDF -----------------------------------------------------------------------


def _extract_pdf(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt("")
        except Exception:  # noqa: BLE001 — missing crypto backend, bad keys, ...
            decrypted = 0
        if not decrypted:
            raise _Refused("encrypted")
    for number, page in enumerate(reader.pages, 1):
        if not out.add(page.extract_text() or "", {"page": number}):
            break
    if not out.segments:
        return STATUS_NO_TEXT_LAYER
    return STATUS_OK


# --- ZIP containers and XML ------------------------------------------------------


def _open_zip(data: bytes, limits: ExtractionLimits) -> zipfile.ZipFile:
    """Open an archive after checking what its central directory declares.

    Reading the central directory decompresses nothing, so a bomb is refused
    on its declared sizes before a single member is inflated. zipfile itself
    never yields more than a member's declared size, so the declared total is
    also a bound on what later reads can produce.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _Refused("not_a_zip") from None
    infos = archive.infolist()
    if len(infos) > limits.max_zip_members:
        raise _TooLarge("zip_members")
    total = sum(info.file_size for info in infos)
    if total > limits.max_zip_uncompressed_bytes:
        raise _TooLarge("zip_uncompressed_bytes")
    if (
        total > limits.zip_ratio_floor_bytes
        and total / max(len(data), 1) > limits.max_zip_ratio
    ):
        raise _TooLarge("zip_ratio")
    for info in infos:
        if (
            info.file_size > limits.zip_ratio_floor_bytes
            and info.file_size / max(info.compress_size, 1) > limits.max_zip_ratio
        ):
            raise _TooLarge("zip_ratio")
    return archive


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise _Refused("missing_part") from None
    with archive.open(info) as member:
        return member.read()


def _parse_xml(raw: bytes):
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )
    root = etree.fromstring(raw, parser)
    docinfo = root.getroottree().docinfo
    # No OOXML or ODF part carries a DTD; one that does is an attack, not a
    # document, so it is refused rather than parsed around.
    if docinfo.doctype or docinfo.internalDTD is not None:
        raise _Refused("xml_dtd")
    return root


def _parse_member(archive: zipfile.ZipFile, name: str):
    return _parse_xml(_read_member(archive, name))


def _localname(element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _relationship_id(element) -> str | None:
    """The namespaced ``r:id`` attribute (not a bare ``id``), in any namespace."""
    for key, value in element.attrib.items():
        if key.startswith("{") and key.endswith("}id"):
            return value
    return None


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """Map relationship id -> (type, member name) for an OOXML part."""
    directory, filename = posixpath.split(part)
    rels_name = posixpath.join(directory, "_rels", filename + ".rels")
    if rels_name not in archive.NameToInfo:
        return {}
    result = {}
    for rel in _parse_member(archive, rels_name).iter("{*}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        if target.startswith("/"):
            name = target.lstrip("/")
        else:
            name = posixpath.normpath(posixpath.join(directory, target))
        result[rel.get("Id") or ""] = (rel.get("Type") or "", name)
    return result


# --- DOCX ------------------------------------------------------------------------


def _extract_docx(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    archive = _open_zip(data, limits)
    # python-docx parses the parts with its own parser; refuse any DTD-bearing
    # part with ours first so the guarantee does not rest on its settings.
    for name in archive.namelist():
        if name.lower().endswith((".xml", ".rels")):
            _parse_member(archive, name)

    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    paragraph_tag, table_tag = qn("w:p"), qn("w:tbl")
    paragraphs = tables = 0
    for child in document.element.body.iterchildren():
        if child.tag == paragraph_tag:
            paragraphs += 1
            text, locator = Paragraph(child, document).text, {"paragraph": paragraphs}
        elif child.tag == table_tag:
            tables += 1
            text = _docx_table_text(Table(child, document), out.remaining)
            locator = {"table": tables}
        else:
            continue
        if not out.add(text, locator):
            break
    return STATUS_OK


def _docx_table_text(table, limit: int) -> str:
    def rows():
        for row in table.rows:
            cells = []
            previous = None
            for cell in row.cells:
                # A horizontally merged cell is reported once per grid column.
                if previous is not None and cell._tc is previous:
                    continue
                previous = cell._tc
                cells.append(cell.text)
            line = "\t".join(cells).rstrip("\t")
            if line.strip():
                yield line

    return _bounded_join(rows(), "\n", limit)


# --- XLSX ------------------------------------------------------------------------


def _xlsx_shared_strings(archive: zipfile.ZipFile, rels: dict) -> list[str]:
    name = next(
        (
            path
            for rel_type, path in rels.values()
            if rel_type.endswith("/sharedStrings")
        ),
        "xl/sharedStrings.xml",
    )
    if name not in archive.NameToInfo:
        return []
    strings = []
    for item in _parse_member(archive, name).iter("{*}si"):
        strings.append(
            "".join(
                t.text or ""
                for t in item.iter("{*}t")
                if _localname(t.getparent()) != "rPh"  # phonetic guide, not content
            )
        )
    return strings


def _xlsx_cell_text(cell, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter("{*}t"))
    value = cell.find("{*}v")
    raw = (value.text or "") if value is not None else ""
    if kind == "s":
        try:
            index = int(raw)
        except ValueError:
            return ""
        return shared[index] if 0 <= index < len(shared) else ""
    return raw


def _extract_xlsx(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    archive = _open_zip(data, limits)
    workbook_name = "xl/workbook.xml"
    workbook = _parse_member(archive, workbook_name)
    rels = _relationships(archive, workbook_name)
    shared = _xlsx_shared_strings(archive, rels)
    for sheet in workbook.iter("{*}sheet"):
        target = rels.get(_relationship_id(sheet) or "")
        if target is None or target[1] not in archive.NameToInfo:
            continue
        root = _parse_member(archive, target[1])

        def rows(root=root):
            for row in root.iter("{*}row"):
                line = "\t".join(_xlsx_cell_text(c, shared) for c in row.iter("{*}c"))
                line = line.rstrip("\t")
                if line.strip():
                    yield line

        text = _bounded_join(rows(), "\n", out.remaining)
        if not out.add(text, {"sheet": sheet.get("name") or ""}):
            break
    return STATUS_OK


# --- PPTX ------------------------------------------------------------------------


def _extract_pptx(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    archive = _open_zip(data, limits)
    presentation_name = "ppt/presentation.xml"
    presentation = _parse_member(archive, presentation_name)
    rels = _relationships(archive, presentation_name)
    for number, slide_id in enumerate(presentation.iter("{*}sldId"), 1):
        target = rels.get(_relationship_id(slide_id) or "")
        if target is None or target[1] not in archive.NameToInfo:
            continue
        slide = _parse_member(archive, target[1])
        lines = (
            "".join(t.text or "" for t in paragraph.iter("{*}t"))
            for paragraph in slide.iter("{*}p")
        )
        text = _bounded_join(
            (line for line in lines if line.strip()), "\n", out.remaining
        )
        if not out.add(text, {"slide": number}):
            break
    return STATUS_OK


# --- OpenDocument ------------------------------------------------------------------

_ODF_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_ODF_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_ODF_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_ODF_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_ODF_PARAGRAPHS = (f"{{{_ODF_TEXT}}}p", f"{{{_ODF_TEXT}}}h")
_ODF_MAX_SPACES = 100


def _odf_text(element) -> str:
    """Text of one ODF paragraph, expanding space/tab/break elements.

    Paragraphs nested inside this one (text boxes, notes) are skipped; the
    document-order walk that called this emits them on their own.
    """
    parts = [element.text or ""]
    for child in element:
        name = _localname(child)
        if name == "s":
            try:
                count = int(child.get(f"{{{_ODF_TEXT}}}c") or 1)
            except ValueError:
                count = 1
            parts.append(" " * max(1, min(count, _ODF_MAX_SPACES)))
        elif name == "tab":
            parts.append("\t")
        elif name == "line-break":
            parts.append("\n")
        elif child.tag not in _ODF_PARAGRAPHS:
            parts.append(_odf_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odf_body(data: bytes, limits: ExtractionLimits, kind: str):
    archive = _open_zip(data, limits)
    body = _parse_member(archive, "content.xml").find(f"{{{_ODF_OFFICE}}}body")
    if body is None:
        raise _Refused("missing_body")
    return body.find(f"{{{_ODF_OFFICE}}}{kind}")


def _extract_odt(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    content = _odf_body(data, limits, "text")
    if content is None:
        return STATUS_OK
    for number, paragraph in enumerate(content.iter(*_ODF_PARAGRAPHS), 1):
        if not out.add(_odf_text(paragraph), {"paragraph": number}):
            break
    return STATUS_OK


def _extract_ods(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    content = _odf_body(data, limits, "spreadsheet")
    if content is None:
        return STATUS_OK
    for table in content.iter(f"{{{_ODF_TABLE}}}table"):

        def rows(table=table):
            for row in table.iter(f"{{{_ODF_TABLE}}}table-row"):
                cells = (
                    "\n".join(_odf_text(p) for p in cell.iter(*_ODF_PARAGRAPHS))
                    for cell in row
                    if _localname(cell) in ("table-cell", "covered-table-cell")
                )
                line = "\t".join(cells).rstrip("\t")
                if line.strip():
                    yield line

        text = _bounded_join(rows(), "\n", out.remaining)
        if not out.add(text, {"sheet": table.get(f"{{{_ODF_TABLE}}}name") or ""}):
            break
    return STATUS_OK


def _extract_odp(data: bytes, out: _Collector, limits: ExtractionLimits) -> str:
    content = _odf_body(data, limits, "presentation")
    if content is None:
        return STATUS_OK
    for number, page in enumerate(content.iter(f"{{{_ODF_DRAW}}}page"), 1):
        lines = (_odf_text(p) for p in page.iter(*_ODF_PARAGRAPHS))
        text = _bounded_join(
            (line for line in lines if line.strip()), "\n", out.remaining
        )
        if not out.add(text, {"slide": number}):
            break
    return STATUS_OK


_EXTRACTORS = {
    PDF: _extract_pdf,
    DOCX: _extract_docx,
    XLSX: _extract_xlsx,
    PPTX: _extract_pptx,
    ODT: _extract_odt,
    ODS: _extract_ods,
    ODP: _extract_odp,
    TEXT: _extract_text,
    CSV: _extract_text,
}


def _apply_resource_limits(limits: ExtractionLimits) -> None:
    try:
        import resource
    except ImportError:  # not POSIX: the parent's wall clock still applies
        return
    cpu = int(limits.timeout_seconds) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    # The child writes only to its stdout pipe; a parser that tries to write a
    # file is killed rather than allowed to.
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    if limits.memory_limit_bytes is not None:
        cap = limits.memory_limit_bytes
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))


def _worker_main() -> int:
    import logging
    import warnings

    raw = sys.stdin.buffer.read()
    header_len = int.from_bytes(raw[:4], "big")
    header = json.loads(raw[4 : 4 + header_len])
    data = raw[4 + header_len :]
    del raw
    limits = ExtractionLimits(**header["limits"])
    _apply_resource_limits(limits)
    # Parser libraries log and warn per malformed object; a crafted file must
    # not turn that into unbounded output.
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    result = _extract_in_process(data, header["content_type"], limits)
    sys.stdout.buffer.write(
        json.dumps(_to_wire(result), ensure_ascii=False).encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
