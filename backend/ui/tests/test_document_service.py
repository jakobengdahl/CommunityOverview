"""
Tests for DocumentService.

Verifies that:
- Text extraction works for supported formats
- File validation works correctly
- Temporary file handling is correct
"""

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
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
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

        result = asyncio.run(document_service.save_upload(content, filename))

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

        result = asyncio.run(document_service.save_upload(content, filename))

        assert not result["success"]
        assert "unsupported" in result["error"].lower()

    def test_save_upload_rejection_names_the_uploaded_file(self, document_service):
        """Each save_upload error path reports the display name of the rejected file."""
        import asyncio
        from unittest.mock import patch

        unsupported = asyncio.run(
            document_service.save_upload(b"x", "C:\\docs\\bad\u202e.xyz")
        )
        too_large = asyncio.run(
            document_service.save_upload(
                b"x" * (document_service.MAX_FILE_SIZE + 1), "../big\x85.txt"
            )
        )
        with patch("builtins.open", side_effect=OSError("disk full")):
            write_failed = asyncio.run(
                document_service.save_upload(b"x", "dir/notes .txt")
            )

        assert not unsupported["success"]
        assert "unsupported" in unsupported["error"].lower()
        assert unsupported["filename"] == "bad.xyz"
        assert not too_large["success"]
        assert "too large" in too_large["error"].lower()
        assert too_large["filename"] == "big.txt"
        assert not write_failed["success"]
        assert "disk full" in write_failed["error"]
        assert write_failed["filename"] == "notes .txt"

    def test_save_upload_rejects_large_files(self, document_service):
        """save_upload should reject files exceeding size limit."""
        import asyncio

        # Create content larger than max size
        content = b"x" * (document_service.MAX_FILE_SIZE + 1)
        filename = "large.txt"

        result = asyncio.run(document_service.save_upload(content, filename))

        assert not result["success"]
        assert "too large" in result["error"].lower()

    def test_process_upload_extracts_and_cleans_up(self, document_service):
        """process_upload should extract text and clean up temp file."""
        import asyncio

        content = b"This is test content for extraction."
        filename = "test_extract.txt"

        result = asyncio.run(document_service.process_upload(content, filename))

        assert result["success"]
        assert "test content" in result["text"].lower()
        assert os.listdir(document_service._upload_dir) == []

    def test_process_upload_removes_the_stored_file_when_extraction_fails(
        self, document_service
    ):
        import asyncio
        from unittest.mock import patch

        with patch(
            "backend.ui.document_service.DocumentProcessor.extract_text",
            side_effect=ValueError("broken"),
        ):
            result = asyncio.run(document_service.process_upload(b"x", "report.txt"))

        assert not result["success"]
        assert os.listdir(document_service._upload_dir) == []

    def test_process_upload_stores_under_the_sanitized_name_in_the_upload_dir(
        self, document_service
    ):
        """The file is written inside _upload_dir under a timestamped, sanitized
        basename, whatever path or characters the client sent."""
        import asyncio
        import re
        from unittest.mock import patch

        for sent in ("../../x.txt", "Mötes anteckningar (v2).txt", "a\\b c.txt"):
            seen = []

            def capture(path, _seen=seen):
                _seen.append(path)
                assert os.path.isfile(path)
                return {"success": True, "text": "t", "filename": "ignored"}

            with patch.object(
                document_service, "extract_text_from_file", side_effect=capture
            ):
                result = asyncio.run(document_service.process_upload(b"x", sent))

            assert result["success"], sent
            (stored,) = seen
            assert Path(stored).parent == Path(document_service._upload_dir), sent
            assert re.fullmatch(r"\d+_[\w.\-]+", Path(stored).name), sent
            assert os.listdir(document_service._upload_dir) == [], sent

    def test_process_upload_reports_the_uploaded_name_not_the_storage_name(
        self, document_service
    ):
        """The chat shows this name and sends it to the LLM, so it must not carry
        the timestamp prefix or the character replacements of the storage name."""
        import asyncio

        result = asyncio.run(
            document_service.process_upload(
                b"Some text.", "Mötes anteckningar (v2).txt"
            )
        )

        assert result["success"]
        assert result["filename"] == "Mötes anteckningar (v2).txt"

    def test_process_upload_strips_client_path_and_control_characters(
        self, document_service
    ):
        import asyncio

        cases = {
            "C:\\Users\\me\\notes.txt": "notes.txt",
            "../../etc/notes.txt": "notes.txt",
            "no\ntes\x07.txt": "notes.txt",
            "notes.txt/": "notes.txt",
            "notes.txt/.": "notes.txt",
            "notes.txt/./": "notes.txt",
            "  notes.txt": "  notes.txt",
            "no\x7ftes\x85\x9b.txt": "notes.txt",
            "evil\u202etxt.exe.txt": "eviltxt.exe.txt",
            "a:b.txt": "a:b.txt",
        }
        for sent, shown in cases.items():
            result = asyncio.run(document_service.process_upload(b"Some text.", sent))
            assert result["success"], sent
            assert result["filename"] == shown, sent

    def test_process_upload_failure_reports_the_uploaded_name(self, document_service):
        """An extraction error names the user's file, not the storage file."""
        import asyncio
        from unittest.mock import patch

        with patch(
            "backend.ui.document_service.DocumentProcessor.extract_text",
            side_effect=ValueError("broken"),
        ):
            result = asyncio.run(document_service.process_upload(b"x", "report.txt"))

        assert not result["success"]
        assert result["filename"] == "report.txt"


class TestDocumentServiceFileSanitization:
    """Tests for filename sanitization."""

    def test_sanitize_filename_removes_path(self, document_service):
        """Should remove path components from filename."""
        result = document_service._sanitize_filename("/path/to/file.txt")
        assert "/" not in result or result.count("/") == 0
        assert "file" in result

    def test_sanitize_filename_replaces_unsafe_chars(self, document_service):
        """Should replace unsafe characters."""
        result = document_service._sanitize_filename('file<>:"|?*.txt')
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
        assert result2[0].isdigit()


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
