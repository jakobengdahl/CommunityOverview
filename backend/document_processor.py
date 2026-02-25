import os
from typing import Optional
import docx

from backend.core.pdf_extractor import extract_text_from_pdf_path

class DocumentProcessor:
    """Handles text extraction from PDF and Word documents"""

    @staticmethod
    def extract_text(file_path: str) -> str:
        """
        Extract text from a file based on extension.

        Args:
            file_path: Path to the file

        Returns:
            Extracted text content

        Raises:
            ValueError: If file format is not supported
        """
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        if ext == '.pdf':
            return DocumentProcessor.parse_pdf(file_path)
        elif ext in ['.docx', '.doc']:
            return DocumentProcessor.parse_docx(file_path)
        elif ext == '.txt':
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        else:
            raise ValueError(f"Unsupported file format: {ext}")

    @staticmethod
    def parse_pdf(file_path: str) -> str:
        """Extract text from PDF using pypdf."""
        try:
            return extract_text_from_pdf_path(file_path)
        except Exception as e:
            raise Exception(f"Error parsing PDF: {str(e)}")

    @staticmethod
    def parse_docx(file_path: str) -> str:
        """Extract text from Word document"""
        try:
            doc = docx.Document(file_path)
            text = [paragraph.text for paragraph in doc.paragraphs]
            return "\n".join(text)
        except Exception as e:
            raise Exception(f"Error parsing Word document: {str(e)}")
