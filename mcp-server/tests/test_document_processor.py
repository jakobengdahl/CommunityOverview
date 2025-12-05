import pytest
import os
import tempfile
from document_processor import DocumentProcessor
import fitz
import docx

@pytest.fixture
def sample_pdf():
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Hello from PDF")
        doc.save(f.name)
        doc.close()
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)

@pytest.fixture
def sample_docx():
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
        path = f.name

    # docx library needs to save to a path, can't write to open handle easily
    doc = docx.Document()
    doc.add_paragraph("Hello from DOCX")
    doc.save(path)

    yield path
    if os.path.exists(path):
        os.remove(path)

@pytest.fixture
def sample_txt():
    with tempfile.NamedTemporaryFile(suffix='.txt', delete=False, mode='w') as f:
        f.write("Hello from TXT")
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)

def test_extract_from_pdf(sample_pdf):
    text = DocumentProcessor.extract_text(sample_pdf)
    assert "Hello from PDF" in text

def test_extract_from_docx(sample_docx):
    text = DocumentProcessor.extract_text(sample_docx)
    assert "Hello from DOCX" in text

def test_extract_from_txt(sample_txt):
    text = DocumentProcessor.extract_text(sample_txt)
    assert "Hello from TXT" in text

def test_unsupported_format():
    with tempfile.NamedTemporaryFile(suffix='.xyz', delete=False) as f:
        path = f.name
    try:
        with pytest.raises(ValueError, match="Unsupported file format"):
            DocumentProcessor.extract_text(path)
    finally:
        if os.path.exists(path):
            os.remove(path)
