"""
Tests for the op-protocol orchestration (step 3) in
``backend/core/session_manager.py``: ordered apply + broadcast, ephemeral
claim ops, catch-up vs snapshot, rate limiting, batch caps, and the
presence/claim lifecycle on connect/disconnect.
"""

import asyncio

import pytest

from backend.core.session_hub import ClaimMap
from backend.core.session_store import InMemorySessionPersistenceBackend, OpError, SessionStore
from backend.core.session_manager import (
    OpBatchTooLarge,
    RateLimited,
    SessionLimitReached,
    SessionManager,
    SessionNotFound,
)

pytestmark = pytest.mark.asyncio


def _manager(**kwargs) -> SessionManager:
    return SessionManager(SessionStore(InMemorySessionPersistenceBackend()), **kwargs)


async def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


class TestApplyOps:
    async def test_ordered_apply_and_broadcast(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)  # discard presence_joined
        res = await mgr.apply_ops(s.id, "c1", 0, [
            {"op": "nodes_added", "node_ids": ["a"]},
            {"op": "node_moved", "node_id": "a", "position": {"x": 1, "y": 2}},
        ])
        assert res["seq"] == 2
        events = await _drain(sub)
        assert [e["op"]["op"] for e in events] == ["nodes_added", "node_moved"]
        assert [e["seq"] for e in events] == [1, 2]

    async def test_persist_once_per_batch(self):
        store = SessionStore(InMemorySessionPersistenceBackend())
        mgr = SessionManager(store)
        s = mgr.create_session()
        calls = {"n": 0}
        original = store.persist
        store.persist = lambda session: (calls.__setitem__("n", calls["n"] + 1), original(session))[1]
        await mgr.apply_ops(s.id, "c1", 0, [
            {"op": "nodes_added", "node_ids": ["a"]},
            {"op": "nodes_added", "node_ids": ["b"]},
        ])
        assert calls["n"] == 1

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            await mgr.apply_ops("9999-9999", "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}])

    async def test_batch_too_large(self):
        mgr = _manager(max_ops_per_batch=2)
        s = mgr.create_session()
        with pytest.raises(OpBatchTooLarge):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": []}] * 3)

    async def test_rate_limit(self):
        mgr = _manager(bucket_capacity=2, bucket_refill_per_sec=0)
        s = mgr.create_session()
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}])
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["b"]}])
        with pytest.raises(RateLimited):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["c"]}])

    async def test_invalid_op_raises_operror(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "bogus"}])


class TestClaimOps:
    async def test_claim_ops_are_ephemeral(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        res = await mgr.apply_ops(s.id, "c1", 0, [{"op": "selection_claimed", "element_ids": ["n1"]}])
        # claims do not advance the persisted seq
        assert res["seq"] == 0
        assert mgr.claims.snapshot(s.id) == {"n1": "c1"}
        events = await _drain(sub)
        assert events[0]["op"]["op"] == "selection_claimed"

    async def test_claim_requires_element_ids(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "selection_claimed"}])


class TestCatchUp:
    async def test_catch_up_returns_missed_ops(self):
        mgr = _manager()
        s = mgr.create_session()
        for i in range(3):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": [f"n{i}"]}])
        cu = mgr.catch_up(s.id, 1)
        assert cu["type"] == "catch_up"
        assert [op["seq"] for op in cu["ops"]] == [2, 3]

    async def test_catch_up_falls_back_to_snapshot(self):
        store = SessionStore(InMemorySessionPersistenceBackend(), ring_size=1)
        mgr = SessionManager(store)
        s = mgr.create_session()
        for i in range(3):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": [f"n{i}"]}])
        cu = mgr.catch_up(s.id, 0)
        assert cu["type"] == "snapshot"
        assert cu["session"]["state"]["node_refs"] == ["n0", "n1", "n2"]

    async def test_no_since_seq_is_snapshot(self):
        mgr = _manager()
        s = mgr.create_session()
        cu = mgr.catch_up(s.id, None)
        assert cu["type"] == "snapshot"


class TestLifecycle:
    async def test_connect_broadcasts_presence(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, member = mgr.connect(s.id, "c1", "Alice")
        events = await _drain(sub)
        assert events[0]["type"] == "presence_joined"
        assert member["display_name"] == "Alice"

    async def test_disconnect_releases_claims_and_presence(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "selection_claimed", "element_ids": ["n1"]}])
        mgr.disconnect(s.id, "c1", sub)
        assert mgr.claims.snapshot(s.id) == {}
        assert mgr.roster(s.id) == []

    async def test_delete_broadcasts(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        assert mgr.delete_session(s.id, deleted_by="c1") is True
        events = await _drain(sub)
        assert events[0]["type"] == "session_deleted"
        assert events[0]["deleted_by"] == "c1"
        assert mgr.get_session(s.id) is None

    async def test_session_limit(self):
        mgr = _manager(max_sessions=1)
        mgr.create_session()
        with pytest.raises(SessionLimitReached):
            mgr.create_session()

    async def test_push_command_unknown_session(self):
        mgr = _manager()
        assert mgr.push_command("9999-9999", {"type": "x"}) is False

    async def test_push_command_broadcasts(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        assert mgr.push_command(s.id, {"type": "tool_result"}) is True
        events = await _drain(sub)
        assert events[0]["type"] == "command"
