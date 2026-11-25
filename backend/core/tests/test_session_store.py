# covers: backend/core/session_store.py, backend/core/session_manager.py
"""
Tests for the server-side session store (step 1) and op state transforms
(step 3) in ``backend/core/session_store.py``.

Covers: persistence roundtrip + atomic file writes, CRUD, id allocation,
per-op state transforms (union/removal/LWW), annotation id assignment and the
dropped-update rule, and the ring buffer catch-up continuity check.
"""

import copy
import errno
import json
import os
import threading

import pytest

from backend.core import session_store, storage_backends
from backend.core.session_store import (
    AnnotationFieldConflict,
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    MAX_SESSION_DESCRIPTION_CHARS,
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

    def test_rejects_trailing_characters(self):
        assert not is_valid_session_id("1234-5678\n")
        assert not is_valid_session_id("1234-5678-9012-3456\n")
        assert not is_valid_session_id("1234-5678x")
        assert not is_valid_session_id("1234-5678-9012-3456x")

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
        # A pre-appearance session fixture has no shape keys.
        legacy = session.to_dict()
        legacy["state"].pop("shape_overrides")
        legacy["state"].pop("type_shapes")
        backend.save(legacy)
        restored = Session.from_dict(backend.load("1111-2222"))
        assert restored.state["shape_overrides"] == {}
        assert restored.state["type_shapes"] == {}
        roundtrip = restored.to_dict()
        for key in ("shape_overrides", "type_shapes"):
            roundtrip["state"].pop(key)
        assert roundtrip == legacy
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

    def test_nodes_added_drops_view_layout_keys(self, tmp_path):
        """Only a replace carries a view's layout; an add relays none of it."""
        store = _store(tmp_path)
        s = store.create()
        applied = self._apply(
            store,
            s,
            {
                "op": "nodes_added",
                "node_ids": ["a"],
                "hidden_node_ids": ["a"],
                "annotations": [{"id": "x", "kind": "note"}],
                "removed_annotation_ids": ["y"],
            },
        )
        assert not {"hidden_node_ids", "annotations", "removed_annotation_ids"} & set(
            applied
        )
        assert s.state["hidden_node_ids"] == []
        assert s.state["annotations"] == []

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

    def test_session_described_persists_and_outlives_clearing_the_canvas(
        self, tmp_path
    ):
        store = _store(tmp_path)
        s = store.create(name="n")
        self._apply(store, s, {"op": "nodes_added", "node_ids": ["a"]})
        self._apply(store, s, {"op": "session_described", "description": "brief"})
        self._apply(store, s, {"op": "nodes_removed", "node_ids": ["a"]})
        store.persist(s)

        reloaded = _store(tmp_path).get(s.id)
        assert reloaded.description == "brief"
        assert reloaded.name == "n"
        assert {m["id"]: m for m in store.list_meta()}[s.id]["description"] == "brief"

    def test_session_described_rejects_an_oversized_description(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        too_long = "x" * (MAX_SESSION_DESCRIPTION_CHARS + 1)
        with pytest.raises(OpError):
            self._apply(store, s, {"op": "session_described", "description": too_long})
        assert s.description is None
        assert s.seq == 0

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

    def test_dim_restore_nodes_and_edges(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(store, s, {"op": "nodes_dimmed", "node_ids": ["a", "b"]})
        self._apply(store, s, {"op": "nodes_undimmed", "node_ids": ["a"]})
        assert s.state["dimmed_node_ids"] == ["b"]
        self._apply(store, s, {"op": "edges_dimmed", "edge_ids": ["e1", "e2"]})
        self._apply(store, s, {"op": "edges_undimmed", "edge_ids": ["e2"]})
        assert s.state["dimmed_edge_ids"] == ["e1"]

    def test_nodes_removed_also_cleans_dimmed_node_ids(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        self._apply(store, s, {"op": "nodes_added", "node_ids": ["a", "b"]})
        self._apply(store, s, {"op": "nodes_dimmed", "node_ids": ["a", "b"]})
        self._apply(store, s, {"op": "nodes_removed", "node_ids": ["a"]})
        assert s.state["dimmed_node_ids"] == ["b"]

    def test_edge_intensity_set_clamps_and_defaults_to_full(self, tmp_path):
        store = _store(tmp_path)
        s = store.create()
        assert s.state["edge_intensity"] == 1.0
        applied = self._apply(store, s, {"op": "edge_intensity_set", "value": 0.4})
        assert applied["value"] == 0.4
        assert s.state["edge_intensity"] == 0.4
        # Out-of-range values clamp rather than raise, so a slightly stale
        # client cannot fail the whole batch on a cosmetic overshoot.
        self._apply(store, s, {"op": "edge_intensity_set", "value": 5})
        assert s.state["edge_intensity"] == 1.0
        self._apply(store, s, {"op": "edge_intensity_set", "value": -2})
        assert s.state["edge_intensity"] == 0.0
        with pytest.raises(OpError):
            self._apply(store, s, {"op": "edge_intensity_set", "value": "bright"})

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

    def test_annotation_updated_trusted_replay_removes_field_a_sparse_update_added(
        self, tmp_path
    ):
        """Regression for smallfix-undo-of-a-field-adding-update-leaves-the-field.

        A sparse ``annotation_updated`` (the MCP patch path — only the
        touched fields are sent) that ADDS a field the annotation never had
        cannot be undone by a plain merge: undo replays the pre-update
        snapshot, which simply lacks that key, so ``target.update(prior)``
        can restore/overwrite keys the snapshot has but can never remove one
        it does not. ``trusted_replay=True`` (set only by the undo path —
        see ``apply_state_op``'s docstring) must make the replay a full
        replace instead, so the added field is gone afterwards, not merely
        reverted to some other value.
        """
        store = _store(tmp_path)
        s = store.create()
        created = self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "kind": "note"},
            },
        )
        assert "text" not in created["annotation"]
        prior_snapshot = copy.deepcopy(created["annotation"])

        updated = self._apply(
            store,
            s,
            {
                "op": "annotation_updated",
                "annotation": {"id": "note-1", "kind": "note", "text": "hi"},
            },
        )
        assert updated["annotation"]["text"] == "hi"
        update_version = updated["annotation"]["version"]

        # Simulate undo_last_action's replay of the stored inverse op: the
        # exact pre-update snapshot, applied with trusted_replay=True.
        replayed = store.apply_state_op(
            s,
            {"op": "annotation_updated", "annotation": prior_snapshot},
            record_activity=False,
            trusted_replay=True,
        )

        restored = replayed["annotation"]
        assert "text" not in restored, (
            "undo of a sparse update that ADDED 'text' must remove the field "
            "entirely, not merely leave it in place"
        )
        # The removal is itself a change to 'text': its field version must
        # track the undo, not the update that added it, or a later
        # base_version check would read 'text' as unchanged since that update.
        assert restored["version"] == update_version + 1
        assert restored["field_versions"].get("text") == restored["version"]

    def test_stale_patch_to_a_field_undo_removed_conflicts(self, tmp_path):
        """The consequence of the field-version bump above: a writer still on
        the update's version that touches 'text' after undo removed it must be
        told it conflicts, not silently re-add the field."""
        store = _store(tmp_path)
        s = store.create()
        created = self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "kind": "note"},
            },
        )
        prior_snapshot = copy.deepcopy(created["annotation"])
        updated = self._apply(
            store,
            s,
            {
                "op": "annotation_updated",
                "annotation": {"id": "note-1", "kind": "note", "text": "hi"},
            },
        )
        update_version = updated["annotation"]["version"]
        store.apply_state_op(
            s,
            {"op": "annotation_updated", "annotation": prior_snapshot},
            record_activity=False,
            trusted_replay=True,
        )
        seq_after_undo = s.seq

        with pytest.raises(AnnotationFieldConflict) as exc:
            self._apply(
                store,
                s,
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "note-1",
                        "kind": "note",
                        "text": "stale edit",
                    },
                    "base_version": update_version,
                },
            )
        assert set(exc.value.conflicts) == {"text"}
        assert "text" not in s.state["annotations"][0]
        assert s.seq == seq_after_undo

    def test_annotation_updated_non_replay_merge_is_unaffected(self, tmp_path):
        """A normal (non-undo) ``annotation_updated`` — trusted_replay=False —
        must keep its existing shallow-merge semantics: untouched fields
        survive, exactly as before this fix. Only the undo-replay path
        (trusted_replay=True) was changed."""
        store = _store(tmp_path)
        s = store.create()
        self._apply(
            store,
            s,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "kind": "note", "color": "red"},
            },
        )

        applied = self._apply(
            store,
            s,
            {
                "op": "annotation_updated",
                "annotation": {"id": "note-1", "kind": "note", "text": "hi"},
            },
        )

        restored = applied["annotation"]
        assert restored["text"] == "hi"
        assert restored["color"] == "red"  # untouched field survives the merge

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
                {
                    "op": "annotation_updated",
                    "annotation": {"id": ann_id, "kind": "arrow"},
                },
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


class TestRestoreRing:
    def _two_sessions(self):
        """``b`` is created first and holds the longer ring, so a restore that
        lands on the first ring or trims every ring to ``saved`` shows."""
        store = SessionStore(InMemorySessionPersistenceBackend())
        b, a = store.create(), store.create()
        store.apply_state_op(a, {"op": "nodes_added", "node_ids": ["a0"]})
        for i in range(3):
            store.apply_state_op(b, {"op": "nodes_added", "node_ids": [f"b{i}"]})
        return store, a, b

    def test_restoring_saved_contents_leaves_other_sessions_rings_alone(self):
        store, a, b = self._two_sessions()
        saved = list(store.ring(a.id))
        b_before = list(store.ring(b.id))
        store.apply_state_op(a, {"op": "nodes_added", "node_ids": ["later"]})

        store.restore_ring(a.id, saved)

        assert list(store.ring(a.id)) == saved
        assert list(store.ring(b.id)) == b_before

    def test_restoring_no_ring_drops_only_that_sessions_ring(self):
        store, a, b = self._two_sessions()
        b_before = list(store.ring(b.id))

        store.restore_ring(a.id, None)

        assert store.ring(a.id) is None
        assert list(store.ring(b.id)) == b_before

    def test_restoring_keeps_the_ring_bounded(self):
        store = SessionStore(InMemorySessionPersistenceBackend(), ring_size=2)
        s = store.create()
        store.apply_state_op(s, {"op": "nodes_added", "node_ids": ["n0"]})
        saved = list(store.ring(s.id))
        store.apply_state_op(s, {"op": "nodes_added", "node_ids": ["n1"]})

        store.restore_ring(s.id, saved)
        for i in range(2, 5):
            store.apply_state_op(s, {"op": "nodes_added", "node_ids": [f"n{i}"]})

        assert store.ring(s.id).maxlen == 2
        assert [op["seq"] for op in store.ring(s.id)] == [4, 5]


class TestAppearanceOperations:
    def test_special_dictionary_keys_survive_persistence_and_removal(self, tmp_path):
        store = _store(tmp_path)
        session = store.create()
        store.apply_state_op(session, {"op": "nodes_added", "node_ids": ["__proto__"]})
        for op in (
            dict(op="node_shape_set", node_id="__proto__"),
            dict(op="type_shape_set", type="__proto__"),
        ):
            store.apply_state_op(
                session,
                {
                    **op,
                    "version": 1,
                    "selection": {"builtin": True},
                    "base_revision": 0,
                    "operation_id": op["op"],
                    "client_id": "c",
                },
            )
        store.persist(session)
        restored = _store(tmp_path).get(session.id)
        assert restored.state["shape_overrides"]["__proto__"] == {"builtin": True}
        assert restored.state["type_shapes"]["__proto__"] == {"builtin": True}
        store.apply_state_op(
            session, {"op": "nodes_removed", "node_ids": ["__proto__"]}
        )
        assert "__proto__" not in session.state["shape_overrides"]
        assert session.state["shape_revisions"]["node:__proto__"] == 2

    def test_validated_selections_reject_malformed_payloads(self, tmp_path):
        """Only complete supported selections can enter session state."""
        store = _store(tmp_path)
        session = store.create()
        for selection in [
            {"builtin": True},
            {"ref": "config:card"},
            {"ref": "saved:card"},
            {
                "inline": {
                    "version": 1,
                    "base": "triangle-up",
                    "scale": 1,
                    "placement": "centre",
                    "areas": {"first": "name", "second": None, "third": None},
                }
            },
            None,
        ]:
            op = dict(
                op="type_shape_set",
                type="Thing",
                selection=selection,
                version=1,
                base_revision=session.state["shape_revisions"].get("type:Thing", 0),
                operation_id=str(session.seq),
            )
            assert store.apply_state_op(session, op)["seq"] == session.seq
            assert session.state["type_shapes"].get("Thing") == selection
        before = copy.deepcopy(session.to_dict())
        for selection in [
            {"builtin": False},
            {"ref": "card"},
            {"builtin": True, "ref": "saved:card"},
            {"inline": {"version": 2}},
        ]:
            with pytest.raises(OpError):
                store.apply_state_op(
                    session, {**op, "selection": selection, "operation_id": "invalid"}
                )
        for extra in ({"node_id": "a"}, {"unexpected": True}):
            with pytest.raises(OpError):
                store.apply_state_op(session, {**op, **extra})
        assert session.to_dict() == before

    def test_revision_retry_persistence_and_atomic_undo(self, tmp_path):
        """Concurrent edits conflict and retries never repeat an atomic type change."""
        from backend.core.session_store import ShapeRevisionConflict

        store = _store(tmp_path)
        session = store.create()
        store.apply_state_op(session, {"op": "nodes_added", "node_ids": ["a", "b"]})
        for node_id in ["a", "b"]:
            store.apply_state_op(
                session,
                dict(
                    op="node_shape_set",
                    node_id=node_id,
                    selection={"builtin": True},
                    version=1,
                    base_revision=0,
                    operation_id=node_id,
                    client_id="first",
                ),
            )
        node_record = store.find_latest_undoable(session, "first")
        store.apply_state_op(
            session,
            node_record["inverse_op"],
            trusted_replay=True,
            record_activity=False,
        )
        assert "b" not in session.state["shape_overrides"]
        store.apply_state_op(
            session,
            {
                "op": "node_shape_set",
                "node_id": "b",
                "selection": {"builtin": True},
                "version": 1,
            },
            trusted_replay=True,
            record_activity=False,
        )
        op = dict(
            op="type_shape_set",
            type="Thing",
            selection={"ref": "config:card"},
            clear_node_id="a",
            clear_base_revision=1,
            version=1,
            base_revision=0,
            operation_id="type",
            client_id="first",
        )
        seq = session.seq
        store.apply_state_op(session, op)
        assert session.seq == seq + 1
        assert session.state["shape_overrides"] == {"b": {"builtin": True}}
        record = store.find_latest_undoable(session, "first")
        assert store.undo_conflict_reason(session, record) is None
        store.persist(session)
        restored_store = _store(tmp_path)
        restored = restored_store.get(session.id)
        assert restored.to_dict() == session.to_dict()
        assert restored_store.apply_state_op(restored, op) is None
        current_retry = {
            **op,
            "client_id": "second",
            "base_revision": 1,
            "clear_base_revision": 2,
            "selection": {"builtin": True},
        }
        assert restored_store.apply_state_op(restored, current_retry) is None
        reconnect_retry = {**op, "client_id": "second"}
        before_retry = copy.deepcopy(restored.to_dict())
        assert restored_store.apply_state_op(restored, reconnect_retry) is None
        assert restored.to_dict() == before_retry
        receipt = json.dumps([op["operation_id"]])
        restored.state["shape_operation_ids"][
            json.dumps(["first", op["operation_id"]])
        ] = restored.state["shape_operation_ids"].pop(receipt)
        assert restored_store.apply_state_op(restored, current_retry) is None
        before_retry = copy.deepcopy(restored.to_dict())
        assert restored_store.apply_state_op(restored, reconnect_retry) is None
        assert restored.to_dict() == before_retry
        with pytest.raises(ShapeRevisionConflict):
            restored_store.apply_state_op(restored, {**op, "operation_id": "new"})
        assert restored.seq == seq + 1
        assert restored.state["type_shapes"] == {"Thing": {"ref": "config:card"}}
        restored_store.apply_state_op(
            restored, record["inverse_op"], trusted_replay=True, record_activity=False
        )
        assert restored.state["shape_overrides"] == {
            "a": {"builtin": True},
            "b": {"builtin": True},
        }
        assert restored.state["type_shapes"] == {}
        restored_store.apply_state_op(
            restored, {"op": "nodes_removed", "node_ids": ["a"]}
        )
        assert "a" not in restored.state["shape_overrides"]

    def test_node_operation_rejects_clear_fields(self, tmp_path):
        store = _store(tmp_path)
        session = store.create()
        store.apply_state_op(session, {"op": "nodes_added", "node_ids": ["a", "b"]})
        before = copy.deepcopy(session.to_dict())
        for field, value in [
            ("clear_node_id", "b"),
            ("clear_base_revision", 0),
            ("type", "Thing"),
        ]:
            with pytest.raises(OpError):
                store.apply_state_op(
                    session,
                    dict(
                        op="node_shape_set",
                        node_id="a",
                        selection={"builtin": True},
                        version=1,
                        base_revision=0,
                        operation_id="bad",
                        **{field: value},
                    ),
                )
            assert session.to_dict() == before


def _in_thread(fn):
    result = {}
    thread = threading.Thread(target=lambda: result.update(value=fn()), daemon=True)
    thread.start()
    return thread, result


class TestTransactionCoversReads:
    """A second backend over the same directory stands in for another process:
    it shares only the ``.sessions.lock`` file, not the in-process lock."""

    @pytest.mark.parametrize(
        "read",
        [
            lambda store, sid: store.get(sid).name,
            lambda store, sid: store._backend.load(sid)["name"],
        ],
        ids=["store_get_miss", "backend_load"],
    )
    def test_read_waits_for_a_transaction_and_sees_its_end_state(self, tmp_path, read):
        writer = FileSessionPersistenceBackend(tmp_path)
        raw = Session(id="1111-2222", name="before").to_dict()
        writer.save(raw)
        store = SessionStore(FileSessionPersistenceBackend(tmp_path))
        with writer.transaction():
            writer.save({**raw, "name": "partial"})
            reader, result = _in_thread(lambda: read(store, raw["id"]))
            reader.join(0.3)
            assert reader.is_alive()
            writer.delete(raw["id"])
            writer.save({**raw, "name": "after"})
        reader.join(5)
        assert result["value"] == "after"

    def test_cache_hit_returns_the_cached_session_without_the_lock(self, tmp_path):
        writer = FileSessionPersistenceBackend(tmp_path)
        writer.save(Session(id="1111-2222", name="before").to_dict())
        store = SessionStore(FileSessionPersistenceBackend(tmp_path))
        cached = store.get("1111-2222")
        with writer.transaction():
            writer.save(Session(id="1111-2222", name="after").to_dict())
            reader, result = _in_thread(lambda: store.get("1111-2222"))
            reader.join(5)
        assert result["value"] is cached
        assert cached.name == "before"

    @pytest.mark.parametrize("case", ["missing", "invalid_id", "corrupt"])
    def test_failed_read_releases_the_lock_and_caches_nothing(self, tmp_path, case):
        backend = FileSessionPersistenceBackend(tmp_path)
        store = SessionStore(backend)
        if case == "invalid_id":
            with pytest.raises(OpError):
                backend.load("../etc")
        elif case == "corrupt":
            (tmp_path / "1111-2222.json").write_text("{", encoding="utf-8")
            with pytest.raises(ValueError):
                store.get("1111-2222")
        else:
            assert store.get("1111-2222") is None
        other = FileSessionPersistenceBackend(tmp_path)
        writer, _ = _in_thread(
            lambda: other.save(Session(id="1111-2222", name="later").to_dict())
        )
        writer.join(5)
        assert not writer.is_alive()
        assert store.get("1111-2222").name == "later"


@pytest.mark.parametrize("module", [session_store, storage_backends])
def test_windows_file_lock_waits_until_acquired(module, monkeypatch, tmp_path):
    """On Windows ``_lock_file`` retries a timed-out lock until it is held,
    for shared and exclusive requests alike, and raises any other error. Lock
    and unlock always target byte 0, whatever the handle's position, and leave
    that position as it was. The session backend's transaction lock takes the
    same waiting path."""
    calls = []
    positions = []
    outcomes = []

    class FakeMsvcrt:
        LK_LOCK, LK_NBLCK, LK_UNLCK = 1, 2, 0

        @staticmethod
        def locking(fileno, mode, nbytes):
            calls.append(mode)
            positions.append(os.lseek(fileno, 0, os.SEEK_CUR))
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise OSError(outcome, "locked")

    monkeypatch.setattr(module, "msvcrt", FakeMsvcrt)
    (tmp_path / "lock").write_text("existing")
    with open(tmp_path / "lock", "ab") as f:
        eof = os.lseek(f.fileno(), 0, os.SEEK_END)
        for exclusive in (True, False):
            calls.clear()
            outcomes[:] = [errno.EDEADLOCK, errno.EACCES, None]
            module._lock_file(f, exclusive=exclusive)
            assert calls == [FakeMsvcrt.LK_LOCK] * 3
        outcomes[:] = [errno.EBADF]
        with pytest.raises(OSError):
            module._lock_file(f)
        outcomes[:] = [None]
        calls.clear()
        module._unlock_file(f)
        assert calls == [FakeMsvcrt.LK_UNLCK]
        assert set(positions) == {0}
        assert os.lseek(f.fileno(), 0, os.SEEK_CUR) == eof > 0
    if module is session_store:
        outcomes[:] = [None, None]
        backend = FileSessionPersistenceBackend(tmp_path / "s")
        calls.clear()
        outcomes[:] = [errno.EDEADLOCK, None, None]
        with backend.transaction():
            pass
        assert calls == [FakeMsvcrt.LK_LOCK] * 2 + [FakeMsvcrt.LK_UNLCK]


def test_creation_paths_share_capacity_across_stores(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from backend.core.session_manager import SessionLimitReached, SessionManager

    first = SessionManager(_store(tmp_path), max_sessions=2)
    source = first.create_session()
    second = SessionManager(_store(tmp_path), max_sessions=2)
    barrier = threading.Barrier(2)

    def create(copy):
        barrier.wait()
        try:
            return second.copy_session(source.id) if copy else first.create_session()
        except SessionLimitReached:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (False, True)))
    assert sum(result is not None for result in results) == 1
    assert len(_store(tmp_path).list_meta()) == 2

    roomy = SessionManager(_store(tmp_path), max_sessions=4)
    assert roomy.create_session()
    assert roomy.copy_session(source.id)


def test_unpublished_document_is_invisible_and_swept_on_restart(tmp_path, monkeypatch):
    store = _store(tmp_path)
    published = store.create()
    directory = tmp_path / "sessions"
    original_rename = session_store.os.rename

    def crash_before_publish(source, destination):
        raise KeyboardInterrupt()

    monkeypatch.setattr(session_store.os, "rename", crash_before_publish)
    with pytest.raises(KeyboardInterrupt):
        store.create_with_capacity(max_sessions=2)
    monkeypatch.setattr(session_store.os, "rename", original_rename)
    assert store.session_count() == 1
    assert [m["id"] for m in store.list_meta()] == [published.id]
    orphan = list(directory.glob("session_*.json"))
    assert len(orphan) == 1
    partial = directory / "session_partial.json"
    partial.write_text("{")
    restarted = _store(tmp_path)
    assert not orphan[0].exists()
    assert not partial.exists()
    assert restarted.get(published.id) is not None
    assert restarted.create_with_capacity(max_sessions=2)[1]


def test_publication_flushes_and_is_visible_to_another_store(tmp_path, monkeypatch):
    first = _store(tmp_path)
    second = _store(tmp_path)
    calls = []
    real_fsync = os.fsync

    def record_fsync(fd):
        calls.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(session_store.os, "fsync", record_fsync)
    session = first.create()
    import stat

    assert any(stat.S_ISREG(mode) for mode in calls)
    assert any(stat.S_ISDIR(mode) for mode in calls)
    assert second.get(session.id).id == session.id


def test_failed_directory_flush_removes_new_session(tmp_path, monkeypatch):
    store = _store(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            session_store,
            "_fsync_directory",
            lambda _: (_ for _ in ()).throw(OSError()),
        )
        with pytest.raises(OSError):
            store.create_with_capacity(max_sessions=1)
    assert store.session_count() == 0
    assert store.create_with_capacity(max_sessions=1)[1]
