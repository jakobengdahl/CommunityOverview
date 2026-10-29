"""
Unit tests for backend.service.graph_archive — building and extracting the
vector-aware export/import archive (ZIP + manifest), independent of
GraphStorage. See docs/adr/0007-vector-aware-export-archive.md.
"""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np
import pytest

from backend.service.graph_archive import (
    EMBEDDINGS_MEMBER,
    GRAPH_MEMBER,
    MANIFEST_MEMBER,
    ArchiveIntegrityError,
    build_archive_bytes,
    extract_archive,
)


def _document() -> dict:
    return {
        "version": "1.0",
        "nodes": [
            {"id": "n1", "type": "Actor", "name": "Alice"},
            {"id": "n2", "type": "Actor", "name": "Bob"},
        ],
        "edges": [],
        "total_nodes": 2,
        "total_edges": 0,
    }


def _vectors() -> dict:
    return {
        "n1": np.array([1.0, 0.0, -0.5], dtype=np.float32),
        "n2": np.array([0.25, 0.75, 1.5], dtype=np.float32),
    }


class TestBuildAndExtractRoundTrip:
    def test_compatible_archive_round_trips_vectors_exactly(self):
        archive_bytes = build_archive_bytes(
            _document(), embedding_vectors=_vectors(), embedding_model="test-model"
        )

        extracted = extract_archive(archive_bytes, live_model_name="test-model")

        assert extracted.compatible is True
        assert extracted.regeneration_reason is None
        assert extracted.embedding_model == "test-model"
        assert extracted.embedding_dimension == 3
        assert set(extracted.embedding_vectors) == {"n1", "n2"}
        for node_id, expected in _vectors().items():
            np.testing.assert_array_equal(
                extracted.embedding_vectors[node_id], expected
            )
        assert extracted.graph_document["nodes"] == _document()["nodes"]

    def test_manifest_carries_schema_version_checksums_and_counts(self):
        archive_bytes = build_archive_bytes(
            _document(), embedding_vectors=_vectors(), embedding_model="test-model"
        )
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            manifest = json.loads(zf.read(MANIFEST_MEMBER))

        assert manifest["schema_version"] == "1.0"
        assert manifest["embedding_model"] == "test-model"
        assert manifest["embedding_dimension"] == 3
        assert manifest["node_count"] == 2
        assert manifest["edge_count"] == 0
        assert set(manifest["checksums"]) == {GRAPH_MEMBER, EMBEDDINGS_MEMBER}
        assert all(v.startswith("sha256:") for v in manifest["checksums"].values())


class TestNoVectorsProducesGraphOnlyArchive:
    def test_no_embeddings_bin_when_there_are_no_vectors_to_export(self):
        archive_bytes = build_archive_bytes(
            _document(), embedding_vectors={}, embedding_model="test-model"
        )
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            names = set(zf.namelist())
        assert names == {GRAPH_MEMBER, MANIFEST_MEMBER}

    def test_extracting_it_still_imports_the_graph_and_asks_for_regeneration(self):
        archive_bytes = build_archive_bytes(
            _document(), embedding_vectors={}, embedding_model="test-model"
        )

        extracted = extract_archive(archive_bytes, live_model_name="test-model")

        assert extracted.graph_document["nodes"] == _document()["nodes"]
        assert extracted.compatible is False
        assert extracted.embedding_vectors is None
        assert "no embeddings.bin" in extracted.regeneration_reason


class TestIncompatibleModelFallsBackGracefully:
    def test_a_different_embedding_model_is_reported_as_incompatible(self):
        archive_bytes = build_archive_bytes(
            _document(), embedding_vectors=_vectors(), embedding_model="model-a"
        )

        extracted = extract_archive(archive_bytes, live_model_name="model-b")

        assert extracted.graph_document["nodes"] == _document()["nodes"]
        assert extracted.compatible is False
        assert extracted.embedding_vectors is None
        assert "model-a" in extracted.regeneration_reason
        assert "model-b" in extracted.regeneration_reason


class TestMissingOrUnreadableManifestFallsBackGracefully:
    def _archive_without_manifest(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, json.dumps(_document()))
        return buffer.getvalue()

    def test_no_manifest_member_at_all_still_imports_the_graph(self):
        """The 'someone zipped a plain graph.json export themselves' case —
        legitimate, not an error."""
        extracted = extract_archive(
            self._archive_without_manifest(), live_model_name="test-model"
        )

        assert extracted.graph_document["nodes"] == _document()["nodes"]
        assert extracted.compatible is False
        assert "no manifest.json" in extracted.regeneration_reason

    def test_unreadable_manifest_json_still_imports_the_graph(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, json.dumps(_document()))
            zf.writestr(MANIFEST_MEMBER, "{not valid json")
        archive_bytes = buffer.getvalue()

        extracted = extract_archive(archive_bytes, live_model_name="test-model")

        assert extracted.graph_document["nodes"] == _document()["nodes"]
        assert extracted.compatible is False
        assert "manifest.json" in extracted.regeneration_reason

    def test_manifest_missing_required_fields_still_imports_the_graph(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, json.dumps(_document()))
            zf.writestr(MANIFEST_MEMBER, json.dumps({"schema_version": "1.0"}))
        archive_bytes = buffer.getvalue()

        extracted = extract_archive(archive_bytes, live_model_name="test-model")

        assert extracted.compatible is False
        assert extracted.graph_document["nodes"] == _document()["nodes"]


class TestEmptyEmbeddingsMemberDegradesGracefully:
    def test_a_zero_row_embeddings_bin_is_treated_as_no_vectors(self):
        from backend.core.embedding_sidecar import serialize_sidecar_bytes

        buffer = io.BytesIO()
        graph_bytes = json.dumps(_document()).encode("utf-8")
        embeddings_bytes = serialize_sidecar_bytes({})
        import hashlib

        manifest = {
            "schema_version": "1.0",
            "embedding_model": "test-model",
            "embedding_dimension": None,
            "checksums": {
                GRAPH_MEMBER: f"sha256:{hashlib.sha256(graph_bytes).hexdigest()}",
                EMBEDDINGS_MEMBER: f"sha256:{hashlib.sha256(embeddings_bytes).hexdigest()}",
            },
        }
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, graph_bytes)
            zf.writestr(EMBEDDINGS_MEMBER, embeddings_bytes)
            zf.writestr(MANIFEST_MEMBER, json.dumps(manifest))

        extracted = extract_archive(buffer.getvalue(), live_model_name="test-model")

        assert extracted.compatible is False
        assert "no vectors" in extracted.regeneration_reason


class TestCorruptOrTamperedArchivesAreRejectedBeforeAnyWrite:
    def test_bytes_that_are_not_a_zip_at_all_are_rejected(self):
        with pytest.raises(ArchiveIntegrityError, match="not a valid ZIP archive"):
            extract_archive(b"this is not a zip file", live_model_name="test-model")

    def test_archive_missing_graph_json_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("something-else.json", "{}")

        with pytest.raises(ArchiveIntegrityError, match=GRAPH_MEMBER):
            extract_archive(buffer.getvalue(), live_model_name="test-model")

    def test_a_tampered_graph_json_fails_its_checksum_and_is_rejected(self):
        archive_bytes = build_archive_bytes(
            _document(), embedding_vectors=_vectors(), embedding_model="test-model"
        )
        tampered = _rewrite_member(
            archive_bytes, GRAPH_MEMBER, json.dumps({"nodes": [], "edges": []}).encode()
        )

        with pytest.raises(ArchiveIntegrityError, match="checksum"):
            extract_archive(tampered, live_model_name="test-model")

    def test_a_tampered_embeddings_bin_fails_its_checksum_and_rejects_the_whole_archive(
        self,
    ):
        """Even though graph.json itself is untouched, the whole import must
        be rejected — a checksum failure anywhere the manifest promised one is
        never silently downgraded to 'just regenerate embeddings'."""
        archive_bytes = build_archive_bytes(
            _document(), embedding_vectors=_vectors(), embedding_model="test-model"
        )
        tampered = _rewrite_member(archive_bytes, EMBEDDINGS_MEMBER, b"corrupted bytes")

        with pytest.raises(ArchiveIntegrityError, match="checksum"):
            extract_archive(tampered, live_model_name="test-model")

    def test_manifest_declaring_a_checksum_for_a_missing_member_is_rejected(self):
        buffer = io.BytesIO()
        graph_bytes = json.dumps(_document()).encode("utf-8")
        import hashlib

        manifest = {
            "schema_version": "1.0",
            "embedding_model": "test-model",
            "embedding_dimension": 3,
            "checksums": {
                GRAPH_MEMBER: f"sha256:{hashlib.sha256(graph_bytes).hexdigest()}",
                EMBEDDINGS_MEMBER: "sha256:" + "0" * 64,
            },
        }
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, graph_bytes)
            zf.writestr(MANIFEST_MEMBER, json.dumps(manifest))

        with pytest.raises(ArchiveIntegrityError, match=EMBEDDINGS_MEMBER):
            extract_archive(buffer.getvalue(), live_model_name="test-model")

    def test_a_structurally_corrupt_embeddings_bin_with_no_manifest_checksum_degrades_instead_of_rejecting(
        self,
    ):
        """No manifest means no checksum promise was made for embeddings.bin,
        so a member that fails to parse as a sidecar degrades to 'regenerate'
        rather than failing the whole import — the graph content is fine."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, json.dumps(_document()))
            zf.writestr(EMBEDDINGS_MEMBER, b"not a sidecar at all")

        extracted = extract_archive(buffer.getvalue(), live_model_name="test-model")

        assert extracted.compatible is False
        assert extracted.graph_document["nodes"] == _document()["nodes"]
        assert "could not be read" in extracted.regeneration_reason


def _rewrite_member(
    archive_bytes: bytes, member_name: str, new_content: bytes
) -> bytes:
    """Return a copy of ``archive_bytes`` with ``member_name`` replaced, every
    other member (including manifest.json) byte-identical — simulating
    tampering/corruption of exactly one member in transit."""
    src = zipfile.ZipFile(io.BytesIO(archive_bytes))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            content = new_content if name == member_name else src.read(name)
            dst.writestr(name, content)
    return buffer.getvalue()
