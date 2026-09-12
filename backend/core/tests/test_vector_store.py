"""
Unit tests for graph_core vector_store

Note: Some tests require the sentence-transformers model to be loaded,
which may take time on first run. Tests are designed to be skippable
if the model is not available.
"""

import sys
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

    def test_cosine_to_unit_rows_matches_expected(self):
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
        allocates must not grow with the matrix. Budgeted against the NUMBER of
        rows rather than against the matrix's bytes - see the comment below for
        what a bytes-derived budget let through - with a second assertion that
        the budget is far under the index, so it cannot pass by being
        generous."""
        import tracemalloc

        store = self._store(4000, dim=256)
        rows = len(store.node_ids)
        # What a query legitimately needs is proportional to the NUMBER of
        # nodes - the similarities, their negation, and the argsort's output -
        # and not to the size of the index. So the budget is sized to the
        # shipped path's measured peak of 17.50 bytes a row, not to something
        # comfortable: a loose budget admits an ordering that is O(n) in
        # Python objects rather than in numpy, which is what G2 is about.
        # Measured at this fixture's own size, both returning results identical
        # to the shipped path: `argsort(...).tolist()` costs 50.07 bytes a row
        # (2.9x) and `sorted(range(n), key=...)` 82.05 (4.7x). The first fits
        # inside the 64-byte-a-row budget this replaced; the second does not,
        # but it did fit a budget derived from the matrix's BYTES at every size
        # tried, which is the budget shape this one exists to reject.
        budget = rows * 20

        probe_node = Node(id="n0", type=NodeType.ACTOR, name="n0")
        # The FIRST search after the index was built, inside the measurement.
        # A warm-up outside it hides anything cached per index revision - and
        # `_update_matrix` runs on every add and every remove, so a workload
        # that writes between searches pays such a cache every time. Measured,
        # caching the transposed matrix per revision peaks at 155 MB on a cold
        # search at 100k x 384 and at nothing at all on a warm one.
        tracemalloc.start()
        store.search(query_node=probe_node, limit=10)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < budget, (
            f"one search allocated {peak} bytes for {rows} rows, over the "
            f"{budget}-byte budget: it is allocating per node rather than per "
            f"query, or copying the matrix again"
        )

        # Again at the shape production asks for. The measurement above uses
        # the default threshold of 0.0, and so did every other allocation
        # budget here - so a pre-filter gated on `threshold > 0` allocated 33
        # bytes a row against this 20-byte budget and no test looked.
        tracemalloc.start()
        store.search(query_node=probe_node, limit=200, threshold=0.3)
        _, floored_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert floored_peak < budget, (
            f"a search above a 0.3 floor allocated {floored_peak} bytes for "
            f"{rows} rows, over the {budget}-byte budget: the floor is being "
            f"applied by building something the size of the index"
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

        # And the WHOLE ranking, not just the head of it. Every ordering
        # assertion above stops at a few dozen rows, so an edit that leaves the
        # top intact and scrambles the tail passes them all - `np_order[50:]`
        # re-sorted into index order survives the entire suite otherwise. That
        # tail is a live path: `search_graph` defaults to limit=50 and
        # `storage_search` over-fetches limit*4, so ranks 51-200 are what a
        # default search actually consumes. 300 rows costs nothing.
        every_row = store.search(
            query_node=Node(id="n7", type=NodeType.ACTOR, name="n7"),
            limit=len(store.node_ids),
            threshold=-2.0,
        )
        full_expected = [
            store.node_ids[i]
            for i in np.argsort(-sims, kind="stable")
            if store.node_ids[i] != "n7"
        ]
        assert [node_id for node_id, _ in every_row] == full_expected, (
            "the ranking past the head does not match the form this replaced"
        )

    def test_the_text_path_scores_what_the_node_path_would_have(self):
        """The query_text branch, on its scores rather than its bytes.

        `test_a_text_query_does_not_promote_the_index_either` watches that
        branch's ALLOCATION, and the 3-row text tests check directions - so
        nothing pinned what the text path actually returns. Narrowing only that
        query to float16 passed the whole suite while costing 206 eps of score
        accuracy and moving 3 of the top 50 ids.

        Both of those numbers are fixture-specific, which is why this seed is
        not arbitrary: at seed 21 the same mutation moves NO ids at all (185
        eps, identical order), so the id assertion below would have been dead
        weight and the score assertion would have been carrying the test alone.
        Seed 3 puts teeth in both.

        The stub returns float32, which is what the shipped model returns, so
        this is the real path rather than a hypothetical one."""
        store = self._store(2000, dim=128, seed=3)
        row = np.asarray(store.embeddings["n0"], dtype=np.float32)

        class _Model:
            def encode(self, text):
                return row

        store.model = _Model()

        by_text = store.search(query_text="anything", limit=50, threshold=-2.0)
        by_node = store.search(
            query_node=Node(id="n0", type=NodeType.ACTOR, name="n0"),
            limit=50,
            threshold=-2.0,
        )

        # n0 is in the index, so the node query drops it from its own results
        # and takes one more row to reach 50, while the text query keeps n0 and
        # therefore stops one row earlier. Compare the common prefix.
        text_ids = [i for i, _ in by_text if i != "n0"]
        node_ids = [i for i, _ in by_node][: len(text_ids)]
        assert len(text_ids) == 49
        assert text_ids == node_ids, (
            "the text path ranks the same vector differently from the node path"
        )
        shared = {i: s for i, s in by_node}
        for node_id, score in by_text:
            if node_id in shared:
                assert abs(score - shared[node_id]) < 4 * float(
                    np.finfo(np.float32).eps
                ), f"{node_id}: text path scored {score}, node path {shared[node_id]}"

        # And the whole ranking on THIS path, because this is the one that
        # ships: `storage_search.py` reaches the index only through
        # `query_text`, and over-fetches limit*4, so ranks 51-200 of a default
        # search are consumed by production and were checked by nothing. The
        # equivalence test covers the full ranking too, but at 300 rows of
        # width 32 via `query_node` - a reorder gated on a larger index, a
        # different width, or on the text branch slipped past all of it.
        eps = float(np.finfo(np.float32).eps)
        every_row = store.search(
            query_text="anything", limit=len(store.node_ids), threshold=-2.0
        )
        assert len(every_row) == len(store.node_ids)

        query = np.asarray(row, dtype=np.float64).reshape(1, -1)
        query = query / (np.linalg.norm(query, axis=1, keepdims=True) + 1e-12)
        rows = np.vstack(
            [np.asarray(store.embeddings[i], dtype=np.float64) for i in store.node_ids]
        )
        rows = rows / (np.linalg.norm(rows, axis=1, keepdims=True) + 1e-12)
        reference = {
            node_id: float((query @ rows.T)[0][i])
            for i, node_id in enumerate(store.node_ids)
        }
        for (first, first_score), (second, second_score) in zip(
            every_row, every_row[1:]
        ):
            if reference[first] < reference[second]:
                gap = reference[second] - reference[first]
                assert first_score == second_score or gap < 4 * eps, (
                    f"{second} outranks {first} in float64 by {gap}, wider than "
                    f"float32 rounding accounts for, yet came back after it - "
                    f"at rank {every_row.index((first, first_score))} of "
                    f"{len(every_row)}, past what any other test inspects"
                )

    def test_equal_scores_keep_index_order(self):
        """The tie-break callers have been seeing. The list-and-sort this
        replaced used Python's stable sort over rows appended in index order,
        so equal scores came back in index order; an unstable sort would
        reorder them for no reason a caller could see.

        Ties MIXED WITH distinct scores, because neither half alone can tell
        the two sorts apart: an all-equal array is left untouched by
        quicksort's partitioning however long it is, and three rows are inside
        the insertion-sort fallback. Measured with this construction, the two
        sorts first disagree at FOUR data rows; fifty is margin, not the
        boundary.
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
        # And at a limit far below the row count, which is where a partial
        # selection would be used: `argpartition` returns the top k as a SET
        # in no particular order, so it changes which rows come back and not
        # merely their order. A limit at or above n never exercises it.
        assert [
            node_id for node_id, _ in store.search(query_node=probe, limit=10)
        ] == expected[:10]

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

        for threshold in (0.4, -1.0, float("-inf")):
            results = store.search(query_node=probe, limit=99, threshold=threshold)
            # Three assertions, because each catches a different way of being
            # wrong. No NaN in the output is the obvious one. The NaN-bearing
            # id being ABSENT is the second: substituting a finite value for
            # the NaN would satisfy the first while inventing a score. And the
            # rest still being there is the third: `argsort` puts NaN last and
            # the walk stops at the first rejection, so an ordering that put
            # NaN first would let one corrupt row suppress every result.
            assert not any(np.isnan(score) for _, score in results), results
            assert "bad" not in dict(results), (
                f"the NaN row came back with a fabricated score: {results}"
            )
            assert "good" in dict(results), (
                f"one NaN row suppressed the whole result: {results}"
            )

    def test_a_threshold_that_is_not_a_number_matches_nothing(self):
        """`nan >= nan` is False, so the filter this replaced rejected every
        row against a NaN threshold. A predicate written as `score < threshold
        or score != score` drops NaN scores - which is what makes it look
        right - and then admits everything when the THRESHOLD is the NaN."""
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.0, 1.0]})
        probe = Node(id="probe", type=NodeType.ACTOR, name="probe")
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        store._update_matrix()

        assert store.search(query_node=probe, limit=9, threshold=float("nan")) == []

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
        # At a threshold nothing can fail, too: dropping the query node by
        # scoring it -inf rather than by skipping it passes every finite
        # threshold and hands it back here.
        assert "a" not in dict(
            store.search(query_node=probe, limit=9, threshold=float("-inf"))
        )

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
        the precision. Asserted here rather than left to the allocation budget:
        that budget counts rows, so it says nothing about how wide a row is."""
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
        # And it is still dropped from its own results. A drop keyed on the
        # score being 1.0 rather than on the id looks right everywhere else -
        # a node matches itself exactly - and fails precisely here, where the
        # query has no direction to match.
        assert "probe" not in dict(results)

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
            # `is float`, not isinstance: np.float64 subclasses float, so it
            # satisfies isinstance - and json.dumps takes it, so it is not what
            # this is about either. np.float32 does NOT subclass float and is
            # what raises; `is float` is the assertion that says so directly.
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
        # Written WITHOUT rebuilding, so the dict holds a row and the matrix
        # holds nothing: that is the state the guard is for, and the half a
        # guard of `not self.embeddings` alone would walk straight past.
        store.embeddings["probe"] = np.asarray([1.0, 0.0], dtype=np.float32)
        assert store.unit_matrix is None
        assert store.search(query_node=probe, limit=5) == []

    def test_the_rows_are_rebuilt_when_the_index_changes_without_growing(self):
        """Row order is pinned above for a fresh index. This is the churn: a
        removal and an addition leave the COUNT unchanged, and a re-embed
        changes no id at all, so an index that rebuilt only on a size change
        would keep serving directions that belong to nothing."""

        class _Model:
            def encode(self, text):
                return np.asarray([0.0, 1.0], dtype=np.float32)

        store = VectorStore()
        store.load_vectors({"keep": [0.0, 1.0], "drop": [1.0, 0.0]})
        store.model = _Model()

        store.remove_node_embedding("drop")
        store._absorb({"added": np.asarray([1.0, 0.0], dtype=np.float32)})
        assert store.node_ids == ["keep", "added"]
        for row, node_id in enumerate(store.node_ids):
            vector = store.embeddings[node_id]
            np.testing.assert_allclose(
                store.unit_matrix[row], vector / np.linalg.norm(vector), atol=1e-6
            )

        # `keep` points the way the query does, so it is found.
        assert dict(
            store.search(query_text="the query's direction", limit=9, threshold=0.5)
        )

        # Same ids, same count, a different direction for one of them - and
        # the FIRST of the two deliberately: re-embedding the last one and then
        # asserting the order cannot fail, because moving it to the end leaves
        # it where it was. Read WITHOUT anything that would change the count
        # again, since a later rebuild repairs the staleness before it can be
        # observed; a text query needs no extra row.
        store._absorb({"keep": np.asarray([1.0, 0.0], dtype=np.float32)})

        # Re-embedding a node must not MOVE it: node_ids is the tie order
        # callers see, so an absorb that re-inserted the id at the end would
        # reorder equal scores for a change that altered no ranking.
        assert store.node_ids == ["keep", "added"]
        assert (
            dict(
                store.search(query_text="the query's direction", limit=9, threshold=0.5)
            )
            == {}
        ), "a re-embedded node still matches the direction it used to have"

    def test_a_text_query_can_reach_every_row(self):
        """The `query_text` branch, which nothing else here exercises: every
        other case queries BY A NODE that is itself in the index, so the
        query-node drop already caps the answer at n-1 and a limit silently
        capped at n-1 would look correct. A stub stands in for the model, which
        is not installed on the base install."""

        class _Model:
            def encode(self, text):
                return np.asarray([1.0, 0.0], dtype=np.float32)

        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [0.8, 0.2]})
        store.model = _Model()

        results = store.search(query_text="anything", limit=3, threshold=-1.0)
        assert [node_id for node_id, _ in results] == ["a", "b", "c"], (
            f"a text query reached {len(results)} of {len(store.node_ids)} rows"
        )

    def test_a_query_node_absent_from_the_index_reaches_every_row(self):
        """The sibling of the text-query case. A limit capped at n-1 is
        invisible whenever the query node is IN the index, because the drop
        already costs a row - so the cap has to be probed from outside it.
        The node carries its own vector, so no model is needed."""
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [0.8, 0.2]})

        class _Model:
            def encode(self, text):
                return np.asarray([1.0, 0.0], dtype=np.float32)

        # Not in the index, so its vector is generated rather than looked up -
        # which is the whole point, and which needs the model the base install
        # does not have.
        store.model = _Model()
        outsider = Node(id="outsider", type=NodeType.ACTOR, name="outsider")
        assert "outsider" not in store.embeddings

        results = store.search(query_node=outsider, limit=3, threshold=-1.0)
        assert [node_id for node_id, _ in results] == ["a", "b", "c"], (
            f"reached {len(results)} of 3 rows"
        )

    def test_a_generated_query_does_not_promote_the_index(self):
        """A float64 query against a float32 index makes numpy promote the
        MATRIX to compare them, which is an index-sized allocation per query -
        measured at 308 MB at 100k x 384.

        Reached through the branch that actually produces one: a query node
        absent from the index is embedded by `generate_embedding`, which ends
        in `.tolist()`, so the query arrives as a Python list and becomes
        float64. That matters for what this test can catch - planting a list
        into `embeddings` instead would exercise the LOOKUP branch, a state no
        production writer creates (both coerce to float32), and would pass
        against a cast applied only to that branch."""
        import tracemalloc

        store = self._store(4000, dim=256)
        row = np.asarray(store.embeddings["n0"])

        class _Model:
            def encode(self, text):
                return row

        store.model = _Model()
        outsider = Node(id="outsider", type=NodeType.ACTOR, name="outsider")
        assert "outsider" not in store.embeddings

        tracemalloc.start()
        store.search(query_node=outsider, limit=5)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < store.unit_matrix.nbytes / 4, (
            f"a generated query allocated {peak} bytes against a "
            f"{store.unit_matrix.nbytes}-byte index: the matrix was promoted"
        )

    def test_a_text_query_does_not_promote_the_index_either(self):
        """The cast's third branch. `test_a_generated_query_...` covers the
        generated one, but every text-query test in this class uses a 3-row,
        2-D index where a promoted matrix is 24 bytes - so dropping the cast on
        the text branch alone is invisible there. Sized here, with a stub whose
        `encode` returns float64: the shipped model returns float32, which is
        why this branch's cast reads as a no-op and needs a fixture that can
        tell the difference."""
        import tracemalloc

        store = self._store(4000, dim=256)
        wide = np.asarray(store.embeddings["n0"], dtype=np.float64)

        class _Model:
            def encode(self, text):
                return wide

        store.model = _Model()

        tracemalloc.start()
        store.search(query_text="anything", limit=5)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < store.unit_matrix.nbytes / 4, (
            f"a float64 text query allocated {peak} bytes against a "
            f"{store.unit_matrix.nbytes}-byte index: the matrix was promoted"
        )

    def test_the_walk_does_not_lengthen_with_the_index(self):
        """G2's time half, measured on the walk rather than on a proxy for it.

        Not unsteppable-around - `sys.settrace` is per-thread, so a walk moved
        onto a worker thread reports nothing here. That is contrived rather
        than plausible, and it is recorded rather than guarded.

        Two earlier versions of this test instrumented an object - `node_ids`,
        then the similarities array - and both were bypassable, because the
        production code chooses how it reads them. Counting `node_ids` put the
        instrument after the threshold check, so that exit could be deleted
        unseen. Counting the scores through `__getitem__`, `item` and
        `__iter__` was better but still evadable: reading them once through
        `np.asarray` hands back a base-class view, and the walk then visits
        every row while the counter reports 1 - inside any bound that is not
        zero.

        So count executed LINES instead. That is the walk itself rather than a
        proxy for it, and it is exactly flat at 2 000 rows and at 20 000 alike.
        An O(n) walk moves the number by thousands however it reads its arrays.

        Lines are counted in every frame belonging to this package, not only
        in `search` and not only in `vector_store`. Each narrower scoping left
        a door open and each was walked through: one code object missed a
        module-level helper (14.5 ms to 57.7 ms at 100k rows), and one module
        missed the same helper moved to a sibling file. Naming any boundary
        smaller than the package invites the next one.

        Both exits are exercised, because only one fires in any given call, and
        the threshold one is what fires in production - both callers in
        `storage_search.py` pass a floor."""
        package = VectorStore.__module__.split(".")[0] + "."

        def executed_lines(store, **kwargs):
            counted = [0]

            def local(frame, event, arg):
                if event == "line":
                    counted[0] += 1
                return local

            def top(frame, event, arg):
                if event != "call":
                    return None
                name = frame.f_globals.get("__name__") or ""
                return local if name.startswith(package) else None

            previous = sys.gettrace()
            sys.settrace(top)
            try:
                store.search(**kwargs)
            finally:
                sys.settrace(previous)
            return counted[0]

        def stubbed(rows):
            store = self._store(rows, dim=64, seed=3)
            vector = np.asarray(store.embeddings["n0"], dtype=np.float32)

            class _Model:
                def encode(self, text):
                    return vector

            store.model = _Model()
            return store

        small, large = stubbed(2000), stubbed(20000)

        # A floor nothing clears, a limit with no floor - and the shape
        # production actually asks for, which is neither. Both original shapes
        # sit at an extreme: at 0.99 almost nothing clears the floor, at -1.0
        # the floor is off. A refactor that pre-filters the candidates when
        # `threshold > 0` is O(n) in exactly the crack between them, and was
        # invisible to every instrument here - the allocation budgets all run
        # at the default threshold of 0.0 too.
        for threshold, limit, exit_name in (
            (0.99, 10, "threshold"),
            (-1.0, 10, "limit"),
            (0.3, 200, "production floor"),
        ):
            near = executed_lines(
                small, query_text="x", limit=limit, threshold=threshold
            )
            far = executed_lines(
                large, query_text="x", limit=limit, threshold=threshold
            )
            assert near == far, (
                f"the {exit_name} exit walked {near} lines over 2000 rows and "
                f"{far} over 20000: the walk grows with the index instead of "
                f"stopping once it is done"
            )
            # Proportional to what was asked for, not a flat number: the
            # walk is O(limit), so 10 rows and 200 rows cannot share a bound.
            ceiling = 50 + 10 * limit
            assert far < ceiling, (
                f"the {exit_name} exit walked {far} lines to return at most "
                f"{limit} rows, over the {ceiling} this shape allows: it is "
                f"not stopping early at all"
            )

    def test_the_ranking_holds_at_the_shape_production_asks_for(self):
        """The full-ranking assertions elsewhere pass `threshold=-2.0` and
        `limit=len(node_ids)`. Production asks for neither.

        The shape here is the over-fetching caller's:
        `search_graph(limit=50)` reaches `semantic_search_nodes` without a
        threshold, so it takes `DEFAULT_SEMANTIC_THRESHOLD` of 0.3, and that
        function asks the index for `max(limit*4, limit)` - 200 rows above a
        0.3 floor. (The 0.4 floor elsewhere in `storage_search.py` belongs to
        `find_similar_nodes`, which does not over-fetch; an earlier version of
        this docstring built one shape out of both.)

        That gap is not theoretical. A tail reorder gated on `threshold > 0`
        is invisible to every other order assertion here, and at this shape it
        puts 150 of the 200 returned rows in the wrong place, starting at rank
        51. The floor does not bite on this fixture - the lowest cosine is
        0.666 - so the 200 is decided by the limit, which is the point: it is
        the limit-and-tail combination that production produces."""
        eps = float(np.finfo(np.float32).eps)
        # Seed 55 on purpose: it is one of the four in 200 where the ordered
        # comparison this test used to make actually fails, so reverting to
        # that form breaks here immediately rather than at 2% per seed.
        store = self._store(2000, dim=128, seed=55)
        vector = np.asarray(store.embeddings["n0"], dtype=np.float32)

        class _Model:
            def encode(self, text):
                return vector

        store.model = _Model()
        returned = store.search(query_text="anything", limit=200, threshold=0.3)
        assert len(returned) == 200, (
            f"the fixture returned {len(returned)} rows, so this is no longer "
            f"the production shape it claims to test"
        )

        query = np.asarray(vector, dtype=np.float64).reshape(1, -1)
        query = query / (np.linalg.norm(query, axis=1, keepdims=True) + 1e-12)
        rows = np.vstack(
            [np.asarray(store.embeddings[i], dtype=np.float64) for i in store.node_ids]
        )
        rows = rows / (np.linalg.norm(rows, axis=1, keepdims=True) + 1e-12)
        reference = {
            node_id: float((query @ rows.T)[0][i])
            for i, node_id in enumerate(store.node_ids)
        }
        for rank, ((first, first_score), (second, second_score)) in enumerate(
            zip(returned, returned[1:])
        ):
            if reference[first] < reference[second]:
                gap = reference[second] - reference[first]
                assert first_score == second_score or gap < 4 * eps, (
                    f"at rank {rank} of {len(returned)}: {second} outranks "
                    f"{first} in float64 by {gap}, yet came back after it"
                )

        # WHICH rows come back, not only in what order. Pairwise order says
        # nothing about membership: dropping the single best-scoring row leaves
        # every remaining pair correctly ordered, and passed this test until
        # this assertion existed.
        #
        # Compared as a SET, deliberately. The first version of this compared
        # the ordered lists, which asserts the float64 order exactly - the very
        # property the loop above permits violations of, and which the test
        # named in that loop's docstring documents as untrue. It fails at 4
        # seeds in 200 on correct code - 55, 67, 102 and 155 - and this test
        # deliberately runs at the first of them, so the ordered form cannot
        # come back unnoticed. Order is the loop's job; membership is this
        # line's.
        # Compared against the cutoff rather than as an exact set, for the
        # same reason the loop above has a tolerance: at the 200th place two
        # rows can sit closer together than float32 can resolve, and then which
        # of them lands inside is not a property of correct code. Measured over
        # 1200 seeds, the exact set comparison fails at one (700, a 0.29-eps
        # swap at ranks 199/200) and this one at none. So: everything clearly
        # above the cutoff must be there, and nothing clearly below it may be.
        best = sorted(reference, key=lambda node_id: -reference[node_id])[:200]
        cutoff = reference[best[-1]]
        returned_ids = set(node_id for node_id, _ in returned)

        clearly_inside = {n for n in best if reference[n] - cutoff > 4 * eps}
        assert clearly_inside <= returned_ids, (
            f"{len(clearly_inside - returned_ids)} rows that outscore the "
            f"cutoff by more than float32 can round away are missing from the "
            f"result - rows are being dropped, which no pairwise check sees"
        )
        for node_id in returned_ids:
            assert reference[node_id] >= cutoff - 4 * eps, (
                f"{node_id} came back despite scoring {reference[node_id]} "
                f"against a cutoff of {cutoff} - further below it than float32 "
                f"can account for, so a row is being admitted that should not"
            )

    def test_equal_scores_keep_index_order_at_scale(self):
        """`test_equal_scores_keep_index_order` builds 51 rows, so an unstable
        sort switched on above a size gate passes it. That is not a contrived
        mutant: exact ties arise whenever two nodes carry identical text, and
        on this 1000-row index `kind="quicksort"` first diverges from stable at
        rank 6 and moves 269 rows - 27% of the index, which is what 2 rows in 7
        being duplicates buys. (Measured on the fixture below. The figures here
        previously described the pairs fixture this one replaced, in the same
        commit that replaced it.)

        Same invariant as the small test, at a size no plausible gate sits
        above, and in groups of three rather than pairs - see the fixture."""
        rng = np.random.default_rng(31)
        vectors = {}
        for i in range(1000):
            # Groups of THREE, not pairs. An earlier version repeated each row
            # once, and a reordering that keeps a group's first member and
            # reverses the rest is a no-op on a pair - it only bites at three
            # or more, which is what two nodes sharing text with a third gives.
            if i % 7 in (1, 2) and i > 1:
                vectors[f"n{i}"] = np.array(vectors[f"n{i - 1}"], dtype=np.float32)
            else:
                vectors[f"n{i}"] = rng.random(48).astype(np.float32)
        store = VectorStore()
        store.load_vectors(vectors)

        vector = np.asarray(store.embeddings["n0"], dtype=np.float32)

        class _Model:
            def encode(self, text):
                return vector

        store.model = _Model()
        returned = store.search(
            query_text="anything", limit=len(store.node_ids), threshold=-2.0
        )

        position = {node_id: i for i, node_id in enumerate(store.node_ids)}
        for (first, first_score), (second, second_score) in zip(returned, returned[1:]):
            if first_score == second_score:
                assert position[first] < position[second], (
                    f"{first} and {second} score identically but came back in "
                    f"index positions {position[first]} and {position[second]} "
                    f"- the sort is not stable"
                )

    def test_the_ranking_holds_on_an_index_larger_than_the_other_fixtures(self):
        """Index size is the one dimension the order assertions do not vary:
        every one of them runs at 2 000 rows or fewer, so a selection fast path
        gated one row above that is invisible. `np.argpartition` for the top
        `limit+1` followed by a stable sort of that slice is the obvious such
        path, and it is FASTER, so no timing or allocation guard objects.

        What it costs shows up only where ties straddle the limit: with a block
        of identical vectors it does not merely reorder, it returns a different
        SET - about half of the 200 nodes the stable form returns are absent
        (95 measured here, and the exact count depends on numpy's partition
        rather than on anything this pins). So this
        asserts the set as well as the order, on a fixture built to straddle:
        a 400-row block of one repeated vector, at limits inside that block.

        And at more than one limit and floor, because index size is not the
        only gate available - see the comment on the loop below.
        """
        rng = np.random.default_rng(77)
        vectors = {f"n{i}": rng.random(96).astype(np.float32) for i in range(10000)}
        shared = rng.random(96).astype(np.float32)
        for i in range(3000, 3400):
            vectors[f"n{i}"] = np.array(shared, dtype=np.float32)
        store = VectorStore()
        store.load_vectors(vectors)

        vector = np.array(shared, dtype=np.float32)

        class _Model:
            def encode(self, text):
                return vector

        store.model = _Model()

        # Three shapes, because size is not the only gate a fast path can hide
        # behind. 200 above a 0.3 floor is what `search_graph(limit=50)`
        # produces through `semantic_search_nodes`; 204 is the same caller one
        # limit higher, and a selection gated on `limit > 200` sat exactly in
        # that crack; 25 above a 0.4 floor is `find_similar_nodes`, whose
        # threshold is `max(0.4, threshold - 0.2)` and so never below 0.4 -
        # nothing else in the suite asserts membership at that floor on an
        # index this size, and a mutation dropping the single best-scoring row
        # there went unseen.
        for limit, threshold in ((200, 0.3), (204, 0.4), (25, 0.4)):
            returned = store.search(
                query_text="anything", limit=limit, threshold=threshold
            )
            assert len(returned) == limit

            # The block scores 1.0, so the stable answer is its first `limit`
            # members in index order - and a partial selection is free to
            # return any `limit` of the 400, which is the difference this pins.
            expected = [f"n{i}" for i in range(3000, 3000 + limit)]
            assert [node_id for node_id, _ in returned] == expected, (
                f"at limit={limit}, threshold={threshold}: the ranking over a "
                f"tie block that straddles the limit is not the stable top-k - "
                f"either the order or the set of rows changed"
            )

    def test_the_rows_are_unit_length_to_float32_resolution(self):
        """G4 stated directly, at the precision the rows are actually stored
        at. The suite's other row assertions compare DIRECTIONS with atol=1e-6,
        which a systematic scale error of ~1e-6 slips through. Such an
        error scales every score, so what it costs is the largest cosine in the
        index: 0.849 on this fixture, hence 8.5e-7 - four and a half times the
        1.9e-7 the cast's comment bounds the width at. (Twice corrected: the
        figure was first quoted from a different fixture, then from row n0's
        largest cosine rather than the whole index's.) Nothing else in the
        suite measures the norms."""
        store = self._store(1500, dim=192, seed=5)
        norms = np.linalg.norm(store.unit_matrix, axis=1)
        worst = float(np.abs(norms - 1.0).max())
        assert worst < 4 * float(np.finfo(np.float32).eps), (
            f"rows are off unit length by {worst}, more than float32 rounding "
            f"accounts for: the normalisation is systematically wrong"
        )

    def test_the_narrower_arithmetic_stays_inside_float32_resolution(self):
        """What scoring at the index's width costs, stated as the property that
        holds rather than as one lucky draw.

        An earlier version of this test asserted that the top of the ranking is
        IDENTICAL to the float64-promoted order. That is not true and the test
        only passed because seed 17 happens to leave a 1.9x margin: at seed 67
        the same construction moves a row at rank 7. Rows float32 cannot
        separate score exactly equal, the stable sort then returns them in
        index order, and that pair can sit anywhere - including the top.

        So pin what the width actually bounds - the SCORE - and allow the
        order to differ exactly where float32 has no standing to decide: a pair
        it scored equal, or one whose float64 separation is smaller than the
        float32 error itself. The second half matters and an earlier version of
        this test got it wrong, asserting that only exact ties may reorder; at
        seed 155 float32 strictly inverts a pair it separated, because its own
        error (1.0 eps here) is the larger quantity. Measured over 400 seeds, the
        widest float64 gap across such an inversion is 0.20 eps, so the 4-eps
        allowance below has roughly 20x margin.

        Swept over seeds rather than fixed to one, because a single seed is
        what hid the original defect: it held at 17 and failed at 155. But the
        sweep is not what makes this sound - under the bound everything passes
        at every seed, over it it fails. The bound is the test."""
        eps = float(np.finfo(np.float32).eps)

        for seed in (17, 67, 3, 128, 155):
            store = self._store(2000, dim=128, seed=seed)
            row = np.asarray(store.embeddings["n0"])

            class _Model:
                def encode(self, text):
                    return row

            store.model = _Model()
            outsider = Node(id="outsider", type=NodeType.ACTOR, name="outsider")
            shipped = store.search(query_node=outsider, limit=50, threshold=-1.0)
            assert len(shipped) == 50

            # The reference is built from `embeddings`, NOT by promoting
            # `unit_matrix` back to float64: reading the shipped matrix back
            # would move the reference with any error in how the rows were
            # normalised, so a row-side mistake would cancel itself out here.
            # Stacking the dict instead makes this an independent ranking.
            query = np.asarray(row.tolist()).reshape(1, -1)
            query = query / (np.linalg.norm(query, axis=1, keepdims=True) + 1e-12)
            rows = np.vstack(
                [
                    np.asarray(store.embeddings[i], dtype=np.float64)
                    for i in store.node_ids
                ]
            )
            rows = rows / (np.linalg.norm(rows, axis=1, keepdims=True) + 1e-12)
            promoted = (query @ rows.T)[0]
            reference = {
                node_id: float(promoted[i]) for i, node_id in enumerate(store.node_ids)
            }

            for node_id, score in shipped:
                assert abs(score - reference[node_id]) < 4 * eps, (
                    f"seed {seed}: {node_id} scored {score} against a promoted "
                    f"{reference[node_id]} - further apart than float32 can "
                    f"account for"
                )

            for (first, first_score), (second, second_score) in zip(
                shipped, shipped[1:]
            ):
                if reference[first] < reference[second]:
                    gap = reference[second] - reference[first]
                    assert first_score == second_score or gap < 4 * eps, (
                        f"seed {seed}: {second} outranks {first} in float64 by "
                        f"{gap} - wider than float32 rounding can account for - "
                        f"yet came back after it, and float32 separated them "
                        f"({first_score} vs {second_score}). That is a real "
                        f"reordering, not the width running out of resolution"
                    )
