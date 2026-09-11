"""
Unit tests for graph_core vector_store

Note: Some tests require the sentence-transformers model to be loaded,
which may take time on first run. Tests are designed to be skippable
if the model is not available.
"""

import pytest
from unittest.mock import patch
import numpy as np

from backend.core.vector_store import VectorStore
from backend.core.models import Node, NodeType


@pytest.fixture
def temp_vector_store():
    """Create a temporary VectorStore instance"""
    # VectorStore no longer uses storage_path - embeddings are stored in graph.json
    store = VectorStore()
    yield store


@pytest.fixture
def sample_nodes():
    """Create sample nodes for testing"""
    return [
        Node(
            id="node-1",
            type=NodeType.ACTOR,
            name="Swedish Government",
            description="The government of Sweden",
            tags=["government", "sweden"],
        ),
        Node(
            id="node-2",
            type=NodeType.ACTOR,
            name="Norwegian Government",
            description="The government of Norway",
            tags=["government", "norway"],
        ),
        Node(
            id="node-3",
            type=NodeType.INITIATIVE,
            name="Digital Transformation",
            description="A digital transformation initiative",
            tags=["digital", "technology"],
        ),
    ]


class TestVectorStoreInit:
    """Tests for VectorStore initialization"""

    def test_creates_empty_store(self, temp_vector_store):
        """Test that a new store starts empty"""
        assert temp_vector_store.get_embedding_count() == 0

    def test_lazy_model_loading(self, temp_vector_store):
        """Test that model is not loaded until needed"""
        assert temp_vector_store.model is None

    def test_default_model_name(self, temp_vector_store):
        """Test that default model name is set correctly"""
        assert temp_vector_store.model_name == "all-MiniLM-L6-v2"


class TestVectorStoreTextRepresentation:
    """Tests for text representation generation"""

    def test_get_text_representation(self, temp_vector_store):
        """Test text representation includes all fields"""
        node = Node(
            type=NodeType.ACTOR,
            name="Test Actor",
            description="A test description",
            summary="Test summary",
            tags=["tag1", "tag2"],
        )

        text = temp_vector_store._get_text_representation(node)

        assert "Test Actor" in text
        assert "test description" in text
        assert "Test summary" in text
        assert "tag1" in text
        assert "tag2" in text

    def test_get_text_representation_minimal(self, temp_vector_store):
        """Test text representation with minimal fields"""
        node = Node(type=NodeType.ACTOR, name="Minimal Node")

        text = temp_vector_store._get_text_representation(node)

        assert "Minimal Node" in text


class TestVectorStoreEmbeddings:
    """Tests for embedding generation and storage"""

    @pytest.mark.slow
    def test_generate_embedding(self, temp_vector_store, sample_nodes):
        """Test generating embedding for a single node"""
        node = sample_nodes[0]
        embedding = temp_vector_store.generate_embedding(node)

        assert embedding is not None
        assert isinstance(embedding, np.ndarray)
        assert len(embedding) == 384  # all-MiniLM-L6-v2 produces 384-dim embeddings

    @pytest.mark.slow
    def test_update_node_embedding(self, temp_vector_store, sample_nodes):
        """Test updating embedding for a node"""
        node = sample_nodes[0]
        temp_vector_store.update_node_embedding(node)

        assert temp_vector_store.has_embedding(node.id)
        assert temp_vector_store.get_embedding_count() == 1

    @pytest.mark.slow
    def test_update_nodes_embeddings_batch(self, temp_vector_store, sample_nodes):
        """Test batch updating embeddings"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        assert temp_vector_store.get_embedding_count() == 3
        for node in sample_nodes:
            assert temp_vector_store.has_embedding(node.id)

    @pytest.mark.slow
    def test_remove_node_embedding(self, temp_vector_store, sample_nodes):
        """Test removing a single embedding"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)
        temp_vector_store.remove_node_embedding("node-1")

        assert not temp_vector_store.has_embedding("node-1")
        assert temp_vector_store.get_embedding_count() == 2

    @pytest.mark.slow
    def test_remove_nodes_embeddings(self, temp_vector_store, sample_nodes):
        """Test removing multiple embeddings"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)
        temp_vector_store.remove_nodes_embeddings(["node-1", "node-2"])

        assert not temp_vector_store.has_embedding("node-1")
        assert not temp_vector_store.has_embedding("node-2")
        assert temp_vector_store.has_embedding("node-3")

    def test_remove_nonexistent_embedding(self, temp_vector_store):
        """Test removing a non-existent embedding is safe"""
        # Should not raise an error
        temp_vector_store.remove_node_embedding("nonexistent")
        temp_vector_store.remove_nodes_embeddings(["nonexistent"])


class TestVectorStoreSearch:
    """Tests for semantic search"""

    @pytest.mark.slow
    def test_search_by_text(self, temp_vector_store, sample_nodes):
        """Test searching by text query"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        results = temp_vector_store.search(query_text="government", limit=5)

        assert len(results) > 0
        # Results should be sorted by score descending
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.slow
    def test_search_by_node(self, temp_vector_store, sample_nodes):
        """Test searching by existing node"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        # Search for nodes similar to Swedish Government
        results = temp_vector_store.search(query_node=sample_nodes[0], limit=5)

        assert len(results) > 0
        # The query node itself should not be in results
        result_ids = [node_id for node_id, _ in results]
        assert "node-1" not in result_ids

    @pytest.mark.slow
    def test_search_with_threshold(self, temp_vector_store, sample_nodes):
        """Test search with similarity threshold"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        results = temp_vector_store.search(
            query_text="government", threshold=0.5, limit=5
        )

        # All results should be above threshold
        for _, score in results:
            assert score >= 0.5

    @pytest.mark.slow
    def test_search_limit(self, temp_vector_store, sample_nodes):
        """Test search result limit"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        results = temp_vector_store.search(query_text="government", limit=1)

        assert len(results) <= 1

    def test_search_empty_store(self, temp_vector_store):
        """Test searching an empty store returns empty list"""
        results = temp_vector_store.search(query_text="government")
        assert results == []

    def test_search_no_query(self, temp_vector_store):
        """Test search without query returns empty list"""
        results = temp_vector_store.search()
        assert results == []


class TestVectorStoreRebuild:
    """Tests for rebuilding index from nodes"""

    def test_rebuild_index_reads_vectors_carried_on_the_nodes(self, sample_nodes):
        """rebuild_index is the pre-sidecar path: it ingests vectors that a
        graph written before the split still carries on its node objects."""
        for i, node in enumerate(sample_nodes):
            node.embedding = [float(i), 1.0 - i, 0.5]

        store = VectorStore()
        store.rebuild_index(sample_nodes)

        assert store.get_embedding_count() == 3
        for i, node in enumerate(sample_nodes):
            assert store.has_embedding(node.id)
            np.testing.assert_allclose(
                store.embeddings[node.id], np.float32([float(i), 1.0 - i, 0.5])
            )

    @pytest.mark.slow
    def test_generated_vectors_stay_off_the_node_object(self, sample_nodes):
        """The vector store owns the vectors; leaving a copy on the node is
        what made a node cost ~51 kB resident and bloated every snapshot."""
        store = VectorStore()
        store.update_nodes_embeddings(sample_nodes)

        assert store.get_embedding_count() == 3
        for node in sample_nodes:
            assert node.embedding is None

    @pytest.mark.slow
    def test_rebuild_empty_nodes(self):
        """Test rebuilding with nodes that have no embeddings"""
        nodes = [
            Node(id="no-embed-1", type=NodeType.ACTOR, name="No Embedding 1"),
            Node(id="no-embed-2", type=NodeType.ACTOR, name="No Embedding 2"),
        ]

        store = VectorStore()
        store.rebuild_index(nodes)

        # Should have no embeddings since nodes had none
        assert store.get_embedding_count() == 0


class TestVectorStorePersistenceSeam:
    """The store is the owner of the vectors; GraphStorage persists them via
    these methods and uses `revision` to skip writes that change nothing."""

    def _store(self):
        store = VectorStore()
        store.load_vectors({"n1": [1.0, 0.0], "n2": [0.0, 1.0]})
        return store

    def test_load_vectors_replaces_the_index(self):
        store = self._store()
        store.load_vectors({"n3": [1.0, 1.0]})

        assert set(store.embeddings) == {"n3"}

    def test_vectors_are_held_as_float32(self):
        store = self._store()

        assert store.embeddings["n1"].dtype == np.float32

    def test_export_vectors_round_trips_through_load_vectors(self):
        store = self._store()
        exported = store.export_vectors()

        other = VectorStore()
        other.load_vectors(exported)

        assert set(other.embeddings) == set(store.embeddings)
        np.testing.assert_allclose(other.embeddings["n1"], store.embeddings["n1"])

    def test_get_vector_list_returns_json_serialisable_values(self):
        store = self._store()

        assert store.get_vector_list("n1") == [1.0, 0.0]
        assert store.get_vector_list("missing") is None

    def test_matrix_rows_follow_node_ids_not_sorted_order(self):
        """Row i of the matrix must belong to node_ids[i]. Both are built from
        the same dict, so nothing pinned it - and a matrix stacked in sorted
        order while node_ids kept insertion order passes every other test,
        because their fixtures insert ids that are already sorted. It hands one
        node's vector to another: the query node's own self-similarity turns up
        attributed to whichever id sits at that row.
        """
        store = VectorStore()
        # Insertion order differs from sorted order (alpha, mid, zeta).
        store.load_vectors({"zeta": [1.0, 0.0], "alpha": [0.9, 0.1], "mid": [0.0, 1.0]})

        assert store.node_ids == ["zeta", "alpha", "mid"]
        assert store.node_ids == list(store.embeddings)
        for row, node_id in enumerate(store.node_ids):
            # The rows are unit length, so the vector as given is compared
            # against its own direction rather than against itself. That is the
            # only difference; which row belongs to which id is the property.
            vector = store.embeddings[node_id]
            np.testing.assert_allclose(
                store.unit_matrix[row],
                vector / np.linalg.norm(vector),
                rtol=1e-6,
                atol=1e-6,
            )

        # The consequence a user would see: zeta's nearest neighbour is alpha.
        # Under the sorted-stacking mismatch it comes back as mid at exactly
        # 1.0, which is zeta's own vector wearing mid's id.
        results = store.search(
            query_node=Node(id="zeta", type=NodeType.ACTOR, name="Z")
        )
        assert results[0][0] == "alpha"
        assert results[0][1] < 1.0

    def test_revision_advances_on_every_change(self):
        store = self._store()
        start = store.revision

        store.load_vectors({"n1": [1.0, 0.0]})
        after_load = store.revision
        assert after_load > start

        store.remove_node_embedding("n1")
        assert store.revision > after_load

    def test_revision_is_unchanged_by_a_read(self):
        store = self._store()
        before = store.revision

        store.export_vectors()
        store.get_vector_list("n1")
        store.search(query_text=None, query_node=None)

        assert store.revision == before

    def test_removing_an_absent_id_does_not_advance_the_revision(self):
        """A no-op removal must not make GraphStorage rewrite the sidecar."""
        store = self._store()
        before = store.revision

        store.remove_nodes_embeddings(["not-here"])

        assert store.revision == before


class TestVectorStoreMatrix:
    """Tests for embedding matrix operations"""

    @pytest.mark.slow
    def test_matrix_updated_on_add(self, temp_vector_store, sample_nodes):
        """Test that embedding matrix is updated when adding nodes"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        assert temp_vector_store.unit_matrix is not None
        assert temp_vector_store.unit_matrix.shape[0] == 3

    @pytest.mark.slow
    def test_matrix_updated_on_remove(self, temp_vector_store, sample_nodes):
        """Test that embedding matrix is updated when removing nodes"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)
        temp_vector_store.remove_node_embedding("node-1")

        assert temp_vector_store.unit_matrix.shape[0] == 2

    def test_empty_matrix(self, temp_vector_store):
        """Test that empty store has no matrix"""
        assert temp_vector_store.unit_matrix is None
        assert temp_vector_store.node_ids == []


class TestVectorStoreNumpySearch:
    """Semantic search runs on numpy alone — no ML extras (torch /
    sentence-transformers) required. Regression for STRUCTURE_REVIEW.md A2,
    which moved those extras out of the base requirements."""

    def _store_with_embeddings(self):
        # Deterministic embeddings set directly on the nodes, so no embedding
        # model is loaded at any point in these tests.
        nodes = [
            Node(id="n1", type=NodeType.ACTOR, name="A"),
            Node(id="n2", type=NodeType.ACTOR, name="B"),
            Node(id="n3", type=NodeType.ACTOR, name="C"),
        ]
        nodes[0].embedding = [1.0, 0.0, 0.0]
        nodes[1].embedding = [0.9, 0.1, 0.0]
        nodes[2].embedding = [0.0, 0.0, 1.0]
        store = VectorStore()
        store.rebuild_index(nodes)
        return store, nodes

    def test_cosine_similarity_matrix_matches_expected(self):
        from backend.core.vector_store import _cosine_to_unit_rows

        store, _ = self._store_with_embeddings()
        query = np.array([[1.0, 0.0, 0.0]])
        sims = _cosine_to_unit_rows(query, store.unit_matrix)

        assert sims.shape == (3,)
        assert sims[0] == pytest.approx(1.0, abs=1e-6)  # identical vector
        assert sims[2] == pytest.approx(0.0, abs=1e-6)  # orthogonal vector
        assert sims[0] > sims[1] > sims[2]

    def test_search_by_node_uses_numpy_only(self):
        """Searching by an already-embedded node needs only numpy; the model
        must never be loaded."""
        store, nodes = self._store_with_embeddings()

        with patch.object(
            store, "_load_model", side_effect=AssertionError("model must not load")
        ):
            results = store.search(query_node=nodes[0], limit=5)

        result_ids = [node_id for node_id, _ in results]
        assert "n1" not in result_ids  # query node itself excluded
        assert result_ids[0] == "n2"  # nearest neighbour ranked first

    def test_search_by_text_degrades_when_model_missing(self):
        """Query-text search embeds the query with the ML model; when that
        model is unavailable, search returns no semantic hits instead of
        raising."""
        store, _ = self._store_with_embeddings()

        def missing_model():
            raise ImportError("No module named 'sentence_transformers'")

        with patch.object(store, "_load_model", side_effect=missing_model):
            results = store.search(query_text="anything", limit=5)

        assert results == []


# Skip slow tests by default, run with: pytest -m slow
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (require model loading)"
    )


def test_absorb_refuses_a_mixed_width_batch_and_leaves_the_index_alone():
    """This raise is the single enforcement point of the one-width invariant the
    whole sidecar split rests on: every consumer was allowed to drop its own
    width guard because entry is guarded here. add_nodes swallows the exception,
    so without it a mixed batch leaves the index permanently mixed and the
    sidecar silently unwritable — with nothing failing at the time."""
    store = VectorStore()
    store.load_vectors({"a": np.ones(4, dtype=np.float32)})
    before = store.export_vectors()

    with pytest.raises(ValueError):
        store._absorb({"b": [1.0, 2.0], "c": [1.0, 2.0, 3.0]})

    after = store.export_vectors()
    assert set(after) == set(before), "a refused batch still changed the index"
    np.testing.assert_allclose(after["a"], before["a"])


class TestSearchCostsNothingItDoesNotHaveTo:
    """What normalising the index once instead of once per query has to buy,
    and what it must not cost.

    The rows are unit length before a query arrives, so a search multiplies
    against them rather than building its own normalised copy of the whole
    matrix first. That copy was the largest allocation the class made and it
    was made on every search: 147 MiB per query at 100k nodes of width 384,
    recomputed identically until the index changed.
    """

    @staticmethod
    def _store(count, dim=64, seed=3):
        rng = np.random.default_rng(seed)
        store = VectorStore()
        store.load_vectors(
            {f"n{i}": rng.random(dim).astype(np.float32) for i in range(count)}
        )
        return store

    def test_a_query_allocates_nothing_the_size_of_the_index(self):
        """The property, measured rather than reasoned about: what one search
        allocates must not grow with the matrix. It is checked against the
        matrix's own size so the assertion cannot pass by the index being
        small."""
        import tracemalloc

        store = self._store(4000, dim=256)
        rows = len(store.node_ids)
        # What a query legitimately needs is proportional to the NUMBER of
        # nodes - the similarities, their negation, and the argsort's output -
        # and not to the size of the index. Budgeting against the matrix's
        # bytes instead leaves room for a per-query allocation that is O(n) in
        # Python objects rather than in numpy: measured, ordering with
        # `sorted(range(n), key=...)` returns identical results at five times
        # the peak, and passed a matrix-derived budget at every size.
        budget = rows * 64
        probe = store.embeddings["n0"]

        probe_node = Node(id="n0", type=NodeType.ACTOR, name="n0")
        assert probe is store.embeddings["n0"], "the probe is not the indexed row"
        store.search(query_node=probe_node, limit=10)  # outside the measurement
        tracemalloc.start()
        store.search(query_node=probe_node, limit=10)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < budget, (
            f"one search allocated {peak} bytes for {rows} rows, over the "
            f"{budget}-byte budget: it is allocating per node rather than per "
            f"query, or copying the matrix again"
        )
        # And the budget is not passing by being generous: the index it is
        # measured against is far larger than it.
        assert budget < store.unit_matrix.nbytes / 8

    def test_results_are_what_the_normalise_per_query_form_returned(self):
        """Equivalence with the form this replaced, computed here rather than
        recalled: normalise both sides at query time, rank in Python, and
        require the shipped path to agree - ids, order and scores."""
        store = self._store(300, dim=32, seed=11)
        query = np.asarray(store.embeddings["n7"]).reshape(1, -1)

        matrix = np.vstack([store.embeddings[nid] for nid in store.node_ids])
        q = query / (np.linalg.norm(query, axis=1, keepdims=True) + 1e-12)
        m = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12)
        sims = (q @ m.T)[0]
        expected = [
            (store.node_ids[i], float(score))
            for i, score in enumerate(sims)
            if score >= 0.72
        ]
        expected.sort(key=lambda pair: pair[1], reverse=True)
        expected = [pair for pair in expected if pair[0] != "n7"][:25]

        actual = store.search(
            query_node=Node(id="n7", type=NodeType.ACTOR, name="n7"),
            limit=25,
            threshold=0.72,
        )

        assert [node_id for node_id, _ in actual] == [
            node_id for node_id, _ in expected
        ]
        np.testing.assert_allclose(
            [score for _, score in actual],
            [score for _, score in expected],
            rtol=1e-5,
            atol=1e-6,
        )

    def test_equal_scores_keep_index_order(self):
        """The tie-break callers have been seeing. The list-and-sort this
        replaced used Python's stable sort over rows appended in index order,
        so equal scores came back in index order; an unstable sort would
        reorder them for no reason a caller could see.

        Ties MIXED WITH distinct scores, and enough of them, because neither
        half alone can tell the two sorts apart: an all-equal array is left
        untouched by quicksort's partitioning, and three rows are inside the
        insertion-sort fallback. Measured: fifty rows over two distinct scores
        is where `quicksort` first disagrees with `stable` here.
        """
        rng = np.random.default_rng(3)
        scores = rng.integers(0, 2, size=50)
        store = VectorStore()
        # Two directions only, so half the rows tie with each other exactly.
        store.load_vectors(
            {
                f"n{i}": ([1.0, 0.0] if score else [0.0, 1.0])
                for i, score in enumerate(scores)
            }
        )

        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        store._update_matrix()
        results = store.search(query_node=probe, limit=50)

        expected = [f"n{i}" for i, score in enumerate(scores) if score] + [
            f"n{i}" for i, score in enumerate(scores) if not score
        ]
        assert [node_id for node_id, _ in results] == expected

    def test_a_zero_row_does_not_become_a_nan(self):
        """A zero vector has no direction. The per-query form divided by the
        norm plus an epsilon, which left the row at zero rather than at nan,
        and pre-normalising has to keep doing that - a nan would poison every
        comparison it takes part in."""
        store = VectorStore()
        store.load_vectors({"zero": [0.0, 0.0], "real": [1.0, 0.0]})

        assert not np.isnan(store.unit_matrix).any()
        np.testing.assert_allclose(store.unit_matrix[0], [0.0, 0.0])

        results = store.search(
            query_node=Node(id="real", type=NodeType.ACTOR, name="real"), limit=5
        )
        assert dict(results)["zero"] == pytest.approx(0.0, abs=1e-6)

    def test_the_threshold_excludes_rather_than_just_ordering(self):
        """The threshold is a filter, and this is the only test of it that
        RUNS: the existing one is marked slow and needs the embedding model, so
        on the base install it skips - which is how a break that admitted a NaN
        got past the suite once already. Nothing here needs a model."""
        store = VectorStore()
        store.load_vectors(
            {
                "same": [1.0, 0.0],
                "half": [1.0, 1.0],
                "orthogonal": [0.0, 1.0],
            }
        )
        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        store._update_matrix()

        assert [node_id for node_id, _ in store.search(query_node=probe, limit=9)] == [
            "same",
            "half",
            "orthogonal",
        ]
        # 0.707 for half, 0.0 for orthogonal: a threshold between them keeps
        # one and drops the other, so a threshold that stopped working shows up
        # as a longer list rather than as a different order.
        assert [
            node_id
            for node_id, _ in store.search(query_node=probe, limit=9, threshold=0.5)
        ] == ["same", "half"]
        assert store.search(query_node=probe, limit=9, threshold=1.5) == []

    def test_a_nan_score_is_dropped_rather_than_returned(self):
        """A NaN is neither above the threshold nor below it. The filter this
        replaced asked `score >= threshold`, which is False for a NaN and
        dropped it; asking `score < threshold` instead is False too, and admits
        it. It is not hypothetical: one corrupt float in the sidecar loads as a
        NaN, and a caller formats the score as `int(score * 100)`, which raises
        on one."""
        store = VectorStore()
        store.load_vectors(
            {"good": [1.0, 0.0], "bad": [float("nan"), 0.0], "other": [0.0, 1.0]}
        )
        assert np.isnan(store.unit_matrix).any(), "the fixture has no NaN in it"

        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        store._update_matrix()

        for result in store.search(query_node=probe, limit=9, threshold=0.4):
            assert not np.isnan(result[1]), f"a NaN score reached the caller: {result}"
        # And with room to spare: the walk runs out of real scores before the
        # limit, which is exactly when a NaN would be reached.
        for result in store.search(query_node=probe, limit=99, threshold=-1.0):
            assert not np.isnan(result[1]), f"a NaN score reached the caller: {result}"

    def test_asking_for_no_results_returns_none(self):
        """`limit=0` reaches here unguarded from search_graph over MCP. The
        slice this replaced returned nothing for it, and a count checked after
        the append returns one - the first candidate is already in the list by
        the time anything asks."""
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [0.0, 1.0]})
        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        store._update_matrix()

        assert store.search(query_node=probe, limit=0) == []
        assert store.search(query_node=probe, limit=-1) == []
        assert len(store.search(query_node=probe, limit=1)) == 1

    def test_the_query_node_does_not_use_one_of_the_slots(self):
        """It is dropped, not counted. The slice this replaced dropped it
        before slicing, so a caller asking for two got two."""
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [0.8, 0.2]})
        probe = Node(id="a", type=NodeType.ACTOR, name="a")

        assert [node_id for node_id, _ in store.search(query_node=probe, limit=2)] == [
            "b",
            "c",
        ]

    def test_emptying_the_index_lets_go_of_the_matrix(self):
        """Removing every vector has to release the matrix, not just stop
        finding it. `search` returns early on an empty `embeddings`, so a
        matrix left behind is invisible to every assertion about results - and
        it is the largest allocation this class makes, on the axis this whole
        change is about."""
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.0, 1.0]})
        assert store.unit_matrix is not None

        store.remove_nodes_embeddings(["a", "b"])

        assert store.embeddings == {}
        assert store.unit_matrix is None, "the matrix outlived every vector in it"
        assert store.node_ids == []

    def test_the_matrix_stays_float32(self):
        """Width times four bytes is what the sizing in
        docs/DATA_MANAGEMENT.md counts on, and a float64 matrix is twice the
        resident cost for no gain - cosine over normalised rows does not need
        the precision. The allocation budget above is derived from the
        matrix's own size, so it scales with this rather than catching it."""
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.0, 1.0]})

        assert store.unit_matrix.dtype == np.float32

    def test_a_query_with_no_direction_is_not_a_nan_either(self):
        """The zero-vector case on the OTHER side. A zero row in the index is
        covered above; a zero QUERY divides by its own norm, and the epsilon
        that keeps the index safe is a separate one. Reachable the same way -
        a corrupt row read back from the sidecar and then used as the query
        node's own vector."""
        store = VectorStore()
        store.load_vectors({"real": [1.0, 0.0], "other": [0.0, 1.0]})

        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([0.0, 0.0], dtype=np.float32)
        store._update_matrix()
        results = store.search(query_node=probe, limit=9, threshold=-1.0)

        assert results, "the zero query returned nothing at all"
        for node_id, score in results:
            assert not np.isnan(score), f"{node_id} scored nan against a zero query"

    def test_a_score_is_a_float_the_rest_of_the_stack_can_serialise(self):
        """`np.float32` compares equal to a float and formats like one, so
        every assertion about scores passes either way - and then json.dumps
        raises on it. The conversion is load-bearing at an API boundary, which
        is the one place nothing here would otherwise look."""
        import json

        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.0, 1.0]})
        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        store._update_matrix()

        results = store.search(query_node=probe, limit=9, threshold=-1.0)
        assert results
        for _, score in results:
            # `is float`, not isinstance: np.float64 is a subclass of float and
            # would satisfy isinstance while still being the wrong thing here.
            assert type(score) is float, f"score is {type(score).__name__}"
        json.dumps(results)

    def test_an_index_that_was_emptied_is_not_searched(self):
        """The guard that stops a query reaching a matrix that is not there.
        It IS covered today, but only by api_host and service tests that
        happen to have an embedder - delete it and this module still passes.
        The class that owns the invariant should be the one that fails."""
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0]})
        store.remove_node_embedding("a")

        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        # Deliberately NOT rebuilt: an index whose matrix and dict disagree is
        # exactly the state the guard exists for.
        store.unit_matrix = None
        assert store.search(query_node=probe, limit=5) == []
