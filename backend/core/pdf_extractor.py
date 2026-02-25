"""
PDF text extraction using pypdf.

Replaces the previous PyMuPDF (fitz) dependency with the permissively-licensed
pypdf library for extracting text content from PDF files.
"""

from typing import Optional


def extract_text_from_pdf_path(path: str) -> str:
    """
    Extract text from a PDF file on disk.

    Args:
        path: Filesystem path to the PDF file.

    Returns:
        Concatenated text from all pages, separated by double newlines.

    Raises:
        FileNotFoundError: If the file does not exist.
        Exception: If the PDF cannot be parsed (corrupt, encrypted, etc.).
    """
    with open(path, "rb") as f:
        return extract_text_from_pdf(f.read())


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extract text from raw PDF bytes.

    Args:
        pdf_bytes: The PDF file content as bytes.

    Returns:
        Concatenated text from all pages, separated by double newlines.
        Returns empty string if no text could be extracted.

    Raises:
        Exception: If the PDF is fundamentally unreadable.
    """
    from pypdf import PdfReader
    import io

    reader = PdfReader(io.BytesIO(pdf_bytes))

    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)

    return "\n\n".join(pages)
