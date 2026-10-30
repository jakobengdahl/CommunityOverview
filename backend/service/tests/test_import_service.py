"""
Tests for backend.service.import_service.import_graph — the whole-graph
REPLACE orchestration (validate -> backup -> replace -> enqueue embeddings).
See docs/adr/0006-graph-import-replace-mode.md and
backend/service/tests/test_graph_import.py (validation) /
backend/core/tests/test_graph_replace.py (atomic swap + rollback) for the
lower layers this builds on.
"""

import io
import json
import time
import zipfile

import pytest

from backend.agents.execution import ExecutionKind, InMemoryExecutionStore
from backend.core import GraphStorage, Node, NodeType
from backend.runtime.authorization import (
    DefaultGraphAuthorizationHook,
    GraphAccessNarrowing,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
)
from backend.service import import_service


class _NarrowedAccessHook:
    """Always allows the action, but with graph-scope narrowing active —
    exactly the shape a multi-graph-aware caller would see."""

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        return GraphAuthorizationDecision(
            allowed=True,
            mode="narrowed",
            source="test",
            graph_access=GraphAccessNarrowing(
                enabled=True, allow_local_graph=False, include_graph_ids=("graph-a",)
            ),
        )


def _valid_document():
    return {
        "nodes": [
            {"id": "n1", "type": "Actor", "name": "Alice"},
            {"id": "n2", "type": "Initiative", "name": "Project X"},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "type": "BELONGS_TO"}],
    }


def _wait_for_terminal(store, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job is not None and job.is_terminal:
            return job
        time.sleep(0.01)
    raise AssertionError(f"import job {job_id} did not reach a terminal state in time")


class TestImportSuccess:
    def test_import_replaces_the_graph_and_returns_success(
        self, empty_storage: GraphStorage
    ):
        empty_storage.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )

        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["node_count"] == 2
        assert result["edge_count"] == 1
        assert "job_id" in result
        assert empty_storage.get_node("old-1") is None
        assert empty_storage.get_node("n1") is not None
        assert empty_storage.get_node("n2") is not None

    def test_import_enqueues_a_job_that_reaches_a_terminal_state(
        self, empty_storage: GraphStorage
    ):
        store = InMemoryExecutionStore()

        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )

        job = _wait_for_terminal(store, result["job_id"])
        assert job.kind == ExecutionKind.IMPORT
        assert job.state.value == "succeeded"
        assert job.result.get("embeddings_status") == "succeeded"

    def test_import_writes_a_pre_import_backup_of_the_PREVIOUS_graph(
        self, empty_storage: GraphStorage
    ):
        empty_storage.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )

        backup_path = result["backup_path"]
        assert backup_path is not None
        with open(backup_path) as fh:
            backup = json.load(fh)
        assert {n["id"] for n in backup["nodes"]} == {"old-1"}


class TestImportValidationFailure:
    def test_invalid_document_leaves_the_graph_untouched_and_enqueues_nothing(
        self, empty_storage: GraphStorage
    ):
        empty_storage.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()
        bad_document = {"nodes": [{"id": "n1", "type": "NotARealType", "name": "x"}]}

        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, bad_document
        )

        assert result["success"] is False
        assert result["error_code"] == "validation_failed"
        assert result["errors"]
        assert result["graph_replaced"] is False
        assert [n.id for n in empty_storage.get_all_nodes()] == ["old-1"]
        assert store.list_jobs() == []


class TestValidationFailureIsCapped:
    def test_the_message_counts_every_problem_while_the_list_is_capped(
        self, empty_storage: GraphStorage
    ):
        store = InMemoryExecutionStore()
        nodes = [
            {"id": f"n{i}", "type": "NotARealType", "name": "x"} for i in range(1_500)
        ]

        result = import_service.import_graph(
            empty_storage,
            DefaultGraphAuthorizationHook(),
            store,
            {"nodes": nodes},
        )

        assert result["error_code"] == "validation_failed"
        assert result["message"].startswith("1500 problem(s)")
        assert len(result["errors"]) == 1_000


class TestImportRequiresFullGraphAccess:
    def test_narrowed_access_is_refused_without_touching_the_graph(
        self, empty_storage: GraphStorage
    ):
        empty_storage.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        store = InMemoryExecutionStore()

        result = import_service.import_graph(
            empty_storage, _NarrowedAccessHook(), store, _valid_document()
        )

        assert result["success"] is False
        assert result["error_code"] == "import_requires_full_graph_access"
        assert result["graph_replaced"] is False
        assert [n.id for n in empty_storage.get_all_nodes()] == ["old-1"]
        assert store.list_jobs() == []


class TestImportReplaceFailure:
    @pytest.mark.parametrize("archive", [False, True], ids=["json", "archive"])
    @pytest.mark.parametrize("partial_cleanup", [False, True])
    def test_journal_cleanup_failure_preserves_the_previous_durable_graph(
        self, empty_storage, monkeypatch, caplog, archive, partial_cleanup
    ):
        empty_storage.add_nodes(
            [Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], []
        )
        backend = empty_storage._persistence_backend
        empty_storage.save().result()
        backend.upsert_node(empty_storage.get_node("old-1").to_dict())
        assert backend.journal_path.stat().st_size > 0
        generation = empty_storage.generation
        private_detail = "journal cleanup failed at /private/store"

        def fail_cleanup():
            if partial_cleanup:
                backend.journal_path.write_bytes(b"")
            raise OSError(private_detail)

        monkeypatch.setattr(backend, "_truncate_journal", fail_cleanup)
        store = InMemoryExecutionStore()
        if archive:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as zf:
                zf.writestr("graph.json", json.dumps(_valid_document()))
            result = import_service.import_graph_archive(
                empty_storage, DefaultGraphAuthorizationHook(), store, buffer.getvalue()
            )
        else:
            result = import_service.import_graph(
                empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
            )

        assert result["success"] is False
        assert result["graph_replaced"] is False
        assert result["error_code"] == "replace_failed"
        with open(result["backup_path"]) as fh:
            assert {n["id"] for n in json.load(fh)["nodes"]} == {"old-1"}
        assert {n.id for n in empty_storage.get_all_nodes()} == {"old-1"}
        assert empty_storage.get_all_edges() == []
        assert empty_storage.generation == generation
        reloaded = type(backend)(backend.json_path).load_graph_data()
        assert {n["id"] for n in reloaded["nodes"]} == {"old-1"}
        assert reloaded["edges"] == []
        assert store.list_jobs() == []
        assert private_detail not in json.dumps(result)
        assert private_detail in caplog.text
        assert "Traceback" in caplog.text

    def test_a_replace_failure_is_reported_and_keeps_the_backup_path(
        self, empty_storage: GraphStorage, monkeypatch, caplog
    ):
        private_detail = "could not save /private/store/graph.json as database_role"

        def _boom(nodes, edges, **kwargs):
            raise RuntimeError(private_detail)

        monkeypatch.setattr(empty_storage, "replace_all_nodes_and_edges", _boom)
        store = InMemoryExecutionStore()

        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )

        assert result["success"] is False
        assert result["error_code"] == "replace_failed"
        assert result["graph_replaced"] is False
        assert result["backup_path"] is not None
        assert store.list_jobs() == []
        assert private_detail not in json.dumps(result)
        assert result["message"] == (
            "Import failed while committing the new graph; the previous graph is unchanged."
        )
        assert private_detail in caplog.text
        assert "RuntimeError" in caplog.text


class TestEnqueueOrDrainStartFailureIsNotFatalToTheReplace:
    @pytest.mark.parametrize("archive", [False, True], ids=["json", "archive"])
    @pytest.mark.parametrize(
        "failure_point", ["_enqueue_embedding_job", "_start_embedding_drain"]
    )
    def test_startup_error_details_are_logged_but_not_returned(
        self, empty_storage, monkeypatch, caplog, archive, failure_point
    ):
        private_detail = (
            "connection to private-db.internal as secret_adapter_role failed"
        )

        def _boom(*args, **kwargs):
            raise RuntimeError(private_detail)

        monkeypatch.setattr(import_service, failure_point, _boom)
        store = InMemoryExecutionStore()
        if archive:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as zf:
                zf.writestr("graph.json", json.dumps(_valid_document()))
            result = import_service.import_graph_archive(
                empty_storage, DefaultGraphAuthorizationHook(), store, buffer.getvalue()
            )
        else:
            result = import_service.import_graph(
                empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
            )

        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["embeddings_status"] == "not_started"
        assert result["job_id"] is None
        assert empty_storage.get_node("n1") is not None
        assert private_detail not in json.dumps(result)
        assert (
            "embedding generation could not be started." in result["embeddings_message"]
        )
        assert private_detail in caplog.text

    def test_a_failure_to_start_embeddings_still_reports_the_replace_as_successful(
        self, empty_storage: GraphStorage, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("job store unreachable")

        monkeypatch.setattr(import_service, "_enqueue_embedding_job", _boom)
        store = InMemoryExecutionStore()

        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )

        # The graph WAS replaced — that must never be reported as a failure,
        # since retrying would perform another full replace to fix a problem
        # that has nothing to do with the graph itself.
        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["node_count"] == 2
        assert result["edge_count"] == 1
        assert empty_storage.get_node("n1") is not None
        assert empty_storage.get_node("n2") is not None
        # But the caller must be able to tell embeddings never started, with
        # a status distinct from every other embeddings_status.
        assert result["job_id"] is None
        assert result["embeddings_status"] == "not_started"
        assert "embeddings_message" in result
        assert store.list_jobs() == []

    def test_a_failure_to_start_the_drain_thread_still_reports_the_replace_as_successful(
        self, empty_storage: GraphStorage, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("could not start worker thread")

        monkeypatch.setattr(import_service, "_start_embedding_drain", _boom)
        store = InMemoryExecutionStore()

        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )

        assert result["success"] is True
        assert result["graph_replaced"] is True
        assert result["job_id"] is None
        assert result["embeddings_status"] == "not_started"
        assert empty_storage.get_node("n1") is not None


class TestImportJobLookup:
    def test_get_import_job_returns_none_for_unknown_id(self):
        store = InMemoryExecutionStore()
        assert import_service.get_import_job(store, "job-does-not-exist") is None

    def test_get_import_job_reflects_current_status(self, empty_storage: GraphStorage):
        store = InMemoryExecutionStore()
        result = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )

        _wait_for_terminal(store, result["job_id"])
        job_view = import_service.get_import_job(store, result["job_id"])

        assert job_view["id"] == result["job_id"]
        assert job_view["status"] == "succeeded"
        assert job_view["kind"] == "import"

    def test_list_import_jobs_is_newest_first_and_import_only(
        self, empty_storage: GraphStorage
    ):
        store = InMemoryExecutionStore()
        first = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )
        _wait_for_terminal(store, first["job_id"])
        second = import_service.import_graph(
            empty_storage, DefaultGraphAuthorizationHook(), store, _valid_document()
        )
        _wait_for_terminal(store, second["job_id"])

        jobs = import_service.list_import_jobs(store)
        assert [job["id"] for job in jobs[:2]] == [second["job_id"], first["job_id"]]
        assert all(job["kind"] == "import" for job in jobs)
