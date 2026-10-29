"""
Tests for backend.service.import_service.import_graph_archive — the
vector-aware archive import, built on the same validate -> backup -> replace
pipeline as backend.service.import_service.import_graph (see
backend/service/tests/test_import_service.py). See
docs/adr/0007-vector-aware-export-archive.md.
"""

import io
import json
import time
import zipfile

import numpy as np

from backend.agents.execution import ExecutionKind, InMemoryExecutionStore
from backend.core import GraphStorage, Node, NodeType
from backend.runtime.authorization import DefaultGraphAuthorizationHook
from backend.service import graph_archive, import_service, views


def _wait_for_terminal(store, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job is not None and job.is_terminal:
            return job
        time.sleep(0.01)
    raise AssertionError(f"import job {job_id} did not reach a terminal state in time")


def _populated_storage_with_vectors(json_path: str) -> GraphStorage:
    """A storage with two nodes whose embeddings were generated the normal
    way (the mock embedding model from conftest.py's autouse fixture makes
    ``add_nodes`` compute them synchronously)."""
    storage = GraphStorage(json_path=json_path)
    storage.add_nodes(
        [
            Node(id="n1", type=NodeType.ACTOR, name="Alice"),
            Node(id="n2", type=NodeType.ACTOR, name="Bob"),
        ],
        [],
    )
    assert storage.vector_store.get_embedding_count() == 2
    return storage


def _export_archive_bytes(storage: GraphStorage) -> bytes:
    result = views.export_graph_archive(storage, DefaultGraphAuthorizationHook())
    assert result["success"] is True
    return result["archive_bytes"]


class TestCompatibleArchiveRestoresVectorsSynchronously:
    def test_vectors_are_restored_bit_identical_with_no_async_job(
        self, temp_dir, tmp_path
    ):
        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        archive_bytes = _export_archive_bytes(source)

        target_json = str(tmp_path / "target.json")
        target = GraphStorage(json_path=target_json)
        target.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, archive_bytes
        )

        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["node_count"] == 2
        assert result["job_id"] is None
        assert result["embeddings_status"] == "restored"
        assert result["embedded_count"] == 2
        assert result["archive_compatible"] is True
        assert target.get_node("old-1") is None
        assert target.get_node("n1") is not None

        for node_id in ("n1", "n2"):
            np.testing.assert_array_equal(
                target.vector_store.get_vector_list(node_id),
                source.vector_store.get_vector_list(node_id),
            )

        # No async job was ever created for this import.
        assert store.list_jobs() == []

    def test_the_restore_is_actually_synchronous_not_a_fast_async_job(
        self, temp_dir, tmp_path, monkeypatch
    ):
        """If this ever regressed into enqueuing a job even on the compatible
        path, a slow encode step (which the async path calls, and the direct
        restore never does) would show up as latency here."""
        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        archive_bytes = _export_archive_bytes(source)

        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        store = InMemoryExecutionStore()

        def _boom(*args, **kwargs):
            raise AssertionError(
                "compute_node_embeddings must not be called on the compatible "
                "archive-restore path"
            )

        monkeypatch.setattr(target.vector_store, "compute_node_embeddings", _boom)

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, archive_bytes
        )

        assert result["embeddings_status"] == "restored"


class TestIncompatibleModelFallsBackToRegeneration:
    def test_a_model_mismatch_still_imports_the_graph_and_regenerates(
        self, temp_dir, tmp_path
    ):
        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        document = views.export_graph(source, DefaultGraphAuthorizationHook())
        archive_bytes = graph_archive.build_archive_bytes(
            document,
            embedding_vectors=source.vector_store.export_vectors(),
            embedding_model="a-completely-different-model",
        )

        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, archive_bytes
        )

        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["archive_compatible"] is False
        assert result["embeddings_status"] == "queued"
        assert result["job_id"] is not None
        assert "a-completely-different-model" in result["embeddings_message"]
        assert target.get_node("n1") is not None

        job = _wait_for_terminal(store, result["job_id"])
        assert job.kind == ExecutionKind.IMPORT
        assert job.state.value == "succeeded"
        assert job.result.get("embeddings_status") == "succeeded"
        # The regenerated vectors land in the live index even though they
        # were not restored from the archive.
        assert target.vector_store.get_embedding_count() == 2


class TestPlainGraphOnlyArchiveImportsAndRegenerates:
    def test_a_zip_with_only_graph_json_still_imports_successfully(
        self, temp_dir, tmp_path
    ):
        """The 'someone made a plain graph.json export and zipped it
        themselves' case — no manifest.json, no embeddings.bin at all."""
        document = {
            "nodes": [{"id": "n1", "type": "Actor", "name": "Alice"}],
            "edges": [],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(graph_archive.GRAPH_MEMBER, json.dumps(document))
        archive_bytes = buffer.getvalue()

        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, archive_bytes
        )

        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["node_count"] == 1
        assert result["archive_compatible"] is False
        assert result["embeddings_status"] == "queued"
        assert "no manifest.json" in result["embeddings_message"]
        assert target.get_node("n1") is not None

        job = _wait_for_terminal(store, result["job_id"])
        assert job.state.value == "succeeded"


class TestCorruptOrTamperedArchiveRejectedBeforeAnyWrite:
    def test_bytes_that_are_not_a_zip_leave_the_graph_untouched(self, tmp_path):
        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        target.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, b"not a zip file at all"
        )

        assert result["success"] is False
        assert result["error_code"] == "archive_integrity_failed"
        assert result["graph_replaced"] is False
        assert [n.id for n in target.get_all_nodes()] == ["old-1"]
        assert store.list_jobs() == []

    def test_a_tampered_member_leaves_the_graph_untouched(self, temp_dir, tmp_path):
        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        archive_bytes = _export_archive_bytes(source)

        src_zip = zipfile.ZipFile(io.BytesIO(archive_bytes))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as dst:
            for name in src_zip.namelist():
                content = (
                    b"tampered graph content"
                    if name == graph_archive.GRAPH_MEMBER
                    else src_zip.read(name)
                )
                dst.writestr(name, content)
        tampered_bytes = buffer.getvalue()

        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        target.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, tampered_bytes
        )

        assert result["success"] is False
        assert result["error_code"] == "archive_integrity_failed"
        assert result["graph_replaced"] is False
        assert [n.id for n in target.get_all_nodes()] == ["old-1"]
        assert store.list_jobs() == []


class TestPartiallyCompatibleArchiveRestoresCoveredSubsetAndScopesRegeneration:
    """The archive is model-compatible, but ``embeddings.bin`` covers only
    SOME of the imported graph's node ids — e.g. the exporting instance had
    its own never-embedded nodes (``add_nodes``'s embedding step
    warns-and-skips on failure rather than retrying). Reporting plain
    ``"restored"`` success here would silently leave the uncovered nodes with
    no vector and no job ever queued to fill the gap."""

    def test_covered_nodes_restored_and_only_the_missing_node_is_regenerated(
        self, temp_dir, tmp_path, monkeypatch
    ):
        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        store = InMemoryExecutionStore()

        # The imported graph has THREE nodes; the archive's embeddings.bin
        # (built from `source`, which only ever had two) covers just two of
        # them.
        document = {
            "nodes": [
                {"id": "n1", "type": "Actor", "name": "Alice"},
                {"id": "n2", "type": "Actor", "name": "Bob"},
                {"id": "n3", "type": "Actor", "name": "Carol"},
            ],
            "edges": [],
        }
        archive_bytes = graph_archive.build_archive_bytes(
            document,
            embedding_vectors=source.vector_store.export_vectors(),
            embedding_model=target.vector_store.model_name,
        )

        seen_node_ids = []
        original_compute = target.vector_store.compute_node_embeddings

        def _tracking_compute(nodes):
            seen_node_ids.extend(n.id for n in nodes)
            return original_compute(nodes)

        monkeypatch.setattr(
            target.vector_store, "compute_node_embeddings", _tracking_compute
        )

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, archive_bytes
        )

        # (c) distinguishable from a fully-restored success.
        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["node_count"] == 3
        assert result["archive_compatible"] is True
        assert result["embeddings_status"] == "restored_partial"
        assert result["embeddings_status"] != "restored"
        assert result["embedded_count"] == 2
        assert result["pending_node_ids"] == ["n3"]
        assert result["pending_count"] == 1
        assert result["job_id"] is not None

        # (a) the covered nodes are restored bit-identical. This happens
        # synchronously inside the call above (`commit_generation_embeddings`,
        # before the regeneration job is even enqueued) — the response's own
        # `embedded_count`/`pending_node_ids` above already prove that
        # ordering; the background job may since have raced ahead and
        # completed by the time this runs, which is fine.
        for node_id in ("n1", "n2"):
            np.testing.assert_array_equal(
                target.vector_store.get_vector_list(node_id),
                source.vector_store.get_vector_list(node_id),
            )

        # (b) a regeneration job was created and is scoped to ONLY the
        # missing node — it must not re-process the nodes already restored
        # directly from the archive.
        job = _wait_for_terminal(store, result["job_id"])
        assert job.kind == ExecutionKind.IMPORT
        assert job.state.value == "succeeded"
        assert seen_node_ids == ["n3"]

        # (d) once that job completes, every node ends up with a vector, and
        # the directly-restored ones were not overwritten in the process.
        assert target.vector_store.get_embedding_count() == 3
        assert target.vector_store.has_embedding("n3") is True
        for node_id in ("n1", "n2"):
            np.testing.assert_array_equal(
                target.vector_store.get_vector_list(node_id),
                source.vector_store.get_vector_list(node_id),
            )

    def test_a_fully_covered_archive_is_unaffected_no_job_is_ever_enqueued(
        self, temp_dir, tmp_path
    ):
        """Regression guard for the common case: when embeddings.bin DOES
        cover every live node, behaviour must stay exactly what it was
        before this partial-coverage handling existed."""
        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        archive_bytes = _export_archive_bytes(source)

        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, archive_bytes
        )

        assert result["embeddings_status"] == "restored"
        assert result["job_id"] is None
        assert "pending_node_ids" not in result
        assert store.list_jobs() == []


class TestArchiveImportWritesAPreImportBackupOfThePreviousGraph:
    def test_backup_reproduces_the_graph_the_archive_replaced(self, temp_dir, tmp_path):
        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        archive_bytes = _export_archive_bytes(source)

        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        target.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, DefaultGraphAuthorizationHook(), store, archive_bytes
        )

        backup_path = result["backup_path"]
        assert backup_path is not None
        with open(backup_path) as fh:
            backup = json.load(fh)
        assert {n["id"] for n in backup["nodes"]} == {"old-1"}


class TestArchiveImportRequiresFullGraphAccessLikePlainImport:
    def test_narrowed_access_is_refused_without_touching_the_graph(
        self, temp_dir, tmp_path
    ):
        from backend.runtime.authorization import (
            GraphAccessNarrowing,
            GraphAuthorizationContext,
            GraphAuthorizationDecision,
        )

        class _NarrowedAccessHook:
            def evaluate(self, context: GraphAuthorizationContext):
                return GraphAuthorizationDecision(
                    allowed=True,
                    mode="narrowed",
                    source="test",
                    graph_access=GraphAccessNarrowing(
                        enabled=True,
                        allow_local_graph=False,
                        include_graph_ids=("graph-a",),
                    ),
                )

        source = _populated_storage_with_vectors(f"{temp_dir}/source.json")
        archive_bytes = _export_archive_bytes(source)

        target = GraphStorage(json_path=str(tmp_path / "target.json"))
        target.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph_archive(
            target, _NarrowedAccessHook(), store, archive_bytes
        )

        assert result["success"] is False
        assert result["error_code"] == "import_requires_full_graph_access"
        assert result["graph_replaced"] is False
        assert [n.id for n in target.get_all_nodes()] == ["old-1"]
