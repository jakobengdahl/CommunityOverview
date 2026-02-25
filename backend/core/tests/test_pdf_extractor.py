"""
Tests for the pypdf-based PDF text extractor.
"""

import io
import os
import tempfile

import pytest

from backend.core.pdf_extractor import extract_text_from_pdf, extract_text_from_pdf_path


def _make_minimal_pdf(text: str = "Hello PDF World") -> bytes:
    """
    Build a minimal single-page PDF containing *text* using only stdlib.

    The PDF is ~200 bytes and contains a single BT/ET text block rendered
    in Helvetica so that pypdf can extract it reliably.
    """
    # Build the content stream
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    stream_bytes = stream.encode("latin-1")

    objects: list[bytes] = []
    offsets: list[int] = []

    def _add(obj: bytes) -> int:
        idx = len(objects) + 1
        offsets.append(0)  # placeholder
        objects.append(obj)
        return idx

    catalog_id = _add(b"")  # placeholder
    pages_id = _add(b"")
    page_id = _add(b"")
    font_id = _add(b"")
    contents_id = _add(b"")

    objects[catalog_id - 1] = f"{catalog_id} 0 obj\n<< /Type /Catalog /Pages {pages_id} 0 R >>\nendobj\n".encode()
    objects[pages_id - 1] = f"{pages_id} 0 obj\n<< /Type /Pages /Kids [{page_id} 0 R] /Count 1 >>\nendobj\n".encode()
    objects[page_id - 1] = (
        f"{page_id} 0 obj\n"
        f"<< /Type /Page /Parent {pages_id} 0 R "
        f"/MediaBox [0 0 612 792] "
        f"/Contents {contents_id} 0 R "
        f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>\n"
        f"endobj\n"
    ).encode()
    objects[font_id - 1] = f"{font_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n".encode()
    objects[contents_id - 1] = (
        f"{contents_id} 0 obj\n"
        f"<< /Length {len(stream_bytes)} >>\n"
        f"stream\n".encode()
        + stream_bytes
        + b"\nendstream\nendobj\n"
    )

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    for i, obj in enumerate(objects):
        offsets[i] = buf.tell()
        buf.write(obj)

    xref_offset = buf.tell()
    buf.write(b"xref\n")
    buf.write(f"0 {len(objects) + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())

    buf.write(b"trailer\n")
    buf.write(f"<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n".encode())
    buf.write(b"startxref\n")
    buf.write(f"{xref_offset}\n".encode())
    buf.write(b"%%EOF\n")

    return buf.getvalue()


class TestExtractTextFromPdf:
    """Tests for extract_text_from_pdf (bytes input)."""

    def test_extracts_known_text(self):
        """A minimal PDF containing known text should be extracted."""
        pdf_bytes = _make_minimal_pdf("Community Knowledge Graph")
        result = extract_text_from_pdf(pdf_bytes)
        assert "Community Knowledge Graph" in result

    def test_returns_string(self):
        pdf_bytes = _make_minimal_pdf("test")
        result = extract_text_from_pdf(pdf_bytes)
        assert isinstance(result, str)

    def test_empty_pdf_returns_empty_string(self):
        """A PDF with no text objects should yield an empty string."""
        # Build a PDF with an empty content stream
        pdf_bytes = _make_minimal_pdf("")
        result = extract_text_from_pdf(pdf_bytes)
        # Empty or whitespace-only is acceptable
        assert result.strip() == "" or result == ""

    def test_corrupt_pdf_raises(self):
        """Random bytes should raise an exception."""
        with pytest.raises(Exception):
            extract_text_from_pdf(b"this is not a pdf")


class TestExtractTextFromPdfPath:
    """Tests for extract_text_from_pdf_path (file path input)."""

    def test_reads_from_file(self, tmp_path):
        pdf_file = tmp_path / "sample.pdf"
        pdf_file.write_bytes(_make_minimal_pdf("File based extraction"))
        result = extract_text_from_pdf_path(str(pdf_file))
        assert "File based extraction" in result

    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            extract_text_from_pdf_path("/nonexistent/path/file.pdf")
