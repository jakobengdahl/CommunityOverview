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


def test_corrupt_deflated_member_is_refused_not_an_error():
    data = bytearray(
        _zip_deflated({"mimetype": types.ODT * 4, "content.xml": "<x/>" * 50})
    )
    # Corrupt the first member's compressed data (just after its local header).
    name_length = int.from_bytes(data[26:28], "little")
    extra_length = int.from_bytes(data[28:30], "little")
    start = 30 + name_length + extra_length
    for offset in range(start, start + 6):
        data[offset] ^= 0xFF
    with pytest.raises(UnsupportedAttachmentType):
        _detect(bytes(data), "file.odt")


def _zip_deflated(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_archive_with_an_oversized_central_directory_is_refused():
    members = {"[Content_Types].xml": "<Types/>", "word/document.xml": "<x/>"}
    # Enough empty members that the directory exceeds the 1 MiB cap.
    count = types.MAX_ZIP_CENTRAL_DIRECTORY_BYTES // 40
    members.update({f"pad/{i:08d}": "" for i in range(count)})
    data = _zip(members)
    assert types._central_directory_is_bounded(io.BytesIO(data)) is False
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "doc.docx")


def test_archive_just_under_the_directory_cap_is_still_detected():
    members = {"[Content_Types].xml": "<Types/>", "word/document.xml": "<x/>"}
    members.update({f"pad/{i:04d}": "" for i in range(1000)})
    assert _detect(_zip(members), "doc.docx") == types.DOCX


def test_zip64_archive_is_refused():
    data = _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": "<x/>"})
    assert _detect(data, "doc.docx") == types.DOCX
    # A ZIP64 end-record locator sits immediately before the end record.
    position = data.rfind(b"PK\x05\x06")
    locator = b"PK\x06\x07" + b"\x00" * 16
    zip64 = data[:position] + locator + data[position:]
    # Checked before zipfile parses anything, not left to zipfile's own error.
    assert types._central_directory_is_bounded(io.BytesIO(zip64)) is False
    with pytest.raises(UnsupportedAttachmentType):
        _detect(zip64, "doc.docx")


def test_allowed_types_naming_no_type_is_refused():
    with pytest.raises(ValueError, match="names no type"):
        parse_allowed_types(" , ")


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n" + b"\x00" * 10,  # truncated PNG signature
        b"\xff\xd8" + b"\x00" * 10,  # two-byte JPEG start only
        b"RIFF\x10\x00\x00\x00WAVEfmt ",  # RIFF, but not WebP
    ],
)
def test_near_miss_signatures_are_not_accepted_as_images(data):
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "x.bin")


def test_pdf_signature_needs_the_dash():
    # "%PDFx" is not a PDF header; as UTF-8 text it is plain text.
    assert _detect(b"%PDFx hello", "x.txt") == types.TEXT


def test_allowed_types_are_case_insensitive():
    assert parse_allowed_types("Application/PDF") == {types.PDF}


@pytest.mark.parametrize("suffix", ["\n", " "])
def test_odf_mimetype_must_match_exactly(suffix):
    data = _zip({"mimetype": types.ODT + suffix, "content.xml": "<x/>"})
    with pytest.raises(UnsupportedAttachmentType):
        _detect(data, "file.odt")


def _docx_with_directory_size(declared_size: int) -> bytes:
    """A valid DOCX whose end record claims a different central directory size."""
    data = bytearray(
        _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": "<x/>"})
    )
    position = data.rfind(b"PK\x05\x06")
    data[position + 12 : position + 16] = declared_size.to_bytes(4, "little")
    return bytes(data)


def test_directory_size_is_bounded_inclusively_at_the_cap():
    cap = types.MAX_ZIP_CENTRAL_DIRECTORY_BYTES
    assert cap == 1024 * 1024
    at_cap = _docx_with_directory_size(cap)
    over_cap = _docx_with_directory_size(cap + 1)
    assert types._central_directory_is_bounded(io.BytesIO(at_cap)) is True
    assert types._central_directory_is_bounded(io.BytesIO(over_cap)) is False


def test_archive_declaring_an_oversized_directory_never_reaches_zipfile(monkeypatch):
    over_cap = _docx_with_directory_size(1024 * 1024 + 1)

    def fail(*args, **kwargs):
        raise AssertionError("zipfile must not parse an oversized directory")

    monkeypatch.setattr(types.zipfile, "ZipFile", fail)
    with pytest.raises(UnsupportedAttachmentType, match="directory too large"):
        _detect(over_cap, "doc.docx")


def test_end_record_decoy_inside_member_data_does_not_decide_the_bound():
    # A stored member holds bytes shaped like an end record that declares a huge
    # directory. The real end record, the last one, is the one that counts.
    decoy = bytearray(b"PK\x05\x06" + b"\x00" * 18)
    decoy[12:16] = (0x7FFFFFFF).to_bytes(4, "little")
    data = _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": bytes(decoy)})
    assert data.find(b"PK\x05\x06") < data.rfind(b"PK\x05\x06")
    assert _detect(data, "doc.docx") == types.DOCX


def test_document_with_the_longest_zip_comment_is_still_detected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<x/>")
        archive.comment = b"c" * 0xFFFF
    assert _detect(buffer.getvalue(), "doc.docx") == types.DOCX
