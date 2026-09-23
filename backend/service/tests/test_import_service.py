"""
Tests for backend.service.import_service.import_graph — the whole-graph
REPLACE orchestration (validate -> backup -> replace -> enqueue embeddings).
See docs/adr/0006-graph-import-replace-mode.md and
backend/service/tests/test_graph_import.py (validation) /
backend/core/tests/test_graph_replace.py (atomic swap + rollback) for the
lower layers this builds on.
"""

import json
import time

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
    def test_a_replace_failure_is_reported_and_keeps_the_backup_path(
        self, empty_storage: GraphStorage, monkeypatch
    ):
        def _boom(nodes, edges):
            raise RuntimeError("disk full")

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
