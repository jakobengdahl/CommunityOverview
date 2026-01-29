"""
DocumentService - Handles document uploads and text extraction.

This module provides document processing functionality by:
- Using DocumentProcessor for text extraction (PDF, Word, text)
- Integrating with ChatService for document analysis
- Managing temporary file uploads

All graph mutations resulting from document analysis
go through ChatService -> GraphService.
"""

import os
import tempfile
import shutil
from typing import Optional, Dict, Any, BinaryIO
from pathlib import Path

# Import from parent directory (legacy module)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from document_processor import DocumentProcessor


class DocumentService:
    """
    Service for handling document uploads and text extraction.

    This class provides:
    - Text extraction from PDF, Word, and text files
    - Temporary file management for uploads
    - Integration with ChatService for analysis

    Note: This service does NOT create graph nodes directly.
    Document analysis and node creation is handled by ChatService.
    """

    # Supported file extensions
    SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.doc', '.txt'}

    # Max file size (10 MB)
    MAX_FILE_SIZE = 10 * 1024 * 1024

    def __init__(self, upload_dir: Optional[str] = None):
        """
        Initialize DocumentService.

        Args:
            upload_dir: Optional directory for temporary uploads.
                       If None, uses system temp directory.
        """
        self._upload_dir = upload_dir or tempfile.gettempdir()
        Path(self._upload_dir).mkdir(parents=True, exist_ok=True)

    def extract_text_from_file(self, file_path: str) -> Dict[str, Any]:
        """
        Extract text from a file.

        Args:
            file_path: Path to the file

        Returns:
            Dict with:
            - success: bool
            - text: Extracted text (if successful)
            - filename: Original filename
            - error: Error message (if failed)
        """
        path = Path(file_path)

        # Validate extension
        if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            return {
                "success": False,
                "error": f"Unsupported file format: {path.suffix}. Supported: {', '.join(self.SUPPORTED_EXTENSIONS)}",
                "filename": path.name
            }

        # Validate file exists
        if not path.exists():
            return {
                "success": False,
                "error": f"File not found: {file_path}",
                "filename": path.name
            }

        # Extract text
        try:
            text = DocumentProcessor.extract_text(str(path))
            return {
                "success": True,
                "text": text,
                "filename": path.name,
                "char_count": len(text),
                "word_count": len(text.split())
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Error extracting text: {str(e)}",
                "filename": path.name
            }

    async def save_upload(
        self,
        file_content: bytes,
        filename: str
    ) -> Dict[str, Any]:
        """
        Save an uploaded file to temporary storage.

        Args:
            file_content: The file content as bytes
            filename: Original filename

        Returns:
            Dict with:
            - success: bool
            - file_path: Path to saved file (if successful)
            - error: Error message (if failed)
        """
        # Validate extension
        ext = Path(filename).suffix.lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            return {
                "success": False,
                "error": f"Unsupported file format: {ext}. Supported: {', '.join(self.SUPPORTED_EXTENSIONS)}"
            }

        # Validate size
        if len(file_content) > self.MAX_FILE_SIZE:
            return {
                "success": False,
                "error": f"File too large. Max size: {self.MAX_FILE_SIZE / 1024 / 1024:.1f} MB"
            }

        # Create safe filename
        safe_filename = self._sanitize_filename(filename)

        # Save to temp directory
        try:
            file_path = Path(self._upload_dir) / safe_filename
            with open(file_path, 'wb') as f:
                f.write(file_content)

            return {
                "success": True,
                "file_path": str(file_path),
                "filename": filename,
                "size": len(file_content)
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Error saving file: {str(e)}"
            }

    async def process_upload(
        self,
        file_content: bytes,
        filename: str
    ) -> Dict[str, Any]:
        """
        Save and extract text from an uploaded file.

        This is a convenience method that combines save_upload
        and extract_text_from_file.

        Args:
            file_content: The file content as bytes
            filename: Original filename

        Returns:
            Dict with extraction results
        """
        # Save the file
        save_result = await self.save_upload(file_content, filename)
        if not save_result["success"]:
            return save_result

        # Extract text
        file_path = save_result["file_path"]
        extract_result = self.extract_text_from_file(file_path)

        # Clean up temp file
        try:
            os.remove(file_path)
        except Exception:
            pass  # Ignore cleanup errors

        return extract_result

    def _sanitize_filename(self, filename: str) -> str:
        """
        Create a safe filename for storage.

        Args:
            filename: Original filename

        Returns:
            Sanitized filename with timestamp prefix
        """
        import time
        import re

        # Remove path components
        name = Path(filename).name

        # Replace unsafe characters
        safe_name = re.sub(r'[^\w\-_\.]', '_', name)

        # Add timestamp prefix for uniqueness
        timestamp = int(time.time() * 1000)

        return f"{timestamp}_{safe_name}"

    def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """
        Clean up old temporary files.

        Args:
            max_age_hours: Maximum file age in hours

        Returns:
            Number of files deleted
        """
        import time

        deleted = 0
        max_age_seconds = max_age_hours * 3600
        now = time.time()

        upload_path = Path(self._upload_dir)
        for file_path in upload_path.iterdir():
            if file_path.is_file():
                try:
                    age = now - file_path.stat().st_mtime
                    if age > max_age_seconds:
                        file_path.unlink()
                        deleted += 1
                except Exception:
                    pass

        return deleted
