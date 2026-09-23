"""
Tests for GraphStorage.replace_all_nodes_and_edges — the atomic whole-graph
swap that backs graph.json import (see backend/service/import_service.py and
docs/adr/0006-graph-import-replace-mode.md).
"""

import json
import threading

import pytest

from backend.core.models import Edge, Node, NodeType, RelationshipType
from backend.core.storage import GraphStorage


class _SnapshotBackend:
    """Minimal snapshot-only fake backend (no capability declaration), same
    shape as ``_SnapshotBackend`` in test_persistence_seam.py, plus a knob to
    make the next ``save_graph_data`` call raise so a mid-replace failure can
    be exercised deterministically."""

    def __init__(self):
        self.data = None
        self.saves = 0
        self.fail_next_save = False

    def exists(self):
        return self.data is not None

    def load_graph_data(self):
        return json.loads(json.dumps(self.data)) if self.data is not None else {}

    def save_graph_data(self, data):
        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("simulated disk failure")
        self.saves += 1
        self.data = json.loads(json.dumps(data))

    def default_graph_name(self):
        return "test-graph"


@pytest.fixture
def backend() -> _SnapshotBackend:
    return _SnapshotBackend()


@pytest.fixture
def storage(backend: _SnapshotBackend) -> GraphStorage:
    return GraphStorage(persistence_backend=backend)


def _old_node() -> Node:
    return Node(id="old-1", type=NodeType.ACTOR, name="Old node")


def _new_node() -> Node:
    return Node(id="new-1", type=NodeType.ACTOR, name="New node")


class TestReplaceSwapsContent:
    def test_replace_discards_old_nodes_and_edges(self, storage: GraphStorage):
        storage.add_nodes([_old_node()], [])
        assert storage.get_node("old-1") is not None

        storage.replace_all_nodes_and_edges([_new_node()], [])

        assert storage.get_node("old-1") is None
        assert storage.get_node("new-1") is not None
        assert [n.id for n in storage.get_all_nodes()] == ["new-1"]
        assert storage.get_all_edges() == []

    def test_replace_keeps_edges_between_new_nodes(self, storage: GraphStorage):
        a = Node(id="a", type=NodeType.ACTOR, name="A")
        b = Node(id="b", type=NodeType.INITIATIVE, name="B")
        edge = Edge(id="e1", source="a", target="b", type=RelationshipType.BELONGS_TO)

        storage.replace_all_nodes_and_edges([a, b], [edge])

        assert storage.get_node("a") is not None
        assert storage.get_node("b") is not None
        edges = storage.get_all_edges()
        assert len(edges) == 1
        assert edges[0].id == "e1"

    def test_replace_persists_the_new_content_to_the_backend(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.replace_all_nodes_and_edges([_new_node()], [])

        node_ids = {n["id"] for n in backend.data["nodes"]}
        assert node_ids == {"new-1"}

    def test_replace_is_searchable_afterwards(self, storage: GraphStorage):
        storage.add_nodes([_old_node()], [])
        storage.replace_all_nodes_and_edges([_new_node()], [])

        results = storage.search_nodes("New node")
        assert any(n.id == "new-1" for n in results)
        assert all(n.id != "old-1" for n in storage.search_nodes("Old node"))


class TestReplaceDropsStaleVectors:
    def test_replace_clears_every_existing_vector(self, storage: GraphStorage):
        storage.add_nodes([_old_node()], [])
        storage.vector_store.load_vectors({"old-1": [0.1, 0.2, 0.3]})
        assert storage.vector_store.has_embedding("old-1")

        # Reusing the SAME id in the new document is the exact footgun this
        # guards against: without clearing, "new-1-reused" would silently
        # inherit "old-1"'s vector for what may be entirely different text.
        reused = Node(id="old-1", type=NodeType.ACTOR, name="Different content now")
        storage.replace_all_nodes_and_edges([reused], [])

        assert storage.vector_store.embeddings == {}
        assert not storage.vector_store.has_embedding("old-1")


class TestReplaceRollsBackOnSaveFailure:
    def test_failed_save_restores_previous_nodes_and_edges(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        # Drain background saves add_nodes already queued before arming the
        # failure — otherwise the flag can be consumed by one of THOSE saves
        # instead of the replace's, since they run on the same executor and
        # add_nodes does not wait for them.
        storage.flush()
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        # The live graph must be exactly what it was before the failed
        # replace — not the new content, and not some partial mix.
        assert storage.get_node("old-1") is not None
        assert storage.get_node("new-1") is None
        assert [n.id for n in storage.get_all_nodes()] == ["old-1"]

    def test_failed_save_restores_vectors(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        storage.vector_store.load_vectors({"old-1": [0.1, 0.2, 0.3]})
        storage.flush()
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        assert storage.vector_store.has_embedding("old-1")

    def test_failed_save_leaves_the_graph_searchable_as_before(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        storage.flush()
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        results = storage.search_nodes("Old node")
        assert any(n.id == "old-1" for n in results)

    def test_failed_save_never_reached_the_backend(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        storage.flush()
        saves_before = backend.saves
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        # The bootstrap save from __init__/add_nodes already landed; the
        # failed replace must not have landed a further one.
        assert backend.saves == saves_before
        assert {n["id"] for n in backend.data["nodes"]} == {"old-1"}

    def test_failed_save_rolls_back_the_generation_counter(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        """`generation` is part of the swap `_restore()` must undo along with
        nodes/edges/graph/vectors — see `commit_generation_embeddings`. If it
        were left bumped after a failed replace, a job stamped against the
        generation BEFORE this failed attempt would be wrongly treated as
        superseded, even though the graph the failed replace never actually
        committed is exactly what is still live."""
        storage.add_nodes([_old_node()], [])
        storage.flush()
        generation_before = storage.generation
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        assert storage.generation == generation_before

        # And a SUBSEQUENT successful replace still advances it normally —
        # the failed attempt is not silently "used up".
        storage.replace_all_nodes_and_edges([_new_node()], [])
        assert storage.generation == generation_before + 1


class TestReplaceBumpsGeneration:
    def test_generation_starts_at_zero_and_increments_once_per_successful_replace(
        self, storage: GraphStorage
    ):
        assert storage.generation == 0

        storage.replace_all_nodes_and_edges([_old_node()], [])
        assert storage.generation == 1

        storage.replace_all_nodes_and_edges([_new_node()], [])
        assert storage.generation == 2

    def test_ordinary_incremental_writes_do_not_move_the_generation(
        self, storage: GraphStorage
    ):
        storage.replace_all_nodes_and_edges([_old_node()], [])
        generation = storage.generation

        storage.add_nodes([_new_node()], [])
        storage.update_node("old-1", {"name": "Renamed"})

        assert storage.generation == generation


class TestGenerationSurvivesAProcessRestart:
    """`_generation` is a plain in-memory int and resets to 0 whenever a new
    `GraphStorage` object is constructed. A real process restart is exactly
    that: a fresh object reading a persisted store, not the same object
    continuing to run. Without persisting the counter, a crash-recovered
    import job stamped with a real, pre-crash generation would be compared
    against a freshly-reset 0 and wrongly treated as superseded even though no
    later import ever happened — see `backend/agents/tests/test_import_worker.py`
    for that failure mode end-to-end against the real embedding worker."""

    def test_generation_is_zero_for_a_graph_that_was_never_imported_into(
        self, backend: _SnapshotBackend
    ):
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes([_old_node()], [])
        storage.flush()

        restarted = GraphStorage(persistence_backend=backend)

        assert restarted.generation == 0

    def test_generation_round_trips_through_a_fresh_instance_of_the_same_store(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.replace_all_nodes_and_edges([_old_node()], [])
        storage.replace_all_nodes_and_edges([_new_node()], [])
        generation_before_restart = storage.generation
        assert generation_before_restart == 2

        # A NEW GraphStorage object reading the same persisted store — the
        # actual shape of a process restart, as opposed to reusing `storage`,
        # which would tell us nothing about persistence at all.
        restarted = GraphStorage(persistence_backend=backend)

        assert restarted.generation == generation_before_restart

    def test_a_further_replace_after_restart_continues_the_count_rather_than_resetting_it(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.replace_all_nodes_and_edges([_old_node()], [])
        restarted = GraphStorage(persistence_backend=backend)
        assert restarted.generation == 1

        restarted.replace_all_nodes_and_edges([_new_node()], [])

        assert restarted.generation == 2


class TestReplaceIsAtomicUnderConcurrentUnlockedReads:
    """Every OTHER read path on GraphStorage (get_node, get_all_nodes,
    get_all_edges, get_stats, ...) reads ``self.nodes`` / ``self.edges`` /
    ``self.graph`` WITHOUT taking ``_lock`` — safe only as long as a writer
    never leaves those containers in a partially-rebuilt state that such an
    unlocked reader could observe. The original implementation cleared
    ``self.nodes`` / ``self.edges`` / ``self.graph`` in place and refilled
    them in a loop — a window, linear in graph size, in which an unlocked
    reader could see an empty node set or a nodes/edges count mismatch. This
    pins the fix: build the new containers off to the side and publish each
    with a single pointer reassignment, so a concurrent reader only ever sees
    the fully-old graph or the fully-new one."""

    def test_a_concurrent_unlocked_reader_never_observes_a_torn_swap(
        self, storage: GraphStorage
    ):
        import sys

        # Forces the GIL to switch between threads far more often than the
        # 5ms default. Without this, the whole swap — a handful of statements
        # under this fix, but also the FEW MILLISECONDS an in-place
        # clear-then-repopulate of a few thousand nodes took under the bug
        # this guards against — can complete inside a single scheduling slice
        # and never actually hand control to the reader thread at all, which
        # would make this test pass for the wrong reason (no samples taken
        # during the swap) rather than the right one (samples taken, and
        # every one consistent).
        original_interval = sys.getswitchinterval()
        sys.setswitchinterval(0.00005)

        old_ids = {f"old-{i}" for i in range(3000)}
        old_nodes = [Node(id=nid, type=NodeType.ACTOR, name=nid) for nid in old_ids]
        storage.replace_all_nodes_and_edges(old_nodes, [])

        new_ids = {f"new-{i}" for i in range(3000)}
        new_nodes = [Node(id=nid, type=NodeType.ACTOR, name=nid) for nid in new_ids]

        mismatches = []
        samples = [0]
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                nodes = storage.get_all_nodes()
                edges = storage.get_all_edges()
                # `self.graph` is a separate object from `self.nodes`; the
                # original bug rebuilt it with a `.clear()` + a Python-level
                # `for` loop AFTER `self.nodes`/`self.edges` were already
                # fully updated to the new content, so the node COUNT and the
                # graph's own node count could disagree for as long as that
                # loop took — the graph momentarily behind, or momentarily
                # ahead of where the dicts already were, without ever tearing
                # the dicts themselves. Comparing them is what actually
                # exercises that window; comparing `nodes`/`edges` alone would
                # not, since those two were already swapped via a single bulk
                # `dict.update()` each.
                graph_node_count = storage.graph.number_of_nodes()
                samples[0] += 1
                ids = {n.id for n in nodes}
                is_fully_old = (
                    ids == old_ids and len(edges) == 0 and graph_node_count == 3000
                )
                is_fully_new = (
                    ids == new_ids and len(edges) == 0 and graph_node_count == 3000
                )
                if not (is_fully_old or is_fully_new):
                    mismatches.append((len(nodes), len(edges), graph_node_count))
                    if len(mismatches) > 20:
                        stop.set()

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            storage.replace_all_nodes_and_edges(new_nodes, [])
        finally:
            stop.set()
            reader_thread.join(timeout=5)
            sys.setswitchinterval(original_interval)

        assert samples[0] > 0, "the reader thread never ran during the replace"
        assert mismatches == [], (
            f"a concurrent unlocked read observed a torn swap "
            f"(node_count, edge_count, graph_node_count): {mismatches[:5]}"
        )
        assert {n.id for n in storage.get_all_nodes()} == new_ids
