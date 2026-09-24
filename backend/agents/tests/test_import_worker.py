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
    _run_one_import_job,
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
        # No nodes at all: compute_node_embeddings is never even called.
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

        monkeypatch.setattr(storage.vector_store, "compute_node_embeddings", _boom)
        job = store.enqueue(_import_job())

        drain_import_jobs(store, storage)

        stored = store.get(job.id)
        assert (
            stored.state == ExecutionState.PENDING
        )  # rescheduled, not degraded-succeeded
        assert "boom" in stored.last_error
        assert stored.attempts == 1
        # An embeddings failure is never a graph-content failure: the node the
        # (already-committed) replace wrote is untouched by the failed
        # embedding attempt (see ADR 0006 section 6, "never rolled back").
        assert storage.get_node("n1") is not None
        assert storage.get_node("n1").name == "Alice"


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

    def test_a_recovered_job_survives_a_real_process_restart_with_no_intervening_import(
        self, store: InMemoryExecutionStore, storage: GraphStorage, tmp_path
    ):
        """The `storage` fixture reused across "crash" and "recovery" in the
        test above never exercises what a real restart does: `_generation`
        resets to 0 on a brand-new `GraphStorage` object, and only a
        PERSISTED generation survives that. Here recovery runs against a
        second, freshly-constructed `GraphStorage` reading the same on-disk
        graph.json — a real restart — with no second import in between, so
        the job's stamped generation must still match and the job must
        complete normally rather than being wrongly marked superseded."""
        storage.replace_all_nodes_and_edges(
            [Node(id="n1", type=NodeType.ACTOR, name="Alice")], []
        )
        job = store.enqueue(
            _import_job(payload={"graph_generation": storage.generation})
        )
        claimed = store.claim_next("crashed-worker", lease_seconds=0.01)
        assert claimed is not None
        time.sleep(0.05)

        restarted = GraphStorage(
            json_path=str(tmp_path / "graph.json"),
            embeddings_path=str(tmp_path / "graph.embeddings.bin"),
        )
        # Sanity check on the fix itself: the persisted generation must have
        # survived the restart before we can say anything about the job.
        assert restarted.generation == storage.generation

        processed = recover_import_jobs(store, restarted)

        assert processed == 1
        stored = store.get(job.id)
        assert stored.state == ExecutionState.SUCCEEDED
        # Not superseded: the ML extras are absent in this test environment
        # (see module docstring), so "succeeded" degrades to "unavailable",
        # but either is the real outcome — "superseded" would mean the bug
        # this test exists to catch is back.
        assert stored.result["embeddings_status"] in ("succeeded", "unavailable")

    def test_a_recovered_job_is_still_superseded_across_a_real_process_restart(
        self, store: InMemoryExecutionStore, storage: GraphStorage, tmp_path
    ):
        """Same restart shape as above, but a second import genuinely lands
        (on the restarted instance) before the recovered job runs. The
        persisted-generation fix must not make the guard blind to a REAL
        supersession — it only has to stop reporting a FALSE one."""
        storage.replace_all_nodes_and_edges(
            [Node(id="n1", type=NodeType.ACTOR, name="First content")], []
        )
        stale_generation = storage.generation
        job = store.enqueue(_import_job(payload={"graph_generation": stale_generation}))
        claimed = store.claim_next("crashed-worker", lease_seconds=0.01)
        assert claimed is not None
        time.sleep(0.05)

        restarted = GraphStorage(
            json_path=str(tmp_path / "graph.json"),
            embeddings_path=str(tmp_path / "graph.embeddings.bin"),
        )
        assert restarted.generation == stale_generation
        # A second import lands on the restarted instance before the
        # recovered job ever runs — the genuine "later import happened" case.
        restarted.replace_all_nodes_and_edges(
            [Node(id="n2", type=NodeType.ACTOR, name="Second content")], []
        )

        processed = recover_import_jobs(store, restarted)

        assert processed == 1
        stored = store.get(job.id)
        assert stored.state == ExecutionState.CANCELLED
        assert stored.result["embeddings_status"] == "superseded"
        assert not restarted.vector_store.has_embedding("n2")


class TestGenerationStalenessGuard:
    """A job's embeddings must never be written back once a later import has
    replaced the graph they were computed against — see the generation guard
    in ``GraphStorage.replace_all_nodes_and_edges`` /
    ``commit_generation_embeddings`` and ``import_worker._run_one_import_job``.

    Timing is controlled deterministically by calling ``_run_one_import_job``
    directly (rather than via ``drain_import_jobs`` on a real background
    thread) and by triggering the "second import lands mid-encode" race from
    inside a monkeypatched ``compute_node_embeddings`` — the exact point in
    the real code where a slow, real encode would still be running.
    """

    def test_a_stale_jobs_embeddings_are_discarded_and_the_new_import_is_not_skipped(
        self,
        store: InMemoryExecutionStore,
        storage: GraphStorage,
        monkeypatch,
    ):
        storage.replace_all_nodes_and_edges(
            [Node(id="shared-1", type=NodeType.ACTOR, name="First content")], []
        )
        job_a = store.enqueue(
            _import_job(key="a", payload={"graph_generation": storage.generation})
        )

        def _encode_then_a_second_import_lands(nodes):
            # A second `POST /import` replacing the graph WHILE job A's
            # (slow, real) encode is still running is exactly the race this
            # guard exists to close — simulated here by performing it from
            # inside the mocked encode step itself, which is the real code's
            # only slow point.
            computed = {n.id: [1.0, 0.0, 0.0] for n in nodes}
            storage.replace_all_nodes_and_edges(
                [Node(id="shared-1", type=NodeType.ACTOR, name="Second content")],
                [],
            )
            return computed

        monkeypatch.setattr(
            storage.vector_store,
            "compute_node_embeddings",
            _encode_then_a_second_import_lands,
        )

        claimed_a = store.claim_next("worker-a")
        assert claimed_a.id == job_a.id
        _run_one_import_job(store, storage, claimed_a)

        stored_a = store.get(job_a.id)
        assert stored_a.state == ExecutionState.CANCELLED
        assert stored_a.result["embeddings_status"] == "superseded"
        # Job A's (wrong, first-content) vector must never have landed on the
        # live "shared-1" node, which by now is the SECOND import's content.
        assert not storage.vector_store.has_embedding("shared-1")
        assert storage.get_node("shared-1").name == "Second content"

        # The second import's own job must not see "shared-1" as already
        # embedded (it would be, had job A's write gone through) and skip it.
        monkeypatch.setattr(
            storage.vector_store,
            "compute_node_embeddings",
            lambda nodes: {n.id: [0.0, 1.0, 0.0] for n in nodes},
        )
        job_b = store.enqueue(
            _import_job(key="b", payload={"graph_generation": storage.generation})
        )
        claimed_b = store.claim_next("worker-b")
        assert claimed_b.id == job_b.id
        _run_one_import_job(store, storage, claimed_b)

        stored_b = store.get(job_b.id)
        assert stored_b.state == ExecutionState.SUCCEEDED
        assert stored_b.result["embeddings_status"] == "succeeded"
        assert stored_b.result["embedded_count"] == 1
        assert storage.vector_store.get_vector_list("shared-1") == [0.0, 1.0, 0.0]

    def test_a_job_stale_before_it_even_starts_is_superseded_without_reading_the_new_graph(
        self,
        store: InMemoryExecutionStore,
        storage: GraphStorage,
        monkeypatch,
    ):
        """Widened crash-recovery window: a job resumed by
        ``recover_import_jobs`` after a later import already landed must not
        touch the new graph's nodes under the old job's identity at all."""
        storage.replace_all_nodes_and_edges(
            [Node(id="n1", type=NodeType.ACTOR, name="First content")], []
        )
        stale_generation = storage.generation
        job = store.enqueue(_import_job(payload={"graph_generation": stale_generation}))
        claimed = store.claim_next("recovered-worker")

        # A second import lands before the recovered job ever runs.
        storage.replace_all_nodes_and_edges(
            [Node(id="n2", type=NodeType.ACTOR, name="Second content")], []
        )

        def _must_not_be_called(nodes):
            raise AssertionError(
                "a stale job must not encode the CURRENT graph's nodes"
            )

        monkeypatch.setattr(
            storage.vector_store, "compute_node_embeddings", _must_not_be_called
        )

        _run_one_import_job(store, storage, claimed)

        stored = store.get(job.id)
        assert stored.state == ExecutionState.CANCELLED
        assert stored.result["embeddings_status"] == "superseded"
        assert not storage.vector_store.has_embedding("n2")

    def test_no_replace_can_land_between_the_generation_check_and_the_write(
        self,
        store: InMemoryExecutionStore,
        storage: GraphStorage,
        monkeypatch,
    ):
        """The replace in the test above lands during the encode, before
        ``commit_generation_embeddings`` is entered, so it passes just as well
        against a commit that checks the generation under the lock, releases
        it, and re-acquires it to write. This one lands a replace at the first
        moment the commit gives the lock up — which is after the write only if
        the check and the write are one critical section."""
        storage.replace_all_nodes_and_edges(
            [Node(id="shared-1", type=NodeType.ACTOR, name="First content")], []
        )
        job_generation = storage.generation
        job = store.enqueue(_import_job(payload={"graph_generation": job_generation}))

        real_lock = storage._lock
        armed = False
        depth = 0
        seen_by_the_replace = []

        def _second_import():
            seen_by_the_replace.append(storage.vector_store.has_embedding("shared-1"))
            storage.replace_all_nodes_and_edges(
                [Node(id="shared-1", type=NodeType.ACTOR, name="Second content")],
                [],
            )

        class _ReplaceOnFirstRelease:
            def __enter__(self):
                nonlocal depth
                real_lock.acquire()
                depth += 1

            def __exit__(self, *exc):
                nonlocal armed, depth
                depth -= 1
                real_lock.release()
                if armed and depth == 0:
                    armed = False
                    _second_import()

        def _encode_then_arm(nodes):
            # Armed only now, so the first release it fires on is the commit's.
            nonlocal armed
            armed = True
            return {n.id: [1.0, 0.0, 0.0] for n in nodes}

        monkeypatch.setattr(storage, "_lock", _ReplaceOnFirstRelease())
        monkeypatch.setattr(
            storage.vector_store, "compute_node_embeddings", _encode_then_arm
        )

        claimed = store.claim_next("worker-a")
        assert claimed.id == job.id
        _run_one_import_job(store, storage, claimed)

        assert seen_by_the_replace, "the hook never fired: nothing was tested"
        assert storage.generation == job_generation + 1
        # The commit was already whole when it first let the lock go ...
        assert seen_by_the_replace == [True]
        # ... so the replace dropped it, and the first content's vector is not
        # left sitting on the second content's node.
        assert not storage.vector_store.has_embedding("shared-1")
        assert storage.get_node("shared-1").name == "Second content"
