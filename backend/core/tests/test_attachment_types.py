"""Content-type detection for node attachments (ADR 0008 section 5)."""

import io
import zipfile

import pytest

from backend.core import attachment_types as types
from backend.core.attachment_types import (
    UnsupportedAttachmentType,
    detect_attachment_type,
    parse_allowed_types,
)


def _zip(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _detect(data: bytes, filename: str = "file.bin") -> str:
    return detect_attachment_type(io.BytesIO(data), filename)


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"%PDF-1.7\n...", types.PDF),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, types.PNG),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, types.JPEG),
        (b"RIFF\x10\x00\x00\x00WEBPVP8 ", types.WEBP),
    ],
)
def test_signature_types_are_detected_from_their_leading_bytes(data, expected):
    assert _detect(data, "misleading.txt") == expected


@pytest.mark.parametrize(
    "main_part, expected",
    [
        ("word/document.xml", types.DOCX),
        ("xl/workbook.xml", types.XLSX),
        ("ppt/presentation.xml", types.PPTX),
    ],
)
def test_ooxml_type_is_decided_by_the_archive_not_the_extension(main_part, expected):
    data = _zip({"[Content_Types].xml": "<Types/>", main_part: "<x/>"})
    assert _detect(data, "renamed.pdf") == expected


def test_ooxml_archive_with_two_main_parts_is_refused():
    data = _zip(
        {
            "[Content_Types].xml": "<Types/>",
            "word/document.xml": "<x/>",
            "xl/workbook.xml": "<x/>",
        }
    )
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "doc.docx")


def test_ooxml_main_part_without_content_types_is_a_bare_archive():
    data = _zip({"word/document.xml": "<x/>"})
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "doc.docx")


@pytest.mark.parametrize("mimetype", [types.ODT, types.ODS, types.ODP])
def test_odf_type_is_read_from_the_mimetype_member(mimetype):
    data = _zip({"mimetype": mimetype, "content.xml": "<x/>"})
    assert _detect(data, "file.zip") == mimetype


def test_odf_mimetype_member_with_another_value_is_refused():
    data = _zip({"mimetype": types.ODT + "x", "content.xml": "<x/>"})
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "file.odt")


def test_bare_zip_is_refused():
    with pytest.raises(UnsupportedAttachmentType):
        _detect(_zip({"readme.txt": "hello"}), "archive.zip")


def test_corrupt_zip_is_refused():
    with pytest.raises(UnsupportedAttachmentType):
        _detect(b"PK\x03\x04" + b"garbage" * 10, "file.docx")


def test_text_with_csv_extension_is_csv_and_otherwise_plain():
    assert _detect(b"a,b\n1,2\n", "Data.CSV") == types.CSV
    assert _detect(b"a,b\n1,2\n", "notes.md") == types.TEXT


def test_text_with_bom_and_leading_whitespace_is_accepted():
    assert _detect(b"\xef\xbb\xbf  \n hello", "a.txt") == types.TEXT


def test_empty_file_is_plain_text():
    assert _detect(b"", "empty.txt") == types.TEXT


@pytest.mark.parametrize(
    "data",
    [
        b"<html><script>alert(1)</script></html>",
        b"  \n<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"\xef\xbb\xbf<?xml version='1.0'?><a/>",
    ],
)
def test_markup_starting_with_angle_bracket_is_refused(data):
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "page.txt")


def test_text_with_a_nul_byte_is_refused():
    with pytest.raises(UnsupportedAttachmentType):
        _detect(b"hello\x00world", "a.txt")


def test_non_utf8_text_is_refused_even_late_in_the_file():
    data = b"a" * (200 * 1024) + "é".encode("latin-1")
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "a.txt")


def test_utf8_sequence_split_across_read_chunks_is_accepted():
    # 64 KiB read chunks: place a two-byte character across the boundary.
    data = b"a" * (64 * 1024 - 1) + "é".encode("utf-8") + b"tail"
    assert _detect(data, "a.txt") == types.TEXT


def test_executable_is_refused():
    with pytest.raises(UnsupportedAttachmentType):
        _detect(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00", "setup.exe")


def test_allowed_types_unset_means_every_supported_type():
    assert parse_allowed_types(None) == types.SUPPORTED_TYPES
    assert parse_allowed_types("  ") == types.SUPPORTED_TYPES


def test_allowed_types_narrow_the_table():
    assert parse_allowed_types("application/pdf, text/plain") == {
        types.PDF,
        types.TEXT,
    }


def test_allowed_types_outside_the_table_are_refused():
    with pytest.raises(ValueError, match="text/html"):
        parse_allowed_types("application/pdf,text/html")
