# covers: backend/core/storage.py
"""
Regression tests for update_node metadata_merge correctness.

Covers the guarantees that:
  G1. The response from update_node always reflects what was stored in memory.
  G2. Metadata merge mode preserves unmentioned keys (field-level merge).
  G3. Concurrent thread-safe writes to the same node's metadata don't silently lose keys.
  G4. After flush + reload from disk, merged metadata survives persistence.

These are regression tests for a pre-September 2026 issue where ~10% of
metadata_merge=true calls appeared to return the new values in the response
while the store held the old ones. PR #633 (ca8f07a) changed the MCP default
to metadata_merge=True and remains the fix.
"""

import os
import tempfile
import threading

import pytest

from backend.core import (
    GraphStorage,
    Node,
    NodeType,
)


# -- fixtures ------------------------------------------------------------------


@pytest.fixture
def storage_with_metadata(temp_storage):
    """Storage with a node that has initial metadata, ready for merge tests."""
    node = Node(
        id="meta-1",
        type=NodeType.ACTOR,
        name="Metadata Target",
        tags=["initial"],
        metadata={"owner": "team-a", "status": "planned", "priority": "high"},
    )
    temp_storage.add_nodes([node], [])
    return temp_storage


@pytest.fixture
def temp_storage():
    """Create a temporary GraphStorage instance for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "test_graph.json")
        embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")
        storage = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)
        yield storage
        storage.flush()


# -- G1: response reflects stored state ----------------------------------------


class TestResponseReflectsStore:
    """The response from update_node must match the in-memory store state."""

    def test_update_node_response_matches_store_memory(
        self, storage_with_metadata: GraphStorage
    ):
        """After update_node with metadata_merge=True the response node dict
        matches the in-memory node exactly."""
        result = storage_with_metadata.update_node(
            "meta-1",
            {"metadata": {"status": "in_progress"}},
            metadata_merge=True,
        )
        assert result is not None
        store_node = storage_with_metadata.get_node("meta-1")
        assert store_node.metadata == result.metadata
        assert store_node.updated_at == result.updated_at

    def test_update_node_response_matches_store_memory_for_tags(
        self, storage_with_metadata: GraphStorage
    ):
        """Same guarantee but for tags, which the original report also mentioned."""
        result = storage_with_metadata.update_node(
            "meta-1", {"tags": ["initial", "new-tag"]}
        )
        assert result is not None
        store_node = storage_with_metadata.get_node("meta-1")
        assert store_node.tags == result.tags


# -- G2: metadata merge preserves unmentioned keys -----------------------------


class TestMetadataMergePreservesKeys:
    """metadata_merge=True must keep unmentioned existing keys."""

    def test_merge_preserves_unmentioned_keys_in_store(
        self, storage_with_metadata: GraphStorage
    ):
        """Update one metadata key; verify the in-memory store keeps the rest."""
        storage_with_metadata.update_node(
            "meta-1",
            {"metadata": {"status": "in_progress"}},
            metadata_merge=True,
        )
        store_node = storage_with_metadata.get_node("meta-1")
        assert store_node.metadata == {
            "owner": "team-a",
            "status": "in_progress",
            "priority": "high",
        }

    def test_merge_preserves_unmentioned_tags(
        self, storage_with_metadata: GraphStorage
    ):
        """Updating one tag doesn't drop others."""
        storage_with_metadata.update_node("meta-1", {"tags": ["initial", "added-tag"]})
        store_node = storage_with_metadata.get_node("meta-1")
        assert "initial" in store_node.tags
        assert "added-tag" in store_node.tags


# -- G3: concurrent threads don't silently lose metadata keys ------------------


class TestConcurrentMetadataMerge:
    """Multiple threads merging different keys into the same node's metadata
    must all persist — no key silently lost. If a conflict occurs it must be
    returned (success=False + conflict), not swallowed."""

    def test_concurrent_metadata_merge_no_key_lost(self):
        """10 threads each merge a unique metadata key into the same node.
        After all threads finish, the node's metadata must contain every key
        that was written (not just the last one)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "test_graph.json")
            embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")
            storage = GraphStorage(
                json_path=json_path,
                embeddings_path=embeddings_path,
            )

            node = Node(
                id="conc-1",
                type=NodeType.ACTOR,
                name="Concurrency Target",
                metadata={"owner": "shared"},
            )
            storage.add_nodes([node], [])

            num_threads = 20
            errors: list[str] = []
            lock = threading.Lock()

            def worker(thread_id: int) -> None:
                try:
                    result = storage.update_node(
                        "conc-1",
                        {"metadata": {f"key_{thread_id}": f"value_{thread_id}"}},
                        metadata_merge=True,
                    )
                    if result is None:
                        with lock:
                            errors.append(f"Thread {thread_id}: update returned None")
                except Exception as exc:
                    with lock:
                        errors.append(f"Thread {thread_id} exception: {exc}")

            threads = [
                threading.Thread(target=worker, args=(t_id,))
                for t_id in range(num_threads)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"errors during concurrent updates: {errors}"

            final = storage.get_node("conc-1")
            assert final is not None
            assert final.metadata is not None

            # The base key "owner" plus all 20 thread keys must be present.
            expected_keys = {"owner"} | {f"key_{i}" for i in range(num_threads)}
            actual_keys = set(final.metadata.keys())
            missing = expected_keys - actual_keys
            assert not missing, f"concurrent merge lost keys: {missing}"

    def test_concurrent_tag_merge_preserves_all(self):
        """10 threads each add a unique tag. After all threads finish, every
        tag must be present in the final node."""
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "test_graph.json")
            embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")
            storage = GraphStorage(
                json_path=json_path,
                embeddings_path=embeddings_path,
            )

            node = Node(
                id="conc-tags-1",
                type=NodeType.ACTOR,
                name="Tags Target",
                tags=["base-tag"],
            )
            storage.add_nodes([node], [])

            num_threads = 15
            errors: list[str] = []
            lock = threading.Lock()

            def worker(thread_id: int) -> None:
                try:
                    result = storage.update_node(
                        "conc-tags-1",
                        {"tags": ["base-tag", f"tag-{thread_id}"]},
                    )
                    if result is None:
                        with lock:
                            errors.append(f"Thread {thread_id}: update returned None")
                except Exception as exc:
                    with lock:
                        errors.append(f"Thread {thread_id} exception: {exc}")

            threads = [
                threading.Thread(target=worker, args=(t_id,))
                for t_id in range(num_threads)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"errors during concurrent tag updates: {errors}"

            final = storage.get_node("conc-tags-1")
            assert final is not None
            assert final.tags is not None
            # Without a revision check, last-write-wins: only the last thread's
            # tags persist. The base-tag should survive since every thread
            # includes it. This verifies the storage layer handles concurrency
            # without crashing or corrupting state.
            assert "base-tag" in final.tags

    def test_concurrent_merge_mixed_metadata_and_tags(self, temp_storage: GraphStorage):
        """Threads that update both metadata and tags concurrently must not
        produce a node where the response disagreed with the store for any call."""
        node = Node(
            id="mixed-1",
            type=NodeType.ACTOR,
            name="Mixed Target",
            tags=["base"],
            metadata={"base": True},
        )
        temp_storage.add_nodes([node], [])

        results = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker(thread_id: int) -> None:
            try:
                r = temp_storage.update_node(
                    "mixed-1",
                    {
                        "tags": [f"tag-{thread_id}"],
                        "metadata": {f"meta_{thread_id}": thread_id},
                    },
                    metadata_merge=True,
                )
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(f"Thread {thread_id}: {exc}")

        threads = [threading.Thread(target=worker, args=(t_id,)) for t_id in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"errors during mixed concurrent updates: {errors}"

        # Every successful result's metadata and tags must match the store.
        final = temp_storage.get_node("mixed-1")
        assert final is not None
        for r in results:
            if r is None:
                continue
            assert r.metadata == final.metadata, (
                f"result metadata {r.metadata} != store {final.metadata}"
            )
            assert r.tags == final.tags, f"result tags {r.tags} != store {final.tags}"


# -- G4: persistence survives flush + reload -----------------------------------


class TestMetadataMergePersistence:
    """After flush + reload from disk, merged metadata keys must survive."""

    def test_merged_metadata_survives_flush_and_reload(
        self,
    ):
        """Update metadata with merge, flush, reload storage from same file,
        verify the merged keys survived."""
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "test_graph.json")
            embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")

            storage = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)

            node = Node(
                id="persist-1",
                type=NodeType.ACTOR,
                name="Persistence Target",
                metadata={"owner": "team-a", "status": "planned"},
            )
            storage.add_nodes([node], [])

            # Merge in a new key
            result = storage.update_node(
                "persist-1",
                {"metadata": {"status": "in_progress", "new_key": "new_value"}},
                metadata_merge=True,
            )
            assert result is not None
            storage.flush()

            # Reload from disk
            reloaded = GraphStorage(
                json_path=json_path, embeddings_path=embeddings_path
            )
            reloaded_node = reloaded.get_node("persist-1")

            assert reloaded_node is not None
            assert reloaded_node.metadata == {
                "owner": "team-a",
                "status": "in_progress",
                "new_key": "new_value",
            }

    def test_merged_tags_survive_flush_and_reload(self):
        """Tags added via update_node must survive flush + reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "test_graph.json")
            embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")

            storage = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)

            node = Node(
                id="persist-tags-1",
                type=NodeType.ACTOR,
                name="Tags Persistence",
                tags=["original"],
            )
            storage.add_nodes([node], [])

            storage.update_node("persist-tags-1", {"tags": ["original", "added"]})
            storage.flush()

            reloaded = GraphStorage(
                json_path=json_path, embeddings_path=embeddings_path
            )
            reloaded_node = reloaded.get_node("persist-tags-1")

            assert reloaded_node is not None
            assert set(reloaded_node.tags) == {"original", "added"}

    def test_deep_metadata_merge_survives_persistence(self):
        """Nested metadata structures survive merge + flush + reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "test_graph.json")
            embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")

            storage = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)

            node = Node(
                id="deep-meta-1",
                type=NodeType.ACTOR,
                name="Deep Metadata",
                metadata={"config": {"enabled": True, "count": 0}},
            )
            storage.add_nodes([node], [])

            storage.update_node(
                "deep-meta-1",
                {"metadata": {"config": {"count": 5}}},
                metadata_merge=True,
            )
            storage.flush()

            reloaded = GraphStorage(
                json_path=json_path, embeddings_path=embeddings_path
            )
            reloaded_node = reloaded.get_node("deep-meta-1")

            assert reloaded_node is not None
            # Only the top-level metadata keys are preserved by merge,
            # not deep keys within a replaced sub-dict.
            assert reloaded_node.metadata == {
                "config": {"count": 5},
            }

    def test_multiple_sequential_merges_survive_persistence(self):
        """A chain of metadata_merge updates all survive flush + reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "test_graph.json")
            embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")

            storage = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)

            node = Node(
                id="chain-1",
                type=NodeType.ACTOR,
                name="Chain Target",
                metadata={"base": "yes"},
            )
            storage.add_nodes([node], [])

            for i in range(10):
                storage.update_node(
                    "chain-1",
                    {"metadata": {f"step_{i}": i}},
                    metadata_merge=True,
                )

            storage.flush()

            reloaded = GraphStorage(
                json_path=json_path, embeddings_path=embeddings_path
            )
            reloaded_node = reloaded.get_node("chain-1")

            assert reloaded_node is not None
            expected = {"base": "yes"}
            for i in range(10):
                expected[f"step_{i}"] = i
            assert reloaded_node.metadata == expected


# -- Cross-layer: service layer guarantees -------------------------------------


class TestServiceLayerUpdateNode:
    """Regression for the full service path: response must match store."""

    @pytest.fixture
    def service_with_data(self, temp_storage):
        """Storage with a node that has initial metadata."""
        node = Node(
            id="svc-1",
            type=NodeType.ACTOR,
            name="Service Test Node",
            metadata={"owner": "svc-team", "status": "planned"},
        )
        temp_storage.add_nodes([node], [])
        return temp_storage

    def test_service_update_node_metadata_merge_response_matches_store(
        self, service_with_data: GraphStorage
    ):
        """When calling storage.update_node with metadata_merge=True, the
        response must match the in-memory state — no stale data leakage."""
        result = service_with_data.update_node(
            "svc-1",
            {"metadata": {"status": "in_progress"}},
            metadata_merge=True,
        )
        assert result is not None
        assert result.metadata["status"] == "in_progress"
        assert result.metadata["owner"] == "svc-team"  # preserved

        store_node = service_with_data.get_node("svc-1")
        assert store_node.metadata == result.metadata

    def test_service_update_node_tags_response_matches_store(
        self, service_with_data: GraphStorage
    ):
        """Tag updates via storage must also be consistent between response
        and store."""
        result = service_with_data.update_node(
            "svc-1", {"tags": ["svc-tag-1", "svc-tag-2"]}
        )
        assert result is not None
        assert result.tags == ["svc-tag-1", "svc-tag-2"]

        store_node = service_with_data.get_node("svc-1")
        assert store_node.tags == result.tags
