"""
Content-type detection for node attachments (ADR 0008 section 5).

The type is decided from the bytes, never from the client's declared type, the
same stance ``image_ingest`` takes for images. Anything that matches no row of
the table is refused: HTML, SVG, XML, executables, bare ZIP and other archives.
"""

from __future__ import annotations

import codecs
import zipfile
from typing import BinaryIO, FrozenSet, Iterable, Optional

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
WEBP = "image/webp"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ODT = "application/vnd.oasis.opendocument.text"
ODS = "application/vnd.oasis.opendocument.spreadsheet"
ODP = "application/vnd.oasis.opendocument.presentation"
TEXT = "text/plain"
CSV = "text/csv"

SUPPORTED_TYPES: FrozenSet[str] = frozenset(
    {PDF, PNG, JPEG, WEBP, DOCX, XLSX, PPTX, ODT, ODS, ODP, TEXT, CSV}
)
TEXT_TYPES: FrozenSet[str] = frozenset({TEXT, CSV})

_OOXML_MAIN_PARTS = {
    "word/document.xml": DOCX,
    "xl/workbook.xml": XLSX,
    "ppt/presentation.xml": PPTX,
}
_ODF_TYPES = (ODT, ODS, ODP)
# The ODF mimetype member holds one of the strings above; anything much
# longer is not one of them, so it is never read in full.
_ODF_MIMETYPE_READ_LIMIT = 128
_ZIP_LOCAL_HEADER = b"PK\x03\x04"
_TEXT_READ_CHUNK = 64 * 1024
_UTF8_BOM = b"\xef\xbb\xbf"
_WHITESPACE = " \t\r\n\f\v"


class UnsupportedAttachmentType(Exception):
    """The bytes match no accepted attachment type."""


def parse_allowed_types(value: Optional[str]) -> FrozenSet[str]:
    """Parse ``ATTACHMENT_ALLOWED_TYPES``; unset or empty means every type.

    A value outside the detection table is refused, so the setting can only
    narrow the table, never re-admit a type it does not detect.
    """
    if value is None or not value.strip():
        return SUPPORTED_TYPES
    requested = {item.strip().lower() for item in value.split(",") if item.strip()}
    unknown = sorted(requested - SUPPORTED_TYPES)
    if unknown:
        raise ValueError(
            "ATTACHMENT_ALLOWED_TYPES names types the attachment detector does "
            f"not know: {', '.join(unknown)}. Supported: "
            f"{', '.join(sorted(SUPPORTED_TYPES))}."
        )
    return frozenset(requested)


def _detect_zip_type(source: BinaryIO) -> str:
    source.seek(0)
    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            matches = set()
            if "[Content_Types].xml" in names:
                matches.update(
                    kind for part, kind in _OOXML_MAIN_PARTS.items() if part in names
                )
            if "mimetype" in names:
                with archive.open("mimetype") as member:
                    declared = member.read(_ODF_MIMETYPE_READ_LIMIT + 1)
                try:
                    declared_text = declared.decode("ascii")
                except UnicodeDecodeError:
                    declared_text = ""
                if declared_text in _ODF_TYPES:
                    matches.add(declared_text)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError) as exc:
        raise UnsupportedAttachmentType("unreadable ZIP archive") from exc
    except (NotImplementedError, RuntimeError, ValueError) as exc:
        # Unsupported compression, encrypted members, malformed headers.
        raise UnsupportedAttachmentType("unreadable ZIP archive") from exc
    if len(matches) != 1:
        # None: a bare archive. Several: an ambiguous one (ADR 0008 sec 5).
        raise UnsupportedAttachmentType("archive is not a supported document")
    return matches.pop()


def _is_acceptable_text(source: BinaryIO) -> bool:
    """The whole file is UTF-8 (BOM allowed), NUL-free, not starting with '<'."""
    source.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    first_char: Optional[str] = None
    at_start = True
    while True:
        chunk = source.read(_TEXT_READ_CHUNK)
        final = not chunk
        if at_start and chunk.startswith(_UTF8_BOM):
            chunk = chunk[len(_UTF8_BOM) :]
        if chunk:
            at_start = False
        if b"\x00" in chunk:
            return False
        try:
            text = decoder.decode(chunk, final=final)
        except UnicodeDecodeError:
            return False
        if first_char is None:
            stripped = text.lstrip(_WHITESPACE)
            if stripped:
                first_char = stripped[0]
                if first_char == "<":
                    return False
        if final:
            return True


def detect_attachment_type(source: BinaryIO, filename: str) -> str:
    """Return the attachment type of ``source``, or raise UnsupportedAttachmentType.

    ``source`` must be seekable (the staged upload). ``filename`` is consulted
    only by the text rule, to tell CSV from plain text.
    """
    source.seek(0)
    head = source.read(16)
    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    if head.startswith(_ZIP_LOCAL_HEADER):
        return _detect_zip_type(source)
    if _is_acceptable_text(source):
        return CSV if filename.lower().endswith(".csv") else TEXT
    raise UnsupportedAttachmentType("content matches no accepted attachment type")


def check_allowed(content_type: str, allowed: Iterable[str]) -> None:
    if content_type not in allowed:
        raise UnsupportedAttachmentType(f"{content_type} is not allowed here")
