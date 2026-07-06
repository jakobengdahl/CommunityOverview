"""
Tests for the op-protocol orchestration (step 3) in
``backend/core/session_manager.py``: ordered apply + broadcast, ephemeral
claim ops, catch-up vs snapshot, rate limiting, batch caps, and the
presence/claim lifecycle on connect/disconnect.
"""

import asyncio

import pytest

from backend.core.session_hub import ClaimMap, InProcessEventBus
from backend.core.session_store import InMemorySessionPersistenceBackend, OpError, SessionStore
from backend.core.session_manager import (
    OpBatchTooLarge,
    RateLimited,
    SessionLimitReached,
    SessionManager,
    SessionNotFound,
)
from backend.service.rest_api import _resolve_stream_event

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

    async def test_batch_is_atomic_on_mid_batch_failure(self):
        """A failing op late in a batch must not apply/broadcast earlier ops.

        Regression for the non-idempotent duplication + subscriber-divergence
        bug: annotation_created before an invalid op used to be applied and
        broadcast, then the batch returned 400 and a retry created a duplicate.
        """
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        with pytest.raises(OpError):
            await mgr.apply_ops(s.id, "c1", 0, [
                {"op": "annotation_created", "annotation": {"kind": "note", "text": "hi"}},
                {"op": "node_moved", "node_id": "a", "position": {"x": "bad"}},
            ])
        after = mgr.get_session(s.id)
        assert after.seq == 0
        assert after.state["annotations"] == []
        assert after.state["node_refs"] == []
        # Nothing was broadcast to subscribers.
        assert await _drain(sub) == []

    async def test_persist_failure_rolls_back_and_does_not_broadcast(self):
        """A persistence failure must roll back in-memory state and broadcast nothing.

        Otherwise the poster gets a 500, retries, and re-applies on top of the
        already-advanced in-memory state — duplicating annotation_created.
        """
        store = SessionStore(InMemorySessionPersistenceBackend())
        mgr = SessionManager(store)
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        def _boom(session):
            raise OSError("disk full")

        store.persist = _boom
        with pytest.raises(OSError):
            await mgr.apply_ops(s.id, "c1", 0, [
                {"op": "annotation_created", "annotation": {"kind": "note", "text": "hi"}},
            ])
        after = mgr.get_session(s.id)
        assert after.seq == 0
        assert after.state["annotations"] == []
        assert store.ops_since(s.id, 0) == []  # ring rolled back too
        assert await _drain(sub) == []

    async def test_atomic_rollback_preserves_prior_state(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["keep"]}])
        with pytest.raises(OpError):
            await mgr.apply_ops(s.id, "c1", 0, [
                {"op": "nodes_added", "node_ids": ["x"]},
                {"op": "group_membership_changed", "group_id": "missing", "member_node_ids": []},
            ])
        after = mgr.get_session(s.id)
        assert after.state["node_refs"] == ["keep"]
        assert after.seq == 1


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


class TestResyncTranslation:
    """R2: a slow consumer's dropped backlog must resync, not diverge forever."""

    async def test_overflowed_subscriber_resync_sentinel_becomes_a_snapshot(self):
        bus = InProcessEventBus(queue_max=2)
        mgr = SessionManager(SessionStore(InMemorySessionPersistenceBackend()), event_bus=bus)
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "slow", "Slow")
        await _drain(sub)  # discard the connect's own presence_joined echo

        # Flood past the tiny queue without draining so the bus drops the
        # backlog and enqueues a `{"type": "resync"}` sentinel (session_hub.py).
        for i in range(5):
            bus.publish(s.id, {"type": "op", "op": {"op": "nodes_added", "node_ids": [f"n{i}"]}, "seq": i})

        events = await _drain(sub)
        assert events[-1] == {"type": "resync"}

        # The stream endpoint must not forward that sentinel verbatim: it
        # translates it into a real snapshot the client already knows how to
        # treat as a resync (a second `snapshot`/`catch_up` delivery).
        resolved = _resolve_stream_event(events[-1], mgr, s.id)
        assert resolved["type"] == "snapshot"
        assert resolved["seq"] == s.seq

    async def test_non_resync_events_pass_through_unchanged(self):
        mgr = _manager()
        s = mgr.create_session()
        event = {"type": "op", "op": {"op": "nodes_added", "node_ids": ["a"]}, "seq": 1}
        assert _resolve_stream_event(event, mgr, s.id) == event


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


class TestOpBatchByteCap:
    """A batch is bounded by *size* as well as op *count* (design §3.9)."""

    async def test_oversized_batch_raises_even_within_count_cap(self):
        # A single op stays under the op-count cap but exceeds the byte cap: a
        # `layout_applied` with many positions is the realistic case.
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        positions = {f"node-{i}": {"x": i, "y": i} for i in range(1000)}
        with pytest.raises(OpBatchTooLarge):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "layout_applied", "positions": positions}])

    async def test_small_batch_within_byte_cap_succeeds(self):
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        res = await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}])
        assert res["seq"] == 1

    async def test_oversized_batch_leaves_state_untouched(self):
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["keep"]}])
        with pytest.raises(OpBatchTooLarge):
            await mgr.apply_ops(
                s.id, "c1", 0,
                [{"op": "layout_applied", "positions": {f"n-{i}": {"x": i, "y": i} for i in range(1000)}}],
            )
        assert mgr.get_session(s.id).state["node_refs"] == ["keep"]
