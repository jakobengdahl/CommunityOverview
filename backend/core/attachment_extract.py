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
run as a script) that the parent kills, with its process group, at the
wall-clock limit and that runs under address-space and CPU rlimits where the
platform has them. The parent bounds what it reads back and releases its pipe
ends on return, so even a compromised child cannot hold the caller's memory,
threads or descriptors past the call (a process that leaves the group on
purpose is outside what a process group can kill). ZIP-based
formats are checked against declared member sizes and compression ratios
before anything is decompressed, and XML is parsed without DTDs, entity
expansion or network access. Dispatch is by the sniffed content type alone:
there is no file name parameter, so an extension cannot pick the parser.

``extract`` blocks for up to the timeout and each call holds one child
process: a caller on an event loop runs it in a worker thread and bounds how
many run at once.

The module is pure: it reads no settings and touches no storage. It must keep
importing only the standard library at module level, because the child runs
it outside the ``backend`` package.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import signal
import subprocess
import sys
import threading
import time
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
# Locator strings (sheet names) are file content too; they are cut to this.
_MAX_LOCATOR_CHARS = 64
# Locator numbers (page, line, ...) stay below this, so they too fit in the
# per-segment share of the reply cap.
_MAX_LOCATOR_INT = 2**53
_LOCATOR_KEYS = frozenset({"page", "paragraph", "table", "sheet", "slide", "line"})


@dataclass(frozen=True)
class ExtractionLimits:
    timeout_seconds: float = 30.0
    max_input_bytes: int = 25 * _MiB
    # Characters of text returned per file, across all segments.
    max_chars: int = 1_000_000
    # Segments returned per file: bounds the reply for a file of a million
    # one-character paragraphs, which max_chars alone would not.
    max_segments: int = 10_000
    max_zip_members: int = 2_000
    # Sum of the uncompressed sizes the archive's central directory declares.
    max_zip_uncompressed_bytes: int = 100 * _MiB
    max_zip_ratio: float = 100.0
    # Members smaller than this skip the per-member ratio test: small XML
    # parts legitimately compress far beyond any sane bomb threshold. The
    # whole-archive ratio has no floor.
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
    # -I: no PYTHON* variables, no user site, and this file's directory off
    # sys.path, so nothing beside it can shadow the parser libraries.
    return [sys.executable, "-I", os.path.abspath(__file__)]


def _max_reply_bytes(limits: ExtractionLimits) -> int:
    # JSON escapes a control character as six bytes. A segment's one-key
    # locator (at most 6 x 64 bytes of string) and its framing fit in 512
    # bytes; the reply's own framing fits in the constant.
    return 6 * limits.max_chars + 512 * limits.max_segments + 4096


def _run_worker(
    data: bytes, ctype: str, limits: ExtractionLimits, argv: list[str]
) -> ExtractionResult:
    header = json.dumps({"content_type": ctype, "limits": asdict(limits)}).encode()
    payload = len(header).to_bytes(4, "big") + header + data
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # The child parses untrusted bytes; it gets none of the server's
            # environment (credentials, API keys).
            env={},
            # Its own process group, so anything it spawns is killed with it.
            start_new_session=os.name == "posix",
        )
    except OSError:  # out of processes, file descriptors or memory
        return ExtractionResult(STATUS_FAILED, detail="worker_spawn")
    exchange = _exchange_posix if os.name == "posix" else _exchange_threads
    try:
        reply, outcome = exchange(proc, payload, limits)
    except OSError:  # e.g. no descriptor left for the selector
        return ExtractionResult(STATUS_FAILED, detail="worker_io")
    finally:
        _kill_group(proc)
        proc.wait()
    if outcome == "timeout":
        return ExtractionResult(STATUS_TIMEOUT, detail="wall_clock")
    if outcome == "overflow":
        return ExtractionResult(STATUS_FAILED, detail="worker_output")
    if proc.returncode != 0:
        return ExtractionResult(STATUS_FAILED, detail=f"worker_exit_{proc.returncode}")
    try:
        return _from_wire(json.loads(bytes(reply)), limits)
    except (ValueError, KeyError, TypeError, RecursionError):
        return ExtractionResult(STATUS_FAILED, detail="worker_output")


def _exchange_posix(proc: subprocess.Popen, payload: bytes, limits: ExtractionLimits):
    """Feed the payload and read the reply without blocking or threads.

    Returns ``(reply, outcome)`` with outcome ``"exited"``, ``"timeout"`` or
    ``"overflow"``. Both pipe ends are always closed before returning, so
    nothing a hostile child leaves behind (even a process that escaped its
    group) can hold this, the server's, process's threads, descriptors or
    memory beyond the call.
    """
    import selectors

    cap = _max_reply_bytes(limits)
    deadline = time.monotonic() + limits.timeout_seconds
    reply = bytearray()
    view = memoryview(payload)
    written = 0
    pipes = {proc.stdin.fileno(): proc.stdin, proc.stdout.fileno(): proc.stdout}
    in_fd, out_fd = proc.stdin.fileno(), proc.stdout.fileno()
    selector = None
    registered: set[int] = set()

    def close(fd: int) -> None:
        if fd in registered:
            selector.unregister(fd)
            registered.discard(fd)
        if fd in pipes:
            pipes.pop(fd).close()

    try:
        selector = selectors.DefaultSelector()
        for fd, event in (
            (in_fd, selectors.EVENT_WRITE),
            (out_fd, selectors.EVENT_READ),
        ):
            os.set_blocking(fd, False)
            selector.register(fd, event)
            registered.add(fd)
        while out_fd in pipes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return reply, "timeout"
            # Sampled before the select: a child seen exited here has already
            # written all it will, so an empty select after it means no tail.
            exited = proc.poll() is not None
            events = selector.select(min(remaining, 0.05))
            for key, _ in events:
                if key.fd == out_fd:
                    chunk = os.read(out_fd, 65536)
                    if not chunk:
                        close(out_fd)
                    elif len(reply) + len(chunk) > cap:
                        return reply, "overflow"
                    else:
                        reply.extend(chunk)
                elif key.fd in pipes:
                    try:
                        written += os.write(in_fd, view[written : written + 65536])
                    except BlockingIOError:
                        continue
                    except OSError:  # the child closed its end or exited
                        written = len(view)
                    if written >= len(view):
                        close(in_fd)
            if not events and exited:
                # The child has exited and nothing is readable: whatever still
                # holds the pipe open is not the child, so its reply is complete.
                break
        # End of output can come a moment before the exit status does.
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            return reply, "timeout"
        return reply, "exited"
    finally:
        for fd in list(pipes):
            close(fd)
        if selector is not None:
            selector.close()


def _exchange_threads(proc: subprocess.Popen, payload: bytes, limits: ExtractionLimits):
    """Fallback for platforms without non-blocking pipes (Windows)."""
    cap = _max_reply_bytes(limits)
    reply = bytearray()
    overflow = threading.Event()

    def feed() -> None:
        try:
            proc.stdin.write(payload)
        except OSError:  # the child exited or was killed before reading it all
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def drain() -> None:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                return
            if len(reply) + len(chunk) > cap:
                overflow.set()
                return
            reply.extend(chunk)

    threads = [threading.Thread(target=fn, daemon=True) for fn in (feed, drain)]
    deadline = time.monotonic() + limits.timeout_seconds
    try:
        for thread in threads:
            thread.start()
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if overflow.is_set():
                    return reply, "overflow"
                if time.monotonic() >= deadline:
                    return reply, "timeout"
        threads[1].join(timeout=5)
        return reply, "overflow" if overflow.is_set() else "exited"
    finally:
        _kill_group(proc)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
        if not threads[1].is_alive():
            proc.stdout.close()


def _kill_group(proc: subprocess.Popen) -> None:
    if os.name != "posix":
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone


def _valid_locator(locator) -> bool:
    # The extractor emits one key per locator; more would break the
    # per-segment bound in _max_reply_bytes.
    if not isinstance(locator, dict) or len(locator) > 1:
        return False
    for key, value in locator.items():
        if key not in _LOCATOR_KEYS:
            return False
        if isinstance(value, str):
            if len(value) > _MAX_LOCATOR_CHARS:
                return False
        elif not isinstance(value, int) or isinstance(value, bool):
            return False
        elif not 0 <= value < _MAX_LOCATOR_INT:
            return False
    return True


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
    if len(obj["segments"]) > limits.max_segments:
        raise ValueError("worker exceeded the segment cap")
    segments = []
    for raw in obj["segments"]:
        text, locator = raw["text"], raw["locator"]
        if not isinstance(text, str) or not _valid_locator(locator):
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
    """Accumulates segments until the character or segment budget runs out."""

    def __init__(self, max_chars: int, max_segments: int):
        self.remaining = max_chars
        self.max_segments = max_segments
        self.segments: list[Segment] = []
        self.truncated = False

    def add(self, text: str, locator: dict) -> bool:
        """Add a segment; return False once no more text will be accepted."""
        if self.truncated:
            return False
        if not text or not text.strip():
            return True
        if len(self.segments) >= self.max_segments:
            self.truncated = True
            return False
        # Lone surrogates (seen in some PDF text) cannot cross the UTF-8 pipe.
        text = text.encode("utf-8", "replace").decode("utf-8")
        if len(text) > self.remaining:
            text = text[: self.remaining]
            self.truncated = True
        self.remaining -= len(text)
        if text:
            locator = {
                key: value[:_MAX_LOCATOR_CHARS] if isinstance(value, str) else value
                for key, value in locator.items()
            }
            self.segments.append(Segment(text, locator))
        return not self.truncated


def _bounded_join(parts, sep: str, limit: int) -> str:
    """Join lazily produced ``parts``, stopping once the result passes ``limit``.

    This only stops a huge sheet or table from being materialised in full when
    the budget can take a fraction of it. It stops only once the joined text is
    *longer* than ``limit``, so a cut always reaches the collector as text over
    its budget and is reported as truncated.
    """
    buf = []
    size = -len(sep)
    for part in parts:
        buf.append(part)
        size += len(sep) + len(part)
        if size > limit:
            break
    return sep.join(buf)


def _extract_in_process(
    data: bytes, ctype: str, limits: ExtractionLimits
) -> ExtractionResult:
    out = _Collector(limits.max_chars, limits.max_segments)
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
    # Lines are counted by newline as an editor counts them; str.splitlines
    # would also break on form feeds and Unicode separators.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, line in enumerate(lines, 1):
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


_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


def _open_zip(data: bytes, limits: ExtractionLimits) -> zipfile.ZipFile:
    """Open an archive after checking what its central directory declares.

    Reading the central directory decompresses nothing, so a bomb is refused
    on its declared sizes before a single member is inflated. Members are then
    only read through ``_read_member``, which never inflates more than the
    declared size, so the declared total also bounds what a lying archive can
    make the child hold.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _Refused("not_a_zip") from None
    infos = archive.infolist()
    # OOXML and ODF use only these two. Other methods' decompressors are not
    # bounded per read on every supported CPython, so they are refused unread.
    if any(info.compress_type not in _ZIP_METHODS for info in infos):
        raise _Refused("zip_compression")
    if len(infos) > limits.max_zip_members:
        raise _TooLarge("zip_members")
    total = sum(info.file_size for info in infos)
    if total > limits.max_zip_uncompressed_bytes:
        raise _TooLarge("zip_uncompressed_bytes")
    if total / max(len(data), 1) > limits.max_zip_ratio:
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
    return _read_bounded(archive, info)


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    # read() with no size inflates a member's whole stream before cutting it to
    # the declared size, so a member that under-declares would expand without
    # bound; read(n) inflates at most n bytes per step.
    with archive.open(info) as member:
        return member.read(info.file_size)


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


def _under_fallback(element) -> bool:
    # mc:AlternateContent carries the same content twice (Choice, Fallback).
    return any(_localname(a) == "Fallback" for a in element.iterancestors())


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
    members = {
        info.filename: _read_bounded(archive, info)
        for info in archive.infolist()
        if not info.is_dir()
    }
    _refuse_docx_dtds(members)

    from docx import Document
    from docx.oxml.ns import qn

    tags = (qn("w:p"), qn("w:tbl"))
    # python-docx reads members with an unbounded read of its own, so it gets
    # an archive rebuilt from the bounded reads, stored uncompressed.
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_STORED) as target:
        for name, body in members.items():
            target.writestr(name, body)
    document = Document(rebuilt)
    paragraphs = tables = 0
    for block in _docx_blocks(document.element.body, tags):
        if block.tag == tags[0]:
            paragraphs += 1
            text, locator = _docx_paragraph_text(block), {"paragraph": paragraphs}
        else:
            tables += 1
            text = _docx_table_text(block, document, tags, out.remaining)
            locator = {"table": tables}
        if not out.add(text, locator):
            break
    return STATUS_OK


def _refuse_docx_dtds(members: dict[str, bytes]) -> None:
    """Parse, with our parser, every member python-docx could parse as XML.

    python-docx picks XML parts by the content type ``[Content_Types].xml``
    gives them, not by extension, and uses its own parser; checking each such
    part here keeps the no-DTD guarantee from resting on its settings.
    """
    try:
        types = _parse_xml(members["[Content_Types].xml"])
    except KeyError:
        raise _Refused("missing_part") from None
    defaults = {
        (el.get("Extension") or "").lower(): el.get("ContentType") or ""
        for el in types.iter("{*}Default")
    }
    overrides = {
        (el.get("PartName") or "").lower(): el.get("ContentType") or ""
        for el in types.iter("{*}Override")
    }
    for name, body in members.items():
        lowered = name.lower()
        extension = posixpath.splitext(lowered)[1].lstrip(".")
        ctype = overrides.get("/" + lowered) or defaults.get(extension, "")
        ctype = ctype.lower()
        # python-docx parses relationship parts by name, whatever their type.
        if lowered.endswith(".rels"):
            _parse_xml(body)
        # It keeps images (SVG included) as opaque bytes; only parts it parses
        # need the check, and an SVG exporter's DOCTYPE is routine.
        elif not ctype.startswith("image/") and (
            lowered.endswith(".xml") or ctype.endswith("xml")
        ):
            _parse_xml(body)


_DOCX_WRAPPERS = frozenset({"sdt", "sdtContent", "customXml", "smartTag"})


def _docx_blocks(element, tags):
    """Paragraphs and tables under ``element`` in document order.

    Content controls and custom-XML wrappers (tables of contents, cover pages,
    template fields) hold ordinary paragraphs and tables one level down.
    """
    for child in element.iterchildren():
        if child.tag in tags:
            yield child
        elif _localname(child) in _DOCX_WRAPPERS:
            yield from _docx_blocks(child, tags)


# Subtrees whose text is not the paragraph's current text: tracked deletions
# and moves, the fallback copy of alternate content, field codes, properties.
_DOCX_SKIPPED = frozenset(
    {"del", "moveFrom", "Fallback", "instrText", "delText", "pPr", "rPr"}
)


def _docx_paragraph_text(paragraph) -> str:
    """Text of a ``w:p``, including runs inside tracked insertions, content
    controls, fields, smart tags and hyperlinks (python-docx's
    ``Paragraph.text`` reads only direct runs and hyperlinks)."""
    parts: list[str] = []

    def walk(element) -> None:
        for child in element:
            name = _localname(child)
            if name in _DOCX_SKIPPED:
                continue
            if name == "t":
                parts.append(child.text or "")
            elif name in ("tab", "ptab"):
                parts.append("\t")
            elif name in ("br", "cr"):
                parts.append("\n")
            elif name == "noBreakHyphen":
                parts.append("-")
            elif name == "p":  # a text-box paragraph inside this one
                if parts:
                    parts.append("\n")
                walk(child)
                parts.append("\n")
            else:
                walk(child)

    walk(paragraph)
    return "".join(parts)


def _docx_table_text(element, parent, tags, limit: int) -> str:
    from docx.table import Table

    def cell_text(cell) -> str:
        # cell.text covers only a cell's own paragraphs; a nested table's text
        # would be lost.
        parts = []
        for block in _docx_blocks(cell._tc, tags):
            if block.tag == tags[0]:
                parts.append(_docx_paragraph_text(block))
            else:
                parts.append(_docx_table_text(block, cell, tags, limit))
        return "\n".join(part for part in parts if part.strip())

    def rows():
        for row in Table(element, parent).rows:
            cells = []
            previous = None
            for cell in row.cells:
                # A horizontally merged cell is reported once per grid column.
                if previous is not None and cell._tc is previous:
                    continue
                previous = cell._tc
                cells.append(cell_text(cell))
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
    seen = set()
    for sheet in workbook.iter("{*}sheet"):
        target = rels.get(_relationship_id(sheet) or "")
        if target is None or target[1] not in archive.NameToInfo or target[1] in seen:
            continue
        seen.add(target[1])
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
    seen = set()
    for number, slide_id in enumerate(presentation.iter("{*}sldId"), 1):
        target = rels.get(_relationship_id(slide_id) or "")
        if target is None or target[1] not in archive.NameToInfo or target[1] in seen:
            continue
        seen.add(target[1])
        slide = _parse_member(archive, target[1])
        lines = (
            "".join(
                "\n" if _localname(el) == "br" else el.text or ""
                for el in paragraph.iter("{*}t", "{*}br")
            )
            for paragraph in slide.iter("{*}p")
            if not _under_fallback(paragraph)
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
        elif name in ("annotation", "annotation-end"):
            # A comment's author and date are not paragraph text; its own
            # paragraphs are emitted by the document-order walk.
            pass
        elif child.tag not in _ODF_PARAGRAPHS:
            parts.append(_odf_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odf_paragraphs(element):
    """Paragraphs and headings under ``element`` in document order, leaving out
    the deleted text a tracked-changes region keeps."""
    tracked = f"{{{_ODF_TEXT}}}tracked-changes"
    for paragraph in element.iter(*_ODF_PARAGRAPHS):
        if next(paragraph.iterancestors(tracked), None) is None:
            yield paragraph


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
    for number, paragraph in enumerate(_odf_paragraphs(content), 1):
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
                    "\n".join(_odf_text(p) for p in _odf_paragraphs(cell))
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
        lines = (_odf_text(p) for p in _odf_paragraphs(page))
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
