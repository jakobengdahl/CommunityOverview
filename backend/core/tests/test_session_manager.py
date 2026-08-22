"""
Tests for the op-protocol orchestration (step 3) in
``backend/core/session_manager.py``: ordered apply + broadcast, ephemeral
claim ops, catch-up vs snapshot, rate limiting, batch caps, and the
presence/claim lifecycle on connect/disconnect.
"""

import asyncio
import threading

import pytest

from backend.core.session_hub import InProcessEventBus
from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    OpError,
    SessionStore,
)
from backend.core.session_manager import (
    AnnotationNotFound,
    AnnotationRecentlyDeleted,
    LayoutBusy,
    OpBatchTooLarge,
    RateLimited,
    RevisionConflict,
    SessionLimitReached,
    SessionManager,
    SessionNotFound,
    _TokenBucket,
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
        res = await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {"op": "nodes_added", "node_ids": ["a"]},
                {"op": "node_moved", "node_id": "a", "position": {"x": 1, "y": 2}},
            ],
        )
        assert res["seq"] == 2
        events = await _drain(sub)
        assert [e["op"]["op"] for e in events] == ["nodes_added", "node_moved"]
        assert [e["seq"] for e in events] == [1, 2]

    async def test_persist_once_per_batch(self):
        store = SessionStore(InMemorySessionPersistenceBackend())
        mgr = SessionManager(store)
        s = mgr.create_session()
        calls = {"n": 0}
        original = store.persist_snapshot
        store.persist_snapshot = lambda snapshot: (
            calls.__setitem__("n", calls["n"] + 1),
            original(snapshot),
        )[1]
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {"op": "nodes_added", "node_ids": ["a"]},
                {"op": "nodes_added", "node_ids": ["b"]},
            ],
        )
        assert calls["n"] == 1

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            await mgr.apply_ops(
                "9999-9999", "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}]
            )

    async def test_batch_too_large(self):
        mgr = _manager(max_ops_per_batch=2)
        s = mgr.create_session()
        with pytest.raises(OpBatchTooLarge):
            await mgr.apply_ops(
                s.id, "c1", 0, [{"op": "nodes_added", "node_ids": []}] * 3
            )

    async def test_rate_limit(self):
        mgr = _manager(bucket_capacity=2, bucket_refill_per_sec=0)
        s = mgr.create_session()
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}])
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["b"]}])
        with pytest.raises(RateLimited):
            await mgr.apply_ops(
                s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["c"]}]
            )

    async def test_lookup_rate_limit_throttles_per_key(self):
        """Session-id lookups are throttled per source and refill over time."""
        mgr = _manager(lookup_bucket_capacity=2, lookup_refill_per_sec=0)
        mgr.check_lookup_rate("1.2.3.4")
        mgr.check_lookup_rate("1.2.3.4")
        with pytest.raises(RateLimited):
            mgr.check_lookup_rate("1.2.3.4")
        # A different source has its own budget.
        mgr.check_lookup_rate("5.6.7.8")

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
            await mgr.apply_ops(
                s.id,
                "c1",
                0,
                [
                    {
                        "op": "annotation_created",
                        "annotation": {"kind": "note", "text": "hi"},
                    },
                    {"op": "node_moved", "node_id": "a", "position": {"x": "bad"}},
                ],
            )
        after = mgr.get_session(s.id)
        assert after.seq == 0
        assert after.state["annotations"] == []
        assert after.state["node_refs"] == []
        # Nothing was broadcast to subscribers.
        assert await _drain(sub) == []

    async def test_batch_rejects_annotation_retype_and_rolls_back(self):
        """The raw ``apply_ops`` batch path (what the browser's websocket
        write path actually calls) must not be able to retype an existing
        annotation either — the store-level check must not be something only
        the MCP tool layer's pre-checks enforce. A same-batch retype attempt
        must also roll back any earlier op in that batch."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "ann-1", "type": "line"})
        seq_before = s.seq
        with pytest.raises(OpError):
            await mgr.apply_ops(
                s.id,
                "c1",
                seq_before,
                [
                    {"op": "annotation_created", "annotation": {"kind": "note"}},
                    {
                        "op": "annotation_updated",
                        "annotation": {"id": "ann-1", "type": "shape"},
                    },
                ],
            )
        after = mgr.get_session(s.id)
        assert after.seq == seq_before
        assert len(after.state["annotations"]) == 1
        assert after.state["annotations"][0]["type"] == "line"

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

        def _boom(snapshot):
            raise OSError("disk full")

        store.persist_snapshot = _boom
        with pytest.raises(OSError):
            await mgr.apply_ops(
                s.id,
                "c1",
                0,
                [
                    {
                        "op": "annotation_created",
                        "annotation": {"kind": "note", "text": "hi"},
                    },
                ],
            )
        after = mgr.get_session(s.id)
        assert after.seq == 0
        assert after.state["annotations"] == []
        assert store.ops_since(s.id, 0) == []  # ring rolled back too
        assert await _drain(sub) == []

    async def test_atomic_rollback_preserves_prior_state(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["keep"]}]
        )
        with pytest.raises(OpError):
            await mgr.apply_ops(
                s.id,
                "c1",
                0,
                [
                    {"op": "nodes_added", "node_ids": ["x"]},
                    {
                        "op": "group_membership_changed",
                        "group_id": "missing",
                        "member_node_ids": [],
                    },
                ],
            )
        after = mgr.get_session(s.id)
        assert after.state["node_refs"] == ["keep"]
        assert after.seq == 1

    async def test_persist_runs_off_event_loop_thread(self):
        """persist() must be called from a worker thread, not the event loop thread.

        FileSessionPersistenceBackend.save does a blocking fsync; running it on
        the event loop stalls all other coroutines. asyncio.to_thread ensures it
        executes in the default ThreadPoolExecutor instead.
        """
        event_loop_thread = threading.current_thread()
        persist_threads: list[threading.Thread] = []

        store = SessionStore(InMemorySessionPersistenceBackend())
        original_persist = store.persist_snapshot

        def _capturing_persist(snapshot):
            persist_threads.append(threading.current_thread())
            original_persist(snapshot)

        store.persist_snapshot = _capturing_persist
        mgr = SessionManager(store)
        s = mgr.create_session()
        await mgr.apply_ops(s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}])

        assert len(persist_threads) == 1
        assert persist_threads[0] is not event_loop_thread

    async def test_persist_failure_via_thread_rolls_back(self):
        """A persistence error raised in the worker thread must still roll back state."""
        store = SessionStore(InMemorySessionPersistenceBackend())
        mgr = SessionManager(store)
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        def _boom(snapshot):
            raise OSError("simulated fsync failure from worker thread")

        store.persist_snapshot = _boom
        with pytest.raises(OSError):
            await mgr.apply_ops(
                s.id,
                "c1",
                0,
                [{"op": "nodes_added", "node_ids": ["x"]}],
            )
        after = mgr.get_session(s.id)
        assert after.seq == 0
        assert after.state["node_refs"] == []
        assert store.ops_since(s.id, 0) == []
        assert await _drain(sub) == []


class TestClaimOps:
    async def test_claim_ops_are_ephemeral(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        res = await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "selection_claimed", "element_ids": ["n1"]}]
        )
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
            await mgr.apply_ops(
                s.id, "c1", 0, [{"op": "nodes_added", "node_ids": [f"n{i}"]}]
            )
        cu = mgr.catch_up(s.id, 1)
        assert cu["type"] == "catch_up"
        assert [op["seq"] for op in cu["ops"]] == [2, 3]

    async def test_catch_up_falls_back_to_snapshot(self):
        store = SessionStore(InMemorySessionPersistenceBackend(), ring_size=1)
        mgr = SessionManager(store)
        s = mgr.create_session()
        for i in range(3):
            await mgr.apply_ops(
                s.id, "c1", 0, [{"op": "nodes_added", "node_ids": [f"n{i}"]}]
            )
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
        mgr = SessionManager(
            SessionStore(InMemorySessionPersistenceBackend()), event_bus=bus
        )
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "slow", "Slow")
        await _drain(sub)  # discard the connect's own presence_joined echo

        # Flood past the tiny queue without draining so the bus drops the
        # backlog and enqueues a `{"type": "resync"}` sentinel (session_hub.py).
        for i in range(5):
            bus.publish(
                s.id,
                {
                    "type": "op",
                    "op": {"op": "nodes_added", "node_ids": [f"n{i}"]},
                    "seq": i,
                },
            )

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

    async def test_resync_for_a_deleted_session_raises_session_not_found(self):
        # A session can be deleted in the narrow window between its
        # subscriber's queue overflowing and the resync translation running;
        # the stream endpoint's event_generator must catch this (rest_api.py)
        # rather than let it crash the SSE response.
        mgr = _manager()
        s = mgr.create_session()
        await mgr.delete_session(s.id)
        with pytest.raises(SessionNotFound):
            _resolve_stream_event({"type": "resync"}, mgr, s.id)


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
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "selection_claimed", "element_ids": ["n1"]}]
        )
        mgr.disconnect(s.id, "c1", sub)
        assert mgr.claims.snapshot(s.id) == {}
        assert mgr.roster(s.id) == []

    async def test_reconnect_race_old_disconnect_does_not_clobber_new_connection(self):
        """Fast reconnect: closing the old SSE must not remove presence/claims.

        Sequence:
          1. c1 connects (old SSE)           → join count = 1
          2. c1 claims n1
          3. c1 reconnects (new SSE)         → join count = 2
          4. old SSE closes                  → join count = 1 → no roster teardown
          5. Roster still shows c1; claims still intact; no presence_left emitted
          6. new SSE closes                  → join count = 0 → roster torn down
        """
        mgr = _manager()
        s = mgr.create_session()

        # Step 1 — old connection
        sub_old, _ = mgr.connect(s.id, "c1", "Alice")
        await _drain(sub_old)

        # Step 2 — c1 claims an element
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "selection_claimed", "element_ids": ["n1"]}]
        )
        await _drain(sub_old)

        # Step 3 — new connection opens before old one tears down
        sub_new, _ = mgr.connect(s.id, "c1", "Alice")
        await _drain(sub_old)
        await _drain(sub_new)

        # Step 4 — old SSE closes
        mgr.disconnect(s.id, "c1", sub_old)

        # Step 5 — roster and claims must survive; no presence_left on sub_new
        assert {m["client_id"] for m in mgr.roster(s.id)} == {"c1"}
        assert mgr.claims.snapshot(s.id) == {"n1": "c1"}
        new_events = await _drain(sub_new)
        assert not any(e.get("type") == "presence_left" for e in new_events)
        assert not any(
            e.get("op", {}).get("op") == "selection_released" for e in new_events
        )

        # Step 6 — last connection closes; now everything is torn down
        mgr.disconnect(s.id, "c1", sub_new)
        assert mgr.roster(s.id) == []
        assert mgr.claims.snapshot(s.id) == {}

    async def test_delete_broadcasts(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        assert await mgr.delete_session(s.id, deleted_by="c1") is True
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
            await mgr.apply_ops(
                s.id, "c1", 0, [{"op": "layout_applied", "positions": positions}]
            )

    async def test_small_batch_within_byte_cap_succeeds(self):
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        res = await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}]
        )
        assert res["seq"] == 1

    async def test_oversized_batch_leaves_state_untouched(self):
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "nodes_added", "node_ids": ["keep"]}]
        )
        with pytest.raises(OpBatchTooLarge):
            await mgr.apply_ops(
                s.id,
                "c1",
                0,
                [
                    {
                        "op": "layout_applied",
                        "positions": {f"n-{i}": {"x": i, "y": i} for i in range(1000)},
                    }
                ],
            )
        assert mgr.get_session(s.id).state["node_refs"] == ["keep"]


class TestTokenBucketEviction:
    """Idle keys are evicted so per-key state does not grow without bound."""

    async def test_idle_key_is_evicted_after_ttl(self):
        # t=0: key "a" makes one consume call (seeds _last["a"] = 0)
        # t=51: sweep fires (51 >= 50), eviction cutoff = 51 - 100 = -49 → "a" is NOT stale yet
        # t=102: another consume on "b" triggers sweep; cutoff = 102 - 100 = 2 > 0 → "a" evicted
        bucket = _TokenBucket(
            capacity=10.0,
            refill_per_sec=1.0,
            time_fn=iter([0, 51, 102]).__next__,
            idle_ttl=100.0,
            sweep_interval=50.0,
        )
        bucket.consume("a", 1.0)  # t=0, seeds "a"
        bucket.consume("b", 1.0)  # t=51, sweep fires; cutoff=-49, "a" not stale
        assert "a" in bucket._last

        bucket.consume("b", 1.0)  # t=102, sweep fires; cutoff=2 > 0, "a" evicted
        assert "a" not in bucket._last
        assert "a" not in bucket._tokens

    async def test_active_key_is_not_evicted(self):
        # Both "a" and "b" consume at t=0 and t=102; "a" is touched within TTL
        times = [0, 0, 80, 102]
        it = iter(times)
        bucket = _TokenBucket(
            capacity=10.0,
            refill_per_sec=1.0,
            time_fn=lambda: next(it),
            idle_ttl=100.0,
            sweep_interval=50.0,
        )
        bucket.consume("a", 1.0)  # t=0
        bucket.consume("b", 1.0)  # t=0
        bucket.consume("a", 1.0)  # t=80, refreshes "a"._last
        bucket.consume(
            "b", 1.0
        )  # t=102, sweep fires; cutoff=2; old "b" state is evicted, then recreated for the current consume call
        assert bucket._last["a"] == 80
        assert bucket._last["b"] == 102

    async def test_evicted_key_restarts_at_full_capacity(self):
        # After "a" is evicted and returns, it should start at full capacity
        # not at the partially-depleted level it had before eviction.
        times = [0, 200, 200]
        it = iter(times)
        bucket = _TokenBucket(
            capacity=10.0,
            refill_per_sec=0.0,  # no refill, so depletion is visible
            time_fn=lambda: next(it),
            idle_ttl=100.0,
            sweep_interval=50.0,
        )
        bucket.consume("a", 7.0)  # t=0: remaining = 3
        # t=200: sweep fires (200 >= 50), cutoff=100; "a" last=0 < 100 → evicted
        # then "a" is looked up with no entry → starts at capacity=10
        result = bucket.consume("a", 9.0)  # t=200: 10 - 9 = 1 left, should succeed
        assert result is True

    async def test_sweep_interval_limits_sweep_frequency(self):
        """Sweep does not fire on every call — only once per sweep_interval."""
        sweep_count = [0]
        times = [0, 10, 20, 30]  # all < sweep_interval=50
        it = iter(times)

        class CountingSweepBucket(_TokenBucket):
            def _evict(self, now):
                sweep_count[0] += 1
                super()._evict(now)

        bucket = CountingSweepBucket(
            capacity=10.0,
            refill_per_sec=1.0,
            time_fn=lambda: next(it),
            idle_ttl=100.0,
            sweep_interval=50.0,
        )
        for _ in range(4):
            bucket.consume("x", 1.0)
        # No sweep should have fired since max time elapsed (30) < sweep_interval (50)
        assert sweep_count[0] == 0


class TestRenameSession:
    """R7/R8: rename materialises an unsaved session and is a real, sequenced op."""

    async def test_rename_materialises_a_session_that_was_never_created(self):
        mgr = _manager()
        sid = "1234-5678"
        assert mgr.get_session(sid) is None
        session = await mgr.rename_session(sid, "Team map")
        assert session.name == "Team map"
        assert mgr.get_session(sid) is not None

    async def test_rename_bumps_seq_and_enters_ring_buffer(self):
        """A reconnecting client's since_seq catch-up must observe the rename,
        not just a full snapshot (R8: session_renamed was a documented but
        unreachable STATE_OP before this)."""
        mgr = _manager()
        s = mgr.create_session()
        seq_before = s.seq
        await mgr.rename_session(s.id, "Renamed", client_id="A")
        assert s.seq == seq_before + 1
        missed = mgr.store.ops_since(s.id, seq_before)
        assert missed is not None
        assert [op["op"] for op in missed] == ["session_renamed"]

    async def test_rename_broadcasts_as_a_sequenced_op(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "A", "Alice")
        await _drain(sub)
        await mgr.rename_session(s.id, "Renamed", client_id="A")
        events = await _drain(sub)
        renamed = [e for e in events if e.get("op", {}).get("op") == "session_renamed"]
        assert len(renamed) == 1
        assert renamed[0]["op"]["name"] == "Renamed"
        assert renamed[0]["seq"] == s.seq

    async def test_rename_rejects_a_full_session_store(self):
        mgr = _manager(max_sessions=0)
        with pytest.raises(SessionLimitReached):
            await mgr.rename_session("9999-9999", "x")


class TestDeleteRenameLocking:
    """R10: delete must not race an in-flight apply_ops batch for the same session."""

    async def test_delete_waits_for_inflight_persist_and_does_not_resurrect(self):
        store = SessionStore(InMemorySessionPersistenceBackend())
        mgr = SessionManager(store)
        s = mgr.create_session()
        sid = s.id

        entered = threading.Event()
        proceed = threading.Event()
        original_persist = store.persist_snapshot

        def slow_persist(snapshot):
            entered.set()
            proceed.wait(timeout=2)
            original_persist(snapshot)

        store.persist_snapshot = slow_persist

        apply_task = asyncio.create_task(
            mgr.apply_ops(sid, "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}])
        )
        # Wait (off the loop thread) until apply_ops is inside persist() —
        # it runs via asyncio.to_thread, so the loop is free to run the
        # delete task concurrently while this is blocked.
        await asyncio.to_thread(entered.wait, 2)

        delete_task = asyncio.create_task(mgr.delete_session(sid))
        await asyncio.sleep(0.05)
        assert not delete_task.done()  # blocked on the same per-session lock

        proceed.set()
        await apply_task
        assert await delete_task is True
        # The delete that ran after the batch committed must be the final
        # word — no resurrection from a stale in-flight `Session` reference
        # persisting after the delete. get_session() re-loads from the
        # backend when not cached in memory, so this proves the persistence
        # layer has nothing lingering either.
        assert mgr.get_session(sid) is None

    async def test_apply_ops_does_not_resurrect_after_concurrent_delete(self):
        """The other ordering: a delete can win the per-session lock and
        complete *between* apply_ops's pre-lock existence check and the point
        where apply_ops itself acquires the lock. The re-fetch inside the
        lock (not the outer check) is what must catch this — the outer check
        alone already passed by the time this race happens."""
        store = SessionStore(InMemorySessionPersistenceBackend())
        mgr = SessionManager(store)
        s = mgr.create_session()
        sid = s.id

        lock = mgr._lock(sid)
        await lock.acquire()
        try:
            apply_task = asyncio.create_task(
                mgr.apply_ops(sid, "c1", 0, [{"op": "nodes_added", "node_ids": ["a"]}])
            )
            await asyncio.sleep(
                0.05
            )  # let it pass the pre-lock check, then block on the lock
            assert not apply_task.done()

            # Simulate a concurrent delete_session that already ran to
            # completion while apply_ops was waiting for this same lock
            # (delete_session needs this exact lock in production; the test
            # holds it directly to control the interleaving deterministically).
            store.delete(sid)
        finally:
            lock.release()

        with pytest.raises(SessionNotFound):
            await apply_task
        assert mgr.get_session(sid) is None


class TestSessionLimitAcrossRestart:
    """R13: max_sessions must be enforced against persisted files, not just
    the in-memory map (SessionStore-level counting is covered directly in
    test_session_store.py) — a fresh SessionManager over a directory that
    already has max_sessions files must refuse to grow it further."""

    async def test_get_or_create_rejects_when_disk_already_at_cap(self, tmp_path):
        backend = FileSessionPersistenceBackend(tmp_path)
        SessionManager(SessionStore(backend), max_sessions=2).create_session()
        SessionManager(SessionStore(backend), max_sessions=2).create_session()

        # A "restarted" manager (fresh in-memory map, same directory) must see
        # the cap is already reached — this is what the unauthenticated
        # stream endpoint's get_or_create relies on to stop growth.
        restarted = SessionManager(SessionStore(backend), max_sessions=2)
        with pytest.raises(SessionLimitReached):
            restarted.get_or_create("9998-0001")


class TestApplyLayout:
    """The synchronous MCP layout write path (``apply_layout``)."""

    async def test_absolute_positions_apply_and_broadcast(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        res = mgr.apply_layout(
            s.id,
            "mcp-agent",
            positions={"a": {"x": 10, "y": 20}, "b": {"x": 30, "y": 40}},
        )
        assert res["moved"] == 2
        assert res["revision"] == s.seq == 1
        assert s.state["positions"] == {
            "a": {"x": 10.0, "y": 20.0},
            "b": {"x": 30.0, "y": 40.0},
        }
        events = await _drain(sub)
        assert len(events) == 1
        assert events[0]["op"]["op"] == "layout_applied"
        assert events[0]["seq"] == 1

    async def test_deltas_resolve_against_current_positions(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.apply_layout(s.id, "mcp-agent", positions={"a": {"x": 100, "y": 100}})
        mgr.apply_layout(s.id, "mcp-agent", deltas={"a": {"dx": 5, "dy": -10}})
        assert s.state["positions"]["a"] == {"x": 105.0, "y": 90.0}

    async def test_delta_for_unknown_node_starts_at_origin(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.apply_layout(s.id, "mcp-agent", deltas={"ghost": {"dx": 7, "dy": 8}})
        assert s.state["positions"]["ghost"] == {"x": 7.0, "y": 8.0}

    async def test_requires_exactly_one_of_positions_or_deltas(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            mgr.apply_layout(s.id, "mcp-agent")
        with pytest.raises(OpError):
            mgr.apply_layout(
                s.id,
                "mcp-agent",
                positions={"a": {"x": 1, "y": 1}},
                deltas={"a": {"dx": 1, "dy": 1}},
            )

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.apply_layout(s.id, "mcp-agent", positions={"a": {"x": 1, "y": 1}})
        with pytest.raises(RevisionConflict) as exc:
            mgr.apply_layout(
                s.id,
                "mcp-agent",
                positions={"a": {"x": 2, "y": 2}},
                expected_revision=0,
            )
        assert exc.value.expected == 0
        assert exc.value.actual == 1
        # The rejected write left the position untouched.
        assert s.state["positions"]["a"] == {"x": 1.0, "y": 1.0}

    async def test_matching_expected_revision_applies(self):
        mgr = _manager()
        s = mgr.create_session()
        res = mgr.apply_layout(
            s.id, "mcp-agent", positions={"a": {"x": 9, "y": 9}}, expected_revision=0
        )
        assert res["revision"] == 1

    async def test_busy_when_session_lock_held(self):
        mgr = _manager()
        s = mgr.create_session()
        async with mgr._lock(s.id):
            with pytest.raises(LayoutBusy):
                mgr.apply_layout(s.id, "mcp-agent", positions={"a": {"x": 1, "y": 1}})

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.apply_layout(
                "9999-9999", "mcp-agent", positions={"a": {"x": 1, "y": 1}}
            )

    async def test_invalid_position_rolls_back(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.apply_layout(s.id, "mcp-agent", positions={"a": {"x": 1, "y": 1}})
        seq_before = s.seq
        with pytest.raises(OpError):
            mgr.apply_layout(s.id, "mcp-agent", positions={"b": {"x": "nope", "y": 0}})
        assert s.seq == seq_before
        assert "b" not in s.state["positions"]

    async def test_persist_failure_rolls_back_and_does_not_broadcast(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        seq_before = s.seq

        def boom(_session):
            raise IOError("disk full")

        mgr.store.persist = boom
        with pytest.raises(IOError):
            mgr.apply_layout(s.id, "mcp-agent", positions={"a": {"x": 1, "y": 1}})
        assert s.seq == seq_before
        assert "a" not in s.state["positions"]
        assert await _drain(sub) == []

    async def test_animation_hint_rides_the_broadcast(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        mgr.apply_layout(
            s.id,
            "mcp-agent",
            positions={"a": {"x": 1, "y": 1}},
            animation={"animate": True, "duration_ms": 250, "easing": "linear"},
        )
        events = await _drain(sub)
        assert events[0]["op"]["animation"] == {
            "animate": True,
            "duration_ms": 250,
            "easing": "linear",
        }

    async def test_batch_too_large_by_count(self):
        mgr = _manager(max_ops_per_batch=2)
        s = mgr.create_session()
        with pytest.raises(OpBatchTooLarge):
            mgr.apply_layout(
                s.id,
                "mcp-agent",
                positions={
                    "a": {"x": 1, "y": 1},
                    "b": {"x": 2, "y": 2},
                    "c": {"x": 3, "y": 3},
                },
            )

    async def test_refuses_during_real_inflight_batch_and_preserves_seq_order(self):
        """The scenario the design exists for: a layout write attempted while an
        apply_ops batch is genuinely mid-persist (lock held across the to_thread
        await) must refuse — and the batch's lower-seq ops must still broadcast in
        order afterwards, never dropped by the client seq-gate."""
        store = SessionStore(InMemorySessionPersistenceBackend())
        mgr = SessionManager(store)
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        entered = threading.Event()
        proceed = threading.Event()
        original = store.persist_snapshot

        def slow_persist(snapshot):
            entered.set()
            proceed.wait(timeout=2)
            original(snapshot)

        store.persist_snapshot = slow_persist
        apply_task = asyncio.create_task(
            mgr.apply_ops(
                s.id,
                "c1",
                0,
                [
                    {"op": "nodes_added", "node_ids": ["a"]},
                    {"op": "node_moved", "node_id": "a", "position": {"x": 1, "y": 1}},
                ],
            )
        )
        # Wait off the loop thread until apply_ops is inside persist with the
        # per-session lock held.
        await asyncio.to_thread(entered.wait, 2)

        with pytest.raises(LayoutBusy):
            mgr.apply_layout(s.id, "mcp-agent", positions={"a": {"x": 9, "y": 9}})

        proceed.set()
        await apply_task
        events = await _drain(sub)
        assert [e["op"]["op"] for e in events] == ["nodes_added", "node_moved"]
        assert [e["seq"] for e in events] == [1, 2]
        # The refused layout write left no trace.
        assert s.state["positions"]["a"] == {"x": 1.0, "y": 1.0}


class TestAddNodeRefs:
    """The synchronous MCP session-population path (``add_node_refs``)."""

    async def test_nodes_enter_state_and_broadcast(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        res = mgr.add_node_refs(s.id, "mcp-agent", ["a", "b"])

        assert res["added"] == ["a", "b"]
        assert res["node_count"] == 2
        assert res["revision"] == s.seq == 1
        assert s.state["node_refs"] == ["a", "b"]
        events = await _drain(sub)
        assert [e["op"]["op"] for e in events] == ["nodes_added"]
        assert events[0]["op"]["node_ids"] == ["a", "b"]

    async def test_adding_nothing_new_is_a_silent_no_op(self):
        """A repeat add must not bump the revision every collaborator tracks."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.add_node_refs(s.id, "mcp-agent", ["a"])
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        seq_before = s.seq

        res = mgr.add_node_refs(s.id, "mcp-agent", ["a"])

        assert res["added"] == []
        assert res["revision"] == seq_before == s.seq
        assert await _drain(sub) == []

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.add_node_refs(s.id, "mcp-agent", ["a"])

        with pytest.raises(RevisionConflict):
            mgr.add_node_refs(s.id, "mcp-agent", ["b"], expected_revision=0)
        assert s.state["node_refs"] == ["a"]

    async def test_empty_node_ids_raises(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            mgr.add_node_refs(s.id, "mcp-agent", [])

    async def test_batch_too_large_by_count(self):
        mgr = _manager(max_ops_per_batch=2)
        s = mgr.create_session()
        with pytest.raises(OpBatchTooLarge):
            mgr.add_node_refs(s.id, "mcp-agent", ["a", "b", "c"])

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.add_node_refs("9999-9999", "mcp-agent", ["a"])

    async def test_busy_when_session_lock_held(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr._lock(s.id).acquire()
        with pytest.raises(LayoutBusy):
            mgr.add_node_refs(s.id, "mcp-agent", ["a"])

    async def test_persist_failure_rolls_back_and_does_not_broadcast(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)
        seq_before = s.seq

        def boom(_session):
            raise IOError("disk full")

        mgr.store.persist = boom
        with pytest.raises(IOError):
            mgr.add_node_refs(s.id, "mcp-agent", ["a"])
        assert s.seq == seq_before
        assert s.state["node_refs"] == []
        assert await _drain(sub) == []
        # The ring must not keep the rolled-back op: the next write would reuse
        # its seq, and a reconnecting client would replay a phantom add.
        assert list(mgr.store.ring(s.id)) == []
        assert mgr.catch_up(s.id, seq_before)["ops"] == []

    async def test_repeated_ids_are_added_once(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        res = mgr.add_node_refs(s.id, "mcp-agent", ["a", "a", "b"])

        assert res["added"] == ["a", "b"]
        assert res["node_count"] == 2
        assert s.state["node_refs"] == ["a", "b"]
        events = await _drain(sub)
        assert events[0]["op"]["node_ids"] == ["a", "b"]


class TestUpsertAnnotation:
    """The synchronous MCP annotation-create/upsert write path (``upsert_annotation``)."""

    async def test_creates_and_broadcasts(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        res = mgr.upsert_annotation(
            s.id, "mcp-agent", {"id": "note-1", "type": "note", "text": "hi"}
        )

        assert res["revision"] == s.seq == 1
        assert res["annotation"]["id"] == "note-1"
        assert s.state["annotations"][0]["text"] == "hi"
        events = await _drain(sub)
        assert events[0]["op"]["op"] == "annotation_created"
        assert events[0]["seq"] == 1

    async def test_omitted_id_is_assigned_by_the_store(self):
        mgr = _manager()
        s = mgr.create_session()
        res = mgr.upsert_annotation(s.id, "mcp-agent", {"type": "note", "text": "hi"})
        assert isinstance(res["annotation"]["id"], str) and res["annotation"]["id"]

    async def test_matching_id_upserts_in_place(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(
            s.id, "mcp-agent", {"id": "note-1", "type": "note", "text": "v1"}
        )
        res = mgr.upsert_annotation(
            s.id, "mcp-agent", {"id": "note-1", "type": "note", "text": "v2"}
        )
        assert len(s.state["annotations"]) == 1
        assert s.state["annotations"][0]["text"] == "v2"
        assert res["revision"] == 2

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "n1", "type": "note"})
        with pytest.raises(RevisionConflict) as exc:
            mgr.upsert_annotation(
                s.id, "mcp-agent", {"id": "n2", "type": "note"}, expected_revision=0
            )
        assert exc.value.expected == 0 and exc.value.actual == 1
        assert len(s.state["annotations"]) == 1

    async def test_recreate_of_recently_deleted_id_raises(self):
        """A create-op retry for an id another collaborator just deleted must not
        resurrect it (D-table rule mirrored from SessionStore)."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        mgr.delete_annotation(s.id, "mcp-agent", "note-1")
        with pytest.raises(AnnotationRecentlyDeleted):
            mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})

    async def test_busy_when_session_lock_held(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr._lock(s.id).acquire()
        with pytest.raises(LayoutBusy):
            mgr.upsert_annotation(s.id, "mcp-agent", {"type": "note"})

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.upsert_annotation("9999-9999", "mcp-agent", {"type": "note"})

    async def test_invalid_type_rolls_back(self):
        mgr = _manager()
        s = mgr.create_session()
        seq_before = s.seq
        with pytest.raises(OpError):
            mgr.upsert_annotation(s.id, "mcp-agent", {"type": "not-a-real-type"})
        assert s.seq == seq_before
        assert s.state["annotations"] == []

    async def test_upsert_by_id_rejects_type_change(self):
        """The store's upsert-by-id path (annotation_created with an existing
        id) must not silently retype an annotation — not even through the
        synchronous MCP write path, which bypasses any tool-layer pre-check."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "ann-1", "type": "line"})
        seq_before = s.seq
        with pytest.raises(OpError):
            mgr.upsert_annotation(s.id, "mcp-agent", {"id": "ann-1", "type": "shape"})
        assert s.seq == seq_before
        assert s.state["annotations"][0]["type"] == "line"


class TestUpdateAnnotation:
    """The synchronous MCP annotation-update write path (``update_annotation``)."""

    async def test_patch_merges_onto_existing(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(
            s.id,
            "mcp-agent",
            {"id": "note-1", "type": "note", "text": "v1", "color": "red"},
        )
        res = mgr.update_annotation(
            s.id, "mcp-agent", {"id": "note-1", "type": "note", "text": "v2"}
        )
        stored = s.state["annotations"][0]
        assert stored["text"] == "v2"
        assert stored["color"] == "red"  # untouched field survives the shallow merge
        assert res["annotation"]["text"] == "v2"

    async def test_missing_annotation_raises_not_found(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(AnnotationNotFound):
            mgr.update_annotation(s.id, "mcp-agent", {"id": "ghost", "type": "note"})

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        with pytest.raises(RevisionConflict):
            mgr.update_annotation(
                s.id,
                "mcp-agent",
                {"id": "note-1", "type": "note", "text": "x"},
                expected_revision=0,
            )

    async def test_requires_string_id_in_patch(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            mgr.update_annotation(s.id, "mcp-agent", {"type": "note"})

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.update_annotation(
                "9999-9999", "mcp-agent", {"id": "note-1", "type": "note"}
            )

    async def test_rejects_type_change(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "ann-1", "type": "line"})
        seq_before = s.seq
        with pytest.raises(OpError):
            mgr.update_annotation(s.id, "mcp-agent", {"id": "ann-1", "type": "shape"})
        assert s.seq == seq_before
        assert s.state["annotations"][0]["type"] == "line"


class TestDeleteAnnotation:
    """The synchronous MCP annotation-delete write path (``delete_annotation``)."""

    async def test_deletes_and_broadcasts(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        res = mgr.delete_annotation(s.id, "mcp-agent", "note-1")

        assert s.state["annotations"] == []
        assert res["revision"] == s.seq
        events = await _drain(sub)
        assert events[0]["op"]["op"] == "annotation_deleted"

    async def test_missing_annotation_raises_not_found(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(AnnotationNotFound):
            mgr.delete_annotation(s.id, "mcp-agent", "ghost")

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        with pytest.raises(RevisionConflict):
            mgr.delete_annotation(s.id, "mcp-agent", "note-1", expected_revision=0)
        # The rejected delete left the annotation in place.
        assert len(s.state["annotations"]) == 1

    async def test_busy_when_session_lock_held(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        await mgr._lock(s.id).acquire()
        with pytest.raises(LayoutBusy):
            mgr.delete_annotation(s.id, "mcp-agent", "note-1")

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.delete_annotation("9999-9999", "mcp-agent", "note-1")
