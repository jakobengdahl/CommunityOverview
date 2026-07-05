"""
Tests for the server-side session store (step 1) and op state transforms
(step 3) in ``backend/core/session_store.py``.

Covers: persistence roundtrip + atomic file writes, CRUD, id allocation,
per-op state transforms (union/removal/LWW), annotation id assignment and the
dropped-update rule, and the ring buffer catch-up continuity check.
"""

import json
from pathlib import Path

import pytest

from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    OpError,
    Session,
    SessionStore,
    is_valid_session_id,
    normalize_state,
)


def _store(tmp_path) -> SessionStore:
    return SessionStore(FileSessionPersistenceBackend(tmp_path / "sessions"))


class TestSessionIdValidation:
    def test_valid_and_invalid(self):
        assert is_valid_session_id("1234-5678")
        assert not is_valid_session_id("12-34")
        assert not is_valid_session_id("abcd-1234")
        assert not is_valid_session_id("../etc")
        assert not is_valid_session_id(None)


class TestPersistence:
    def test_create_writes_file(self, tmp_path):
        store = _store(tmp_path)
        session = store.create("hello")
        path = tmp_path / "sessions" / f"{session.id}.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["name"] == "hello"
        assert data["state"]["node_refs"] == []

    def test_load_through_from_disk(self, tmp_path):
        store = _store(tmp_path)
        session = store.create()
        # Fresh store over the same directory must load the persisted session.
        store2 = _store(tmp_path)
        loaded = store2.get(session.id)
        assert loaded is not None
        assert loaded.id == session.id

    def test_roundtrip_preserves_state(self, tmp_path):
        backend = FileSessionPersistenceBackend(tmp_path)
        session = Session(id="1111-2222", name="x", seq=5)
        session.state["node_refs"] = ["a", "b"]
        backend.save(session.to_dict())
        restored = Session.from_dict(backend.load("1111-2222"))
        assert restored.seq == 5
        assert restored.state["node_refs"] == ["a", "b"]

    def test_delete_removes_file(self, tmp_path):
        store = _store(tmp_path)
        session = store.create()
        assert store.delete(session.id) is True
        assert not (tmp_path / "sessions" / f"{session.id}.json").exists()
        assert store.delete(session.id) is False

    def test_list_meta_sorted(self, tmp_path):
        store = _store(tmp_path)
        a = store.create("a")
        b = store.create("b")
        metas = store.list_meta()
        ids = {m["id"] for m in metas}
        assert {a.id, b.id} <= ids
        assert all("state" not in m for m in metas)


class TestGetOrCreate:
    def test_creates_with_given_id(self, tmp_path):
        store = _store(tmp_path)
        session, created = store.get_or_create("4321-8765")
        assert created is True
        assert session.id == "4321-8765"
        again, created2 = store.get_or_create("4321-8765")
        assert created2 is False
        assert again.id == session.id

    def test_rejects_bad_id(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(OpError):
            store.get_or_create("nope")


class TestStateOps:
    def _apply(self, store, session, op):
        return store.apply_state_op(session, op)

    def test_nodes_added_union_idempotent(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(store, s, {"op": "nodes_added", "node_ids": ["a", "b"]})
        self._apply(store, s, {"op": "nodes_added", "node_ids": ["b", "c"]})
        assert s.state["node_refs"] == ["a", "b", "c"]
        assert s.seq == 2

    def test_nodes_removed_cleans_positions_and_membership(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(store, s, {"op": "nodes_added", "node_ids": ["a", "b"]})
        self._apply(store, s, {"op": "node_moved", "node_id": "a", "position": {"x": 1, "y": 2}})
        self._apply(store, s, {"op": "annotation_created", "annotation": {"kind": "group", "member_node_ids": ["a", "b"]}})
        self._apply(store, s, {"op": "nodes_removed", "node_ids": ["a"]})
        assert s.state["node_refs"] == ["b"]
        assert "a" not in s.state["positions"]
        assert s.state["annotations"][0]["member_node_ids"] == ["b"]

    def test_node_moved_is_lww(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(store, s, {"op": "node_moved", "node_id": "a", "position": {"x": 1, "y": 1}})
        self._apply(store, s, {"op": "node_moved", "node_id": "a", "position": {"x": 9, "y": 9}})
        assert s.state["positions"]["a"] == {"x": 9.0, "y": 9.0}

    def test_node_moved_rejects_bad_position(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        with pytest.raises(OpError):
            self._apply(store, s, {"op": "node_moved", "node_id": "a", "position": {"x": "no"}})

    def test_hide_show_sets(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(store, s, {"op": "nodes_hidden", "node_ids": ["a", "b"]})
        self._apply(store, s, {"op": "nodes_shown", "node_ids": ["a"]})
        assert s.state["hidden_node_ids"] == ["b"]
        self._apply(store, s, {"op": "edges_hidden", "edge_ids": ["e1"]})
        assert s.state["hidden_edge_ids"] == ["e1"]

    def test_annotation_created_assigns_id(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        applied = self._apply(store, s, {"op": "annotation_created", "annotation": {"kind": "note", "text": "hi"}})
        assert isinstance(applied["annotation"]["id"], str)
        assert s.state["annotations"][0]["text"] == "hi"

    def test_annotation_update_on_deleted_is_dropped(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        applied = self._apply(store, s, {"op": "annotation_created", "annotation": {"kind": "note"}})
        ann_id = applied["annotation"]["id"]
        self._apply(store, s, {"op": "annotation_deleted", "annotation_id": ann_id})
        seq_before = s.seq
        result = self._apply(store, s, {"op": "annotation_updated", "annotation": {"id": ann_id, "kind": "note", "text": "x"}})
        assert result is None
        assert s.seq == seq_before  # dropped op must not advance seq

    def test_group_membership_changed_requires_group(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        with pytest.raises(OpError):
            self._apply(store, s, {"op": "group_membership_changed", "group_id": "missing", "member_node_ids": []})

    def test_layout_applied_batch(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(store, s, {"op": "layout_applied", "positions": {"a": {"x": 1, "y": 2}, "b": {"x": 3, "y": 4}}})
        assert s.state["positions"] == {"a": {"x": 1.0, "y": 2.0}, "b": {"x": 3.0, "y": 4.0}}

    def test_annotation_limit_enforced(self, tmp_path):
        store = SessionStore(InMemorySessionPersistenceBackend(), max_annotations=2)
        s = store.create()
        self._apply(store, s, {"op": "annotation_created", "annotation": {"kind": "note"}})
        self._apply(store, s, {"op": "annotation_created", "annotation": {"kind": "note"}})
        with pytest.raises(OpError):
            self._apply(store, s, {"op": "annotation_created", "annotation": {"kind": "note"}})

    def test_unknown_op_raises(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        with pytest.raises(OpError):
            self._apply(store, s, {"op": "frobnicate"})


class TestRingBufferCatchUp:
    def test_ops_since_returns_missed(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        for i in range(3):
            store.apply_state_op(s, {"op": "nodes_added", "node_ids": [f"n{i}"]})
        missed = store.ops_since(s.id, 1)
        assert [op["seq"] for op in missed] == [2, 3]

    def test_ops_since_up_to_date_is_empty(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        store.apply_state_op(s, {"op": "nodes_added", "node_ids": ["a"]})
        assert store.ops_since(s.id, s.seq) == []

    def test_ops_since_gap_returns_none(self, tmp_path):
        store = SessionStore(InMemorySessionPersistenceBackend(), ring_size=2)
        s = store.create()
        for i in range(5):
            store.apply_state_op(s, {"op": "nodes_added", "node_ids": [f"n{i}"]})
        # ring holds only the last 2 ops (seq 4,5); asking from seq 1 cannot be served.
        assert store.ops_since(s.id, 1) is None
        # but a recent enough since_seq is served from the ring
        assert [op["seq"] for op in store.ops_since(s.id, 4)] == [5]


class TestNormalizeState:
    def test_drops_unknown_keys_and_dedupes(self):
        out = normalize_state(
            {
                "node_refs": ["a", "b", "a"],
                "hidden_node_ids": ["h", "h"],
                "bogus": 123,
            },
            max_annotations=100,
        )
        assert out["node_refs"] == ["a", "b"]
        assert out["hidden_node_ids"] == ["h"]
        assert "bogus" not in out
        # missing keys default to the empty-state shape
        assert out["positions"] == {}
        assert out["annotations"] == []
        assert out["manual_edges"] == []

    def test_validates_positions(self):
        out = normalize_state({"positions": {"a": {"x": 1, "y": 2}}}, max_annotations=100)
        assert out["positions"] == {"a": {"x": 1.0, "y": 2.0}}
        with pytest.raises(OpError):
            normalize_state({"positions": {"a": {"x": "no"}}}, max_annotations=100)

    def test_rejects_non_string_node_refs(self):
        with pytest.raises(OpError):
            normalize_state({"node_refs": ["a", 3]}, max_annotations=100)

    def test_assigns_annotation_ids_and_enforces_limit(self):
        out = normalize_state(
            {"annotations": [{"kind": "group", "label": "G", "member_node_ids": ["a"]}]},
            max_annotations=100,
        )
        assert isinstance(out["annotations"][0]["id"], str)
        assert out["annotations"][0]["kind"] == "group"
        with pytest.raises(OpError):
            normalize_state(
                {"annotations": [{"kind": "note"}, {"kind": "note"}]},
                max_annotations=1,
            )

    def test_rejects_unknown_annotation_kind(self):
        with pytest.raises(OpError):
            normalize_state({"annotations": [{"kind": "sticker"}]}, max_annotations=100)


class TestReplaceState:
    def test_replaces_bumps_seq_and_persists(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        store.apply_state_op(s, {"op": "nodes_added", "node_ids": ["old"]})
        prev_seq = s.seq

        store.replace_state(s, {"node_refs": ["a", "b"], "positions": {"a": {"x": 1, "y": 1}}})

        assert s.state["node_refs"] == ["a", "b"]
        assert "old" not in s.state["node_refs"]
        assert s.seq == prev_seq + 1
        # survives a reload from disk
        reloaded = _store(tmp_path).get(s.id)
        assert reloaded.state["node_refs"] == ["a", "b"]

    def test_invalid_state_raises_before_mutation(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        store.apply_state_op(s, {"op": "nodes_added", "node_ids": ["keep"]})
        with pytest.raises(OpError):
            store.replace_state(s, {"node_refs": [1, 2]})
        assert s.state["node_refs"] == ["keep"]
