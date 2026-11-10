# covers: backend/core/session_manager.py
"""Annotation history replay and actor-scoped redo regression tests."""

import copy

import pytest

from backend.core.session_manager import (
    LeaseConflict,
    NoUndoableAction,
    SessionManager,
    UndoConflict,
)
from backend.core.session_store import SessionStore
from backend.core.tests.test_session_manager import _drain, _kind_annotation, _manager

pytestmark = pytest.mark.asyncio


class TestHistoryReplay:
    @pytest.mark.parametrize("action", ["create", "update", "delete"])
    async def test_annotation_history_round_trip_reaches_both_viewers(self, action):
        """Own annotation actions undo/redo in session state and both streams."""
        mgr = _manager()
        s = mgr.create_session()
        ann = _kind_annotation("freehand", "stroke")
        mgr.upsert_annotation(s.id, "c1", ann)
        if action == "update":
            mgr.update_annotation(s.id, "c1", {**ann, "color": "#ff0000"})
        elif action == "delete":
            mgr.delete_annotation(s.id, "c1", "stroke")
        expected = copy.deepcopy(s.state["annotations"])
        own, _ = mgr.connect(s.id, "c1", "A")
        other, _ = mgr.connect(s.id, "c2", "B")
        await _drain(own)
        await _drain(other)
        mgr.undo_last_action(s.id, "c1")
        if action == "create":
            assert s.state["annotations"] == []
        elif action == "delete":
            assert s.state["annotations"][0]["id"] == "stroke"
        else:
            assert s.state["annotations"][0].get("color") == ann.get("color")
        result = mgr.redo_last_action(s.id, "c1")
        for actual, wanted in zip(s.state["annotations"], expected, strict=True):
            assert {
                k: v
                for k, v in actual.items()
                if k not in {"version", "field_versions", "updated_at"}
            } == {
                k: v
                for k, v in wanted.items()
                if k not in {"version", "field_versions", "updated_at"}
            }
        for sub in (own, other):
            ops = [e for e in await _drain(sub) if e.get("type") == "op"]
            assert len(ops) == 2
            assert all(e["client_id"] != "c1" for e in ops)
            assert ops[-1]["op"] == result["applied"]

    async def test_redo_is_actor_scoped_and_fresh_actions_invalidate_it(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "c1", {"id": "a", "type": "note"})
        mgr.undo_last_action(s.id, "c1")
        with pytest.raises(NoUndoableAction):
            mgr.redo_last_action(s.id, "c2")
        mgr.upsert_annotation(s.id, "c2", {"id": "b", "type": "note"})
        mgr.redo_last_action(s.id, "c1")
        mgr.undo_last_action(s.id, "c1")
        mgr.upsert_annotation(s.id, "c1", {"id": "c", "type": "note"})
        with pytest.raises(NoUndoableAction):
            mgr.redo_last_action(s.id, "c1")

    async def test_redo_refuses_concurrent_edits_and_leases(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "c1", {"id": "a", "type": "note", "text": "a"})
        mgr.update_annotation(s.id, "c1", {"id": "a", "type": "note", "text": "b"})
        mgr.undo_last_action(s.id, "c1")
        mgr.leases.acquire(s.id, "c2", ["a"])
        with pytest.raises(LeaseConflict):
            mgr.redo_last_action(s.id, "c1")
        mgr.leases.release(s.id, "c2", ["a"])
        mgr.update_annotation(s.id, "c2", {"id": "a", "type": "note", "text": "c"})
        with pytest.raises(UndoConflict):
            mgr.redo_last_action(s.id, "c1")

    async def test_redo_failure_rolls_back_and_can_retry_after_reload(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "c1", {"id": "a", "type": "note"})
        mgr.undo_last_action(s.id, "c1")
        saved = copy.deepcopy(s.to_dict())
        persist = mgr.store.persist

        def fail(_session):
            raise OSError()

        mgr.store.persist = fail
        with pytest.raises(OSError):
            mgr.redo_last_action(s.id, "c1")
        assert s.to_dict() == saved
        mgr.store.persist = persist
        reloaded = SessionManager(SessionStore(mgr.store._backend))
        reloaded.redo_last_action(s.id, "c1")
        assert reloaded.store.get(s.id).state["annotations"][0]["id"] == "a"

    async def test_multiple_undos_and_redos_restore_edit_sequence(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "c1", {"id": "a", "type": "note", "text": "a"})
        mgr.update_annotation(s.id, "c1", {"id": "a", "type": "note", "text": "b"})
        mgr.undo_last_action(s.id, "c1")
        mgr.undo_last_action(s.id, "c1")
        mgr.redo_last_action(s.id, "c1")
        mgr.redo_last_action(s.id, "c1")
        assert s.state["annotations"][0]["text"] == "b"

    async def test_undo_redo_node_moved_restores_positions(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.apply_layout(s.id, "mcp-agent", positions={"n1": {"x": 1.0, "y": 2.0}})
        await mgr.apply_ops(
            s.id,
            "mcp-agent",
            None,
            [{"op": "node_moved", "node_id": "n1", "position": {"x": 9.0, "y": 9.0}}],
        )

        mgr.undo_last_action(s.id, "mcp-agent")

        assert s.state["positions"]["n1"] == {"x": 1.0, "y": 2.0}
        mgr.redo_last_action(s.id, "mcp-agent")
        assert s.state["positions"]["n1"] == {"x": 9.0, "y": 9.0}
