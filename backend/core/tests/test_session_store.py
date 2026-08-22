"""
Tests for the server-side session store (step 1) and op state transforms
(step 3) in ``backend/core/session_store.py``.

Covers: persistence roundtrip + atomic file writes, CRUD, id allocation,
per-op state transforms (union/removal/LWW), annotation id assignment and the
dropped-update rule, and the ring buffer catch-up continuity check.
"""

import json

import pytest

from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    OpError,
    Session,
    SessionStore,
    is_valid_session_id,
)


def _store(tmp_path) -> SessionStore:
    return SessionStore(FileSessionPersistenceBackend(tmp_path / "sessions"))


class TestSessionIdValidation:
    def test_valid_and_invalid(self):
        assert is_valid_session_id("1234-5678")  # legacy two-group form
        assert is_valid_session_id("1234-5678-9012-3456")  # new four-group form
        assert not is_valid_session_id("12-34")
        assert not is_valid_session_id("1234-5678-9012")  # three groups
        assert not is_valid_session_id("abcd-1234")
        assert not is_valid_session_id("../etc")
        assert not is_valid_session_id(None)

    def test_new_ids_use_four_group_form(self, tmp_path):
        store = _store(tmp_path)
        for _ in range(20):
            session = store.create()
            assert is_valid_session_id(session.id)
            assert len(session.id.split("-")) == 4


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

    def test_manual_edges_is_not_a_state_field(self, tmp_path):
        """R14: manual_edges was dead (no op wrote it; manual edges persist in
        the graph itself since PR #186) and has been removed from the model."""
        store = _store(tmp_path)
        session = store.create()
        assert "manual_edges" not in session.state

    def test_loading_a_pre_r14_file_with_manual_edges_is_tolerated(self, tmp_path):
        """A session file persisted before R14 may still carry the now-removed
        key; loading it must not fail, and the stale key is simply dropped."""
        backend = FileSessionPersistenceBackend(tmp_path)
        session = Session(id="1111-3333")
        raw = session.to_dict()
        raw["state"]["manual_edges"] = [{"id": "e1", "source": "a", "target": "b"}]
        backend.save(raw)
        restored = Session.from_dict(backend.load("1111-3333"))
        assert "manual_edges" not in restored.state

    def test_list_meta_sorted(self, tmp_path):
        store = _store(tmp_path)
        a = store.create("a")
        b = store.create("b")
        metas = store.list_meta()
        ids = {m["id"] for m in metas}
        assert {a.id, b.id} <= ids
        assert all("state" not in m for m in metas)

    def test_list_meta_cache_reflects_a_rename(self, tmp_path):
        """R13: list_meta caches the backend scan but must not serve a stale
        name after a rename invalidates it."""
        store = _store(tmp_path)
        a = store.create("a")
        store.list_meta()  # populate the cache
        store.rename(a.id, "renamed")
        names = {m["id"]: m["name"] for m in store.list_meta()}
        assert names[a.id] == "renamed"

    def test_session_count_counts_disk_files_not_just_in_memory(self, tmp_path):
        """R13: the cap must survive a restart (D13: no eviction, so session
        files outlive the process) — not reset to 0 with an empty in-memory map."""
        store = _store(tmp_path)
        store.create()
        store.create()
        restarted = _store(tmp_path)  # fresh in-memory map, same directory
        assert restarted.session_count() == 2

    def test_session_count_updates_on_create_and_delete(self, tmp_path):
        store = _store(tmp_path)
        a = store.create()
        store.create()
        assert store.session_count() == 2
        store.delete(a.id)
        assert store.session_count() == 1


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
        self._apply(
            store, s, {"op": "node_moved", "node_id": "a", "position": {"x": 1, "y": 2}}
        )
        self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {"kind": "group", "member_node_ids": ["a", "b"]},
            },
        )
        self._apply(store, s, {"op": "nodes_removed", "node_ids": ["a"]})
        assert s.state["node_refs"] == ["b"]
        assert "a" not in s.state["positions"]
        assert s.state["annotations"][0]["member_node_ids"] == ["b"]

    def test_node_moved_is_lww(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(
            store, s, {"op": "node_moved", "node_id": "a", "position": {"x": 1, "y": 1}}
        )
        self._apply(
            store, s, {"op": "node_moved", "node_id": "a", "position": {"x": 9, "y": 9}}
        )
        assert s.state["positions"]["a"] == {"x": 9.0, "y": 9.0}

    def test_node_moved_rejects_bad_position(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        with pytest.raises(OpError):
            self._apply(
                store, s, {"op": "node_moved", "node_id": "a", "position": {"x": "no"}}
            )

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
        applied = self._apply(
            store,
            s,
            {"op": "annotation_created", "annotation": {"kind": "note", "text": "hi"}},
        )
        assert isinstance(applied["annotation"]["id"], str)
        assert applied["annotation"]["type"] == "note"
        assert s.state["annotations"][0]["text"] == "hi"

    def test_annotation_created_accepts_v1_types_and_migrates_arrow_alias(
        self, tmp_path
    ):
        store = _store(tmp_path)
        s = store.create()
        line = self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {
                    "id": "line-1",
                    "type": "line",
                    "from": {"x": 0, "y": 0},
                    "to": {"x": 1, "y": 1},
                },
            },
        )
        arrow = self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {"id": "arrow-1", "kind": "arrow"},
            },
        )
        assert line["annotation"]["kind"] == "line"
        assert arrow["annotation"]["type"] == "line"
        assert [a["id"] for a in s.state["annotations"]] == ["line-1", "arrow-1"]

    def test_annotation_created_retry_with_same_id_upserts_not_duplicates(
        self, tmp_path
    ):
        store = _store(tmp_path)
        s = store.create()
        ann = {"id": "fixed-id", "kind": "note", "text": "hi"}
        self._apply(store, s, {"op": "annotation_created", "annotation": ann})
        # Simulate a client retry of the exact same batch (e.g. a lost POST
        # response) resending the identically-id'd create op.
        applied = self._apply(store, s, {"op": "annotation_created", "annotation": ann})
        assert len(s.state["annotations"]) == 1
        assert applied["annotation"]["id"] == "fixed-id"

    def test_annotation_created_retry_after_delete_does_not_resurrect(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        ann = {"id": "fixed-id", "kind": "note", "text": "hi"}
        self._apply(store, s, {"op": "annotation_created", "annotation": ann})
        self._apply(store, s, {"op": "annotation_deleted", "annotation_id": "fixed-id"})
        # A collaborator's create retry (lost response) for the now-deleted id
        # arrives after the delete — same "dropped" rule as an update arriving
        # after a delete, just below.
        result = self._apply(store, s, {"op": "annotation_created", "annotation": ann})
        assert result is None
        assert s.state["annotations"] == []

    def test_annotation_update_on_deleted_is_dropped(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        applied = self._apply(
            store, s, {"op": "annotation_created", "annotation": {"kind": "note"}}
        )
        ann_id = applied["annotation"]["id"]
        self._apply(store, s, {"op": "annotation_deleted", "annotation_id": ann_id})
        seq_before = s.seq
        result = self._apply(
            store,
            s,
            {
                "op": "annotation_updated",
                "annotation": {"id": ann_id, "kind": "note", "text": "x"},
            },
        )
        assert result is None
        assert s.seq == seq_before  # dropped op must not advance seq

    def test_annotation_created_upsert_rejects_type_change(self, tmp_path):
        """A create-op that upserts by id (existing id, new content) must not
        be able to retype the annotation — that must go through delete +
        create, which is an explicit, visible two-step action."""
        store = _store(tmp_path)
        s = store.create()
        self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {"id": "ann-1", "type": "line", "to": {"x": 1, "y": 1}},
            },
        )
        seq_before = s.seq
        with pytest.raises(OpError):
            self._apply(
                store,
                s,
                {
                    "op": "annotation_created",
                    "annotation": {"id": "ann-1", "type": "shape"},
                },
            )
        assert s.seq == seq_before
        assert s.state["annotations"][0]["type"] == "line"

    def test_annotation_updated_rejects_type_change(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        applied = self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {"type": "line", "to": {"x": 1, "y": 1}},
            },
        )
        ann_id = applied["annotation"]["id"]
        seq_before = s.seq
        with pytest.raises(OpError):
            self._apply(
                store,
                s,
                {
                    "op": "annotation_updated",
                    "annotation": {"id": ann_id, "type": "shape"},
                },
            )
        assert s.seq == seq_before
        assert s.state["annotations"][0]["type"] == "line"

    def test_annotation_updated_rejects_type_change_via_kind_alias(self, tmp_path):
        """The legacy `arrow` alias resolves to `line`; a stored `line`
        annotation must still be protected even when the incoming patch
        spells its (different) type via `kind` instead of `type`."""
        store = _store(tmp_path)
        s = store.create()
        applied = self._apply(
            store, s, {"op": "annotation_created", "annotation": {"kind": "note"}}
        )
        ann_id = applied["annotation"]["id"]
        with pytest.raises(OpError):
            self._apply(
                store,
                s,
                {"op": "annotation_updated", "annotation": {"id": ann_id, "kind": "arrow"}},
            )
        assert s.state["annotations"][0]["type"] == "note"

    def test_group_membership_changed_requires_group(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        with pytest.raises(OpError):
            self._apply(
                store,
                s,
                {
                    "op": "group_membership_changed",
                    "group_id": "missing",
                    "member_node_ids": [],
                },
            )

    def test_layout_applied_batch(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(
            store,
            s,
            {
                "op": "layout_applied",
                "positions": {"a": {"x": 1, "y": 2}, "b": {"x": 3, "y": 4}},
            },
        )
        assert s.state["positions"] == {
            "a": {"x": 1.0, "y": 2.0},
            "b": {"x": 3.0, "y": 4.0},
        }

    def test_annotation_limit_enforced(self, tmp_path):
        store = SessionStore(InMemorySessionPersistenceBackend(), max_annotations=2)
        s = store.create()
        self._apply(
            store, s, {"op": "annotation_created", "annotation": {"kind": "note"}}
        )
        self._apply(
            store, s, {"op": "annotation_created", "annotation": {"kind": "note"}}
        )
        with pytest.raises(OpError):
            self._apply(
                store, s, {"op": "annotation_created", "annotation": {"kind": "note"}}
            )

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
