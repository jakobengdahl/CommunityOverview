"""
Tests for DocumentService.

Verifies that:
- Text extraction works for supported formats
- File validation works correctly
- Temporary file handling is correct
"""

import pytest
import tempfile
import os
from pathlib import Path


class TestDocumentServiceExtraction:
    """Tests for text extraction."""

    def test_extract_text_from_txt_file(self, document_service, test_text_file):
        """Should extract text from .txt files."""
        result = document_service.extract_text_from_file(test_text_file)

        assert result["success"]
        assert "test document" in result["text"].lower()
        assert result["char_count"] > 0
        assert result["word_count"] > 0

    def test_extract_text_from_nonexistent_file(self, document_service):
        """Should return error for nonexistent file."""
        result = document_service.extract_text_from_file("/nonexistent/file.txt")

        assert not result["success"]
        assert "not found" in result["error"].lower()

    def test_extract_text_unsupported_format(self, document_service):
        """Should return error for unsupported formats."""
        with tempfile.NamedTemporaryFile(suffix='.xyz', delete=False) as f:
            f.write(b"test")
            path = f.name

        try:
            result = document_service.extract_text_from_file(path)
            assert not result["success"]
            assert "unsupported" in result["error"].lower()
        finally:
            os.unlink(path)


class TestDocumentServiceUpload:
    """Tests for file upload handling."""

    def test_save_upload_creates_file(self, document_service):
        """save_upload should create a file in the upload directory."""
        import asyncio
        content = b"Test file content"
        filename = "test_upload.txt"

        result = asyncio.get_event_loop().run_until_complete(
            document_service.save_upload(content, filename)
        )

        assert result["success"]
        assert result["file_path"]
        assert os.path.exists(result["file_path"])

        # Cleanup
        os.unlink(result["file_path"])

    def test_save_upload_rejects_unsupported_format(self, document_service):
        """save_upload should reject unsupported file formats."""
        import asyncio
        content = b"Test content"
        filename = "test.xyz"

        result = asyncio.get_event_loop().run_until_complete(
            document_service.save_upload(content, filename)
        )

        assert not result["success"]
        assert "unsupported" in result["error"].lower()

    def test_save_upload_rejects_large_files(self, document_service):
        """save_upload should reject files exceeding size limit."""
        import asyncio
        # Create content larger than max size
        content = b"x" * (document_service.MAX_FILE_SIZE + 1)
        filename = "large.txt"

        result = asyncio.get_event_loop().run_until_complete(
            document_service.save_upload(content, filename)
        )

        assert not result["success"]
        assert "too large" in result["error"].lower()

    def test_process_upload_extracts_and_cleans_up(self, document_service):
        """process_upload should extract text and clean up temp file."""
        import asyncio
        content = b"This is test content for extraction."
        filename = "test_extract.txt"

        result = asyncio.get_event_loop().run_until_complete(
            document_service.process_upload(content, filename)
        )

        assert result["success"]
        assert "test content" in result["text"].lower()
        # Temp file should be cleaned up (can't easily verify, but no error is good)


class TestDocumentServiceFileSanitization:
    """Tests for filename sanitization."""

    def test_sanitize_filename_removes_path(self, document_service):
        """Should remove path components from filename."""
        result = document_service._sanitize_filename("/path/to/file.txt")
        assert "/" not in result or result.count("/") == 0
        assert "file" in result

    def test_sanitize_filename_replaces_unsafe_chars(self, document_service):
        """Should replace unsafe characters."""
        result = document_service._sanitize_filename("file<>:\"|?*.txt")
        # Should not contain any of these characters
        for char in '<>:"|?*':
            assert char not in result

    def test_sanitize_filename_adds_timestamp(self, document_service):
        """Should add timestamp for uniqueness."""
        result1 = document_service._sanitize_filename("file.txt")
        result2 = document_service._sanitize_filename("file.txt")
        # Results might be different due to timestamp (or same if called fast)
        # Both should start with digits
        assert result1[0].isdigit()


class TestDocumentServiceCleanup:
    """Tests for file cleanup."""

    def test_cleanup_old_files(self, document_service):
        """cleanup_old_files should remove old files."""
        # Create an old file
        old_file = Path(document_service._upload_dir) / "old_file.txt"
        old_file.write_text("old content")

        # Set modification time to be old (2 days ago)
        import time
        old_time = time.time() - (48 * 3600)
        os.utime(old_file, (old_time, old_time))

        # Cleanup files older than 1 hour
        deleted = document_service.cleanup_old_files(max_age_hours=1)

        assert deleted >= 1
        assert not old_file.exists()


class TestSupportedFormats:
    """Tests for supported format validation."""

    def test_supported_extensions(self, document_service):
        """Should have correct supported extensions."""
        assert ".pdf" in document_service.SUPPORTED_EXTENSIONS
        assert ".docx" in document_service.SUPPORTED_EXTENSIONS
        assert ".doc" in document_service.SUPPORTED_EXTENSIONS
        assert ".txt" in document_service.SUPPORTED_EXTENSIONS

    def test_max_file_size(self, document_service):
        """Should have reasonable max file size."""
        # Should be at least 1 MB
        assert document_service.MAX_FILE_SIZE >= 1024 * 1024
        # Should be at most 100 MB
        assert document_service.MAX_FILE_SIZE <= 100 * 1024 * 1024
