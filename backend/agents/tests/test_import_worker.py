"""
Tests for backend.agents.execution.import_worker — the background half of
graph.json import that (re)generates embeddings after a REPLACE (see
docs/adr/0006-graph-import-replace-mode.md and
backend/service/import_service.py, which enqueues the jobs this module
drains).

Deliberately run WITHOUT the ``mock_embedding_model`` fixture that
``backend/service/tests/conftest.py`` applies: sentence-transformers is not
installed in this environment (the same ML-free posture this repo's CI runs
under), so these tests exercise the real "ML extras absent" degrade path
end-to-end rather than mocking it.
"""

import time

import pytest

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    InMemoryExecutionStore,
)
from backend.agents.execution.import_worker import (
    drain_import_jobs,
    recover_import_jobs,
)
from backend.core import GraphStorage, Node, NodeType


@pytest.fixture
def storage(tmp_path) -> GraphStorage:
    return GraphStorage(
        json_path=str(tmp_path / "graph.json"),
        embeddings_path=str(tmp_path / "graph.embeddings.bin"),
    )


@pytest.fixture
def store() -> InMemoryExecutionStore:
    return InMemoryExecutionStore()


def _import_job(**overrides) -> ExecutionJob:
    payload = dict(
        agent_id="system:graph-import",
        kind=ExecutionKind.IMPORT,
        idempotency_key=f"import:{overrides.pop('key', 'k1')}",
    )
    payload.update(overrides)
    return ExecutionJob(**payload)


class TestMlExtrasUnavailableDegrade:
    def test_a_node_lacking_an_embedding_completes_the_job_as_degraded(
        self, store: InMemoryExecutionStore, storage: GraphStorage
    ):
        storage.add_nodes([Node(id="n1", type=NodeType.ACTOR, name="Alice")], [])
        assert not storage.vector_store.has_embedding("n1")
        job = store.enqueue(_import_job())

        processed = drain_import_jobs(store, storage)

        assert processed == 1
        stored = store.get(job.id)
        assert stored.state == ExecutionState.SUCCEEDED
        assert stored.result["embeddings_status"] == "unavailable"

    def test_a_graph_with_nothing_left_to_embed_still_succeeds(
        self, store: InMemoryExecutionStore, storage: GraphStorage
    ):
        # No nodes at all: update_nodes_embeddings is never even called.
        job = store.enqueue(_import_job())

        drain_import_jobs(store, storage)

        stored = store.get(job.id)
        assert stored.state == ExecutionState.SUCCEEDED
        assert stored.result["embeddings_status"] == "succeeded"
        assert stored.result["embedded_count"] == 0


class TestDrainQueueMechanics:
    def test_drain_returns_zero_on_an_empty_queue(
        self, store: InMemoryExecutionStore, storage: GraphStorage
    ):
        assert drain_import_jobs(store, storage) == 0

    def test_drain_processes_every_pending_import_job(
        self, store: InMemoryExecutionStore, storage: GraphStorage
    ):
        store.enqueue(_import_job(key="a"))
        store.enqueue(_import_job(key="b"))

        processed = drain_import_jobs(store, storage)

        assert processed == 2

    def test_a_non_import_job_is_failed_back_rather_than_processed(
        self, store: InMemoryExecutionStore, storage: GraphStorage
    ):
        foreign = store.enqueue(
            ExecutionJob(
                agent_id="some-agent",
                kind=ExecutionKind.EVENT,
                idempotency_key="evt-1",
            )
        )

        processed = drain_import_jobs(store, storage)

        assert processed == 0  # not counted as import work done
        stored = store.get(foreign.id)
        assert stored.attempts == 1
        assert "import embeddings worker" in stored.last_error
        # Retry budget (default max_attempts=3) not yet exhausted: rescheduled,
        # not dead-lettered.
        assert stored.state == ExecutionState.PENDING


class TestRealEmbeddingFailureRetries:
    def test_a_genuine_error_is_retried_rather_than_marked_succeeded(
        self,
        store: InMemoryExecutionStore,
        storage: GraphStorage,
        monkeypatch,
    ):
        storage.add_nodes([Node(id="n1", type=NodeType.ACTOR, name="Alice")], [])

        def _boom(nodes):
            raise RuntimeError("boom: encoder crashed")

        monkeypatch.setattr(storage.vector_store, "update_nodes_embeddings", _boom)
        job = store.enqueue(_import_job())

        drain_import_jobs(store, storage)

        stored = store.get(job.id)
        assert (
            stored.state == ExecutionState.PENDING
        )  # rescheduled, not degraded-succeeded
        assert "boom" in stored.last_error
        assert stored.attempts == 1


class TestStartupRecovery:
    def test_a_job_left_running_by_a_crashed_worker_is_resumed(
        self, store: InMemoryExecutionStore, storage: GraphStorage
    ):
        store.enqueue(_import_job())
        # Simulate a worker that claimed the job and then crashed: a very
        # short lease that has genuinely expired by the time recovery runs.
        claimed = store.claim_next("crashed-worker", lease_seconds=0.01)
        assert claimed is not None
        time.sleep(0.05)

        processed = recover_import_jobs(store, storage)

        assert processed == 1
        stored = store.get(claimed.id)
        assert stored.state == ExecutionState.SUCCEEDED
