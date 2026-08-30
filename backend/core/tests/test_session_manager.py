"""
Tests for the op-protocol orchestration (step 3) in
``backend/core/session_manager.py``: ordered apply + broadcast, ephemeral
claim ops, catch-up vs snapshot, rate limiting, batch caps, and the
presence/claim lifecycle on connect/disconnect.
"""

import asyncio
import json
import threading

import pytest

from backend.core import image_ingest
from backend.core.session_annotations import build_annotation, build_annotation_patch
from backend.core.session_hub import InProcessEventBus, LeaseMap
from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    OpError,
    SessionStore,
)
from backend.core.session_manager import (
    AnnotationNotFound,
    AnnotationRecentlyDeleted,
    ImageBudgetExceeded,
    LayoutBusy,
    LeaseConflict,
    NoUndoableAction,
    OpBatchTooLarge,
    RateLimited,
    RevisionConflict,
    SessionLimitReached,
    SessionManager,
    SessionNotFound,
    UndoConflict,
    _TokenBucket,
    _UNDO_REPLAY_CLIENT_ID,
)
from backend.service.rest_api import _resolve_stream_event

pytestmark = pytest.mark.asyncio


def _manager(**kwargs) -> SessionManager:
    return SessionManager(SessionStore(InMemorySessionPersistenceBackend()), **kwargs)


def _image_annotation(annotation_id: str, *, data_bytes: int) -> dict:
    """A raw `image` annotation dict with an embedded data URI of exactly
    ``data_bytes`` decoded bytes (padding-free, so the approximation in
    ``data_url_byte_length`` is exact)."""
    import base64

    encoded = base64.b64encode(b"x" * data_bytes).decode("ascii")
    return {
        "id": annotation_id,
        "type": "image",
        "image": {"url": f"data:image/webp;base64,{encoded}"},
    }


async def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def _kind_annotation(kind: str, ann_id: str, *, variant: str = "a") -> dict:
    """A minimal, valid v1 annotation dict of *kind* for a raw op.

    Accepts the six v1 annotation kinds beyond note/label/group —
    ``text``/``shape``/``icon``/``vote_dot``/``image``/``freehand`` —
    task-annotation-shared-session-realtime's remaining_scope per-kind
    reconnect/catch-up/duplicate-suppression/lock-ownership audit
    (``TestPerKindReconnectCatchUpAndLocks`` below). ``frame`` is excluded: it
    no longer exists as a separate kind (merged into ``shape``, PR #521); note/
    label/group are excluded too — ``GraphCanvasRemote.test.jsx`` already
    covers those three at the canvas-application layer.

    Per-kind payload fields live at the annotation's own top level for a raw
    op (mirroring what ``build_annotation``'s ``content`` merges onto the
    annotation, and what the browser's own translators read/write — see
    ``GENERIC_OVERLAY_FIELDS`` in ``packages/ui-graph-canvas/src/utils/
    annotations.js``), not nested under a ``content`` key — matching every
    existing raw-op test in this module (e.g. a note's top-level ``text``).

    ``variant`` gives a second, distinguishable value for the same kind and
    id, for cross-write conflict tests that need to tell "the first write"
    and "the second write" apart in the stored result.
    """
    if kind == "image":
        # image is exempt from _kind_annotation's variant knob: an ingested
        # image's pixel content is the one field session_store's
        # `_require_ingested_image` actually checks, so varying anything
        # else is enough to distinguish two writes without touching it.
        ann = _image_annotation(ann_id, data_bytes=64)
        ann["alt"] = "a" if variant == "a" else "b"
        return ann
    ann: dict = {"id": ann_id, "type": kind}
    if kind == "text":
        ann["text"] = "hi" if variant == "a" else "bye"
    elif kind == "shape":
        ann["shape"] = "rectangle"
        ann["text"] = "hi" if variant == "a" else "bye"
    elif kind == "icon":
        ann["icon"] = "flag" if variant == "a" else "star"
    elif kind == "vote_dot":
        ann["color"] = "#ef4444" if variant == "a" else "#22c55e"
    elif kind == "freehand":
        ann["points"] = [{"x": 0, "y": 0}, {"x": 10, "y": 10 if variant == "a" else 20}]
    else:
        raise ValueError(f"unhandled kind for _kind_annotation: {kind!r}")
    return ann


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


class TestSelectionNeverAcquiresLease:
    """Core guarantee of ``dec-mcp-agent-ops-vs-annotation-claimmap``: selecting
    (``selection_claimed``) is a purely cosmetic presence marker and must never
    acquire, renew or block on an edit lease — only an explicit
    ``edit_lease_acquired`` does. ``TestLeaseEnforcement``/
    ``TestConflictMatrixTwoClients`` below prove the lease side; this class
    proves selection stays outside it entirely.
    """

    async def _seeded(self, mgr, ann_id="note-1"):
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": {"id": ann_id, "type": "note"},
                }
            ],
        )
        return s

    async def test_selecting_does_not_block_another_clients_write(self):
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "selection_claimed", "element_ids": ["note-1"]}]
        )
        # c1 merely selected note-1 — c2 can still edit it freely.
        await mgr.apply_ops(
            s.id,
            "c2",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "note-1", "type": "note", "text": "c2 wrote"},
                }
            ],
        )
        assert s.state["annotations"][0]["text"] == "c2 wrote"
        # No lease was ever created by the selection.
        assert mgr.leases.snapshot(s.id) == {}

    async def test_selecting_someone_elses_leased_annotation_does_not_steal_it(self):
        """The exact bug this task closes: selection used to feed the same
        LWW map a write was checked against, so selecting an annotation
        someone else was editing silently took the lock away from them."""
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        # c2 selects the same annotation c1 is actively editing...
        await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "selection_claimed", "element_ids": ["note-1"]}]
        )
        # ...c1's lease is completely unaffected by c2's selection.
        assert mgr.leases.snapshot(s.id) == {"note-1": "c1"}
        with pytest.raises(LeaseConflict):
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "note-1",
                            "type": "note",
                            "text": "c2 hijacked via selection",
                        },
                    }
                ],
            )

    async def test_undo_is_unaffected_by_a_live_selection_claim(self):
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "note-1", "type": "note", "text": "mine"},
                }
            ],
        )
        await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "selection_claimed", "element_ids": ["note-1"]}]
        )
        # c2 merely selected — c1's undo of its own edit is not blocked.
        result = mgr.undo_last_action(s.id, "c1")
        assert result["undone_op"] == "annotation_updated"


class TestLeaseAcquisition:
    """First-actual-editor-wins acquisition semantics for ``edit_lease_acquired``/
    ``edit_lease_released`` themselves (task-annotation-exclusive-edit-leases):
    atomic grant/deny, renewal, expiry and disconnect release. Whether a live
    lease then blocks a *different* client's mutation is
    ``TestLeaseEnforcement``'s concern; this class is about acquiring the
    lease in the first place.
    """

    async def test_first_acquirer_is_granted(self):
        mgr = _manager()
        s = mgr.create_session()
        res = await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        applied = res["applied"][0]
        assert applied["element_ids"] == ["note-1"]
        assert applied["denied"] == {}
        assert mgr.leases.snapshot(s.id) == {"note-1": "c1"}

    async def test_second_acquirer_is_denied_not_given_a_takeover(self):
        """The exact first-holder-wins property the old ClaimMap.claim() (LWW)
        never had: a second client's acquisition attempt is refused, and the
        first client's lease is completely unaffected by the attempt."""
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        res = await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        applied = res["applied"][0]
        assert applied["element_ids"] == []
        assert applied["denied"] == {"note-1": "c1"}
        # c1's lease still stands, untouched by c2's refused attempt.
        assert mgr.leases.snapshot(s.id) == {"note-1": "c1"}

    async def test_two_concurrent_acquisitions_admit_only_one_editor(self):
        """The atomicity guarantee behind first-holder-wins: apply_ops holds
        the per-session asyncio.Lock across both the pre-batch lease snapshot
        and the post-batch commit (see apply_ops' own comments), so two
        `edit_lease_acquired` batches racing for the same never-before-held
        annotation cannot both be granted — whichever enters the critical
        section first wins, and the second sees it as already held. Run with
        real concurrency (asyncio.gather, not sequential awaits) so a
        would-be race is actually exercised rather than assumed serial."""
        mgr = _manager()
        s = mgr.create_session()
        results = await asyncio.gather(
            mgr.apply_ops(
                s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
            ),
            mgr.apply_ops(
                s.id, "c2", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
            ),
        )
        granted_by = [
            (client, r["applied"][0]["element_ids"])
            for client, r in zip(("c1", "c2"), results)
        ]
        winners = [client for client, ids in granted_by if ids == ["note-1"]]
        losers = [client for client, ids in granted_by if ids == []]
        assert len(winners) == 1
        assert len(losers) == 1
        # The map agrees with whichever one actually won.
        assert mgr.leases.snapshot(s.id) == {"note-1": winners[0]}

    async def test_renewal_by_the_same_holder_extends_the_ttl(self):
        clock = {"t": 0.0}
        mgr = SessionManager(
            SessionStore(InMemorySessionPersistenceBackend()),
            leases=LeaseMap(ttl=30.0, time_fn=lambda: clock["t"]),
        )
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        clock["t"] += 25.0  # short of the 30s TTL, but a slow typist would be close
        res = await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        assert res["applied"][0]["element_ids"] == ["note-1"]
        clock["t"] += 25.0  # 50s since acquire, but only 25s since the renewal
        # Still held by c1 — the renewal reset the TTL, so this is not expired.
        res = await mgr.apply_ops(
            s.id,
            "c2",
            0,
            [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}],
        )
        assert res["applied"][0]["denied"] == {"note-1": "c1"}

    async def test_expired_lease_can_be_acquired_by_someone_else(self):
        clock = {"t": 0.0}
        mgr = SessionManager(
            SessionStore(InMemorySessionPersistenceBackend()),
            leases=LeaseMap(ttl=30.0, time_fn=lambda: clock["t"]),
        )
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        clock["t"] += 31.0  # past the TTL with no renewal
        res = await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        assert res["applied"][0]["element_ids"] == ["note-1"]
        assert mgr.leases.snapshot(s.id) == {"note-1": "c2"}

    async def test_explicit_release_reopens_acquisition_immediately(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_released", "element_ids": ["note-1"]}]
        )
        res = await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        assert res["applied"][0]["element_ids"] == ["note-1"]

    async def test_disconnect_releases_the_departing_clients_leases(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        mgr.disconnect(s.id, "c1", sub)
        assert mgr.leases.snapshot(s.id) == {}
        res = await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        assert res["applied"][0]["element_ids"] == ["note-1"]

    async def test_lease_acquire_requires_element_ids(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            await mgr.apply_ops(s.id, "c1", 0, [{"op": "edit_lease_acquired"}])


class TestLeaseEnforcement:
    """Server-side rejection of a browser (``apply_ops``) write to an
    annotation another client holds a live edit lease on
    (task-annotation-exclusive-edit-leases' server-side-lease-enforcement
    slice, superseding the old advisory-ClaimMap-based enforcement). Covers
    both browser write paths — the ``apply_ops`` batch and
    ``undo_last_action`` — while the last test in this class documents that
    the synchronous MCP write path is deliberately left unaffected pending
    ``task-mcp-annotation-human-edit-guard``.
    """

    async def _seeded(self, mgr, ann_id="note-1"):
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": {"id": ann_id, "type": "note"},
                }
            ],
        )
        return s

    async def test_non_holder_update_is_rejected(self):
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        with pytest.raises(LeaseConflict) as exc_info:
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "note-1",
                            "type": "note",
                            "text": "hijacked",
                        },
                    }
                ],
            )
        assert exc_info.value.annotation_id == "note-1"
        assert exc_info.value.held_by == "c1"
        # rejected, so the state never actually changed
        assert s.state["annotations"][0].get("text") != "hijacked"

    async def test_undo_of_own_action_is_rejected_while_another_client_holds_it(self):
        """Actor-scoping is not a substitute for the lease check.

        Undo reverts the caller's *own* past action, but the annotation it
        touches can be under someone else's live edit lease by the time the
        undo runs — which is exactly the window this closes.
        """
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "note-1", "type": "note", "text": "mine"},
                }
            ],
        )
        await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        with pytest.raises(LeaseConflict) as exc_info:
            mgr.undo_last_action(s.id, "c1")
        assert exc_info.value.annotation_id == "note-1"
        assert exc_info.value.held_by == "c2"
        # Refused before anything was touched: the edit stands and the record
        # is still undoable once the lease clears.
        assert s.state["annotations"][0]["text"] == "mine"
        assert any(not r.get("undone") for r in s.activity_log)

    async def test_undo_is_allowed_while_the_caller_holds_the_lease(self):
        mgr = _manager()
        s = mgr.create_session()
        # Seeded WITH text, so undoing an edit to it demonstrably restores the
        # old value. `annotation_updated` merges rather than replaces, so
        # undoing an edit that *added* a field would leave the field in place —
        # a separate fidelity question, deliberately not what this asserts.
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": {"id": "note-1", "type": "note", "text": "original"},
                }
            ],
        )
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "note-1", "type": "note", "text": "mine"},
                }
            ],
        )
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        result = mgr.undo_last_action(s.id, "c1")
        assert result["undone_op"] == "annotation_updated"
        assert s.state["annotations"][0]["text"] == "original"

    async def test_undo_is_allowed_when_nobody_holds_the_lease(self):
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "note-1", "type": "note", "text": "mine"},
                }
            ],
        )
        result = mgr.undo_last_action(s.id, "c1")
        assert result["undone_op"] == "annotation_updated"

    async def test_undo_of_a_delete_proceeds_since_the_annotation_is_gone(self):
        """The inverse op is an ``annotation_created`` for an id no longer in
        state, so ``_claimed_annotation_target`` reports no target and a stale
        lease on the deleted id cannot block the restore."""
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "annotation_deleted", "annotation_id": "note-1"}]
        )
        await mgr.apply_ops(
            s.id, "c2", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        result = mgr.undo_last_action(s.id, "c1")
        assert result["undone_op"] == "annotation_deleted"

    async def test_lease_conflict_message_matches_the_ui_classifier(self):
        """The browser tells the retryable lease 409 apart from the permanent
        "state changed since" 409 by matching this substring — see
        ``classifyUndoError`` in frontend/web/src/utils/sessionActivity.js.
        Nothing else carries the distinction over the wire (both are a bare
        409 with a prose ``detail``), so reword the message and the UI silently
        starts telling users a retryable refusal is permanent."""
        assert "is being edited by another client" in str(
            LeaseConflict("note-1", "c2")
        )

    async def test_non_holder_delete_is_rejected(self):
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        with pytest.raises(LeaseConflict):
            await mgr.apply_ops(
                s.id, "c2", 0, [{"op": "annotation_deleted", "annotation_id": "note-1"}]
            )
        assert len(s.state["annotations"]) == 1

    async def test_lease_holders_own_write_still_succeeds(self):
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "note-1", "type": "note", "text": "mine"},
                }
            ],
        )
        assert s.state["annotations"][0]["text"] == "mine"

    async def test_a_new_annotation_id_has_no_lease_to_protect(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        # "note-1" is leased but does not exist yet — creating it fresh is not
        # a mutation of anything the lease holder has, so it is not blocked.
        await mgr.apply_ops(
            s.id,
            "c2",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": {"id": "note-1", "type": "note"},
                }
            ],
        )
        assert s.state["annotations"][0]["id"] == "note-1"

    async def test_release_reopens_the_write_path(self):
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_released", "element_ids": ["note-1"]}]
        )
        await mgr.apply_ops(
            s.id,
            "c2",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "note-1", "type": "note", "text": "now free"},
                }
            ],
        )
        assert s.state["annotations"][0]["text"] == "now free"

    async def test_expired_lease_reopens_the_write_path(self):
        clock = {"t": 0.0}
        mgr = SessionManager(
            SessionStore(InMemorySessionPersistenceBackend()),
            leases=LeaseMap(ttl=30.0, time_fn=lambda: clock["t"]),
        )
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        clock["t"] += 31.0  # past the 30s TTL
        await mgr.apply_ops(
            s.id,
            "c2",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "note-1",
                        "type": "note",
                        "text": "expired lease",
                    },
                }
            ],
        )
        assert s.state["annotations"][0]["text"] == "expired lease"

    async def test_a_conflicting_op_rolls_back_the_whole_batch(self):
        """apply_ops is documented all-or-nothing: an earlier op in the same
        batch as a rejected one must not have taken effect either."""
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        with pytest.raises(LeaseConflict):
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [
                    {"op": "nodes_added", "node_ids": ["n1"]},
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "note-1",
                            "type": "note",
                            "text": "hijacked",
                        },
                    },
                ],
            )
        assert s.state["node_refs"] == []

    async def test_mcp_write_path_is_unaffected_by_a_live_lease(self):
        """Deliberately unenforced in v1 — see LeaseConflict's docstring and
        dec-mcp-agent-ops-vs-annotation-claimmap.

        upsert_annotation/update_annotation/delete_annotation are the
        synchronous MCP tool write path (mcp_tools.py, all keyed to the
        shared 'mcp-agent' client id) and never go through apply_ops, so
        they are not checked against LeaseMap at all — an MCP write still
        silently overrides a live human edit lease. Covering that is
        task-mcp-annotation-human-edit-guard's separate, deliberately-
        sequenced-after scope; this test documents and pins the current
        (unaffected) behaviour rather than guessing at it.
        """
        mgr = _manager()
        s = await self._seeded(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["note-1"]}]
        )
        result = mgr.update_annotation(
            s.id,
            "mcp-agent",
            {"id": "note-1", "type": "note", "text": "agent wrote anyway"},
        )
        assert result["annotation"]["text"] == "agent wrote anyway"


class TestConflictMatrixTwoClients:
    """Two-*real*-client conflict matrix for docs/ANNOTATION_CONTRACT.md's new
    "Two-client conflict matrix" section (task-annotation-shared-session-
    realtime remaining_scope item 1: document + test what happens when two
    clients edit the same annotation at close to the same time, broken out by
    same-field vs different-field edits, claim-holder vs non-holder, and
    mutation category).

    Every test here drives ``SessionManager.apply_ops`` with two distinct
    ``client_id``s against the same session — the real batch-apply/lease-
    check/persist path a browser goes through — not just a call into the
    ``LeaseMap`` helper directly the way a narrower unit test would.
    ``TestLeaseEnforcement`` above already proves the lease-conflict half of
    this for ``note``; this class adds the cross-field-clobber half the prior
    slice's tests never exercised, plus one representative case per other
    mutation category (style, lock) two clients can race on. "no claim held"
    below now means "no lease held" — a lease closes the collision window
    these clobber cases fall into; it does not itself add field-level
    granularity (that is dec-annotation-field-patches-and-conflicts' separate,
    still-open scope), so the *held* cells below prove the write is refused
    outright rather than partially merged.
    """

    async def _seeded_shape(self, mgr, ann_id="shape-1"):
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": {
                        "id": ann_id,
                        "type": "shape",
                        "shape": "rectangle",
                        "text": "orig",
                        "geometry": {"x": 0, "y": 0, "w": 160, "h": 96},
                        "style": {"fill": "#94a3b8"},
                    },
                }
            ],
        )
        return s

    # --- Same-field, no lease held: whole-annotation resend is last-write-wins ---
    async def test_same_field_text_edits_without_a_lease_second_writer_wins(self):
        mgr = _manager()
        s = await self._seeded_shape(mgr)
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "shape-1",
                        "type": "shape",
                        "text": "A's edit",
                    },
                }
            ],
        )
        await mgr.apply_ops(
            s.id,
            "c2",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "shape-1",
                        "type": "shape",
                        "text": "B's edit",
                    },
                }
            ],
        )
        stored = next(a for a in s.state["annotations"] if a["id"] == "shape-1")
        assert stored["text"] == "B's edit"

    # --- Different-field, no lease held: a documented finding, not a designed
    # guarantee — see the new ANNOTATION_CONTRACT.md section this test backs.
    # apply_state_op's `annotation_updated` handler does target.update(incoming)
    # (session_store.py), and the browser always resends the WHOLE annotation
    # object on every publish (sessionSyncClient.js's diffState/syncState), not
    # a per-field delta — so a client whose own local copy has not yet caught
    # up on a concurrent peer's text edit clobbers that edit as a side effect
    # of publishing its own, unrelated geometry change, because its outgoing
    # "geometry update" is really a whole-annotation resend carrying its own
    # (stale) copy of every other field too.
    async def test_geometry_edit_from_a_stale_client_clobbers_a_concurrent_text_edit(
        self,
    ):
        mgr = _manager()
        s = await self._seeded_shape(mgr)
        # A publishes a text-only change...
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "shape-1",
                        "type": "shape",
                        "text": "A's edit",
                    },
                }
            ],
        )
        # ...but B's outgoing "geometry" op is really its own whole local copy
        # of the annotation, captured before B's client received A's update
        # (in-flight SSE, or B simply committed a fraction of a second sooner
        # than A's edit reached it) — so it still carries the pre-edit "orig"
        # text alongside its own new geometry.
        await mgr.apply_ops(
            s.id,
            "c2",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "shape-1",
                        "type": "shape",
                        "text": "orig",
                        "geometry": {"x": 40, "y": 40, "w": 160, "h": 96},
                    },
                }
            ],
        )
        stored = next(a for a in s.state["annotations"] if a["id"] == "shape-1")
        assert stored["geometry"] == {"x": 40, "y": 40, "w": 160, "h": 96}
        # A's text edit is gone — clobbered by B's stale resend, not merged
        # around it. This is the whole-document-LWW property documented in
        # ANNOTATION_CONTRACT.md's "Two-client conflict matrix" section.
        assert stored["text"] == "orig"

    # --- A lease blocks a DIFFERENT-field edit too, not only a same-field one:
    # the lease check is on the annotation id, not on which fields an op
    # touches, so it also closes the clobber case above whenever the second
    # writer is the one refused rather than racing in silently.
    async def test_lease_blocks_a_different_field_edit_too_not_only_same_field(self):
        mgr = _manager()
        s = await self._seeded_shape(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["shape-1"]}]
        )
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "shape-1",
                        "type": "shape",
                        "text": "A's edit",
                    },
                }
            ],
        )
        with pytest.raises(LeaseConflict) as exc_info:
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "shape-1",
                            "type": "shape",
                            "geometry": {"x": 40, "y": 40, "w": 160, "h": 96},
                        },
                    }
                ],
            )
        assert exc_info.value.annotation_id == "shape-1"
        stored = next(a for a in s.state["annotations"] if a["id"] == "shape-1")
        # Refused, so neither the geometry write nor any clobber of A's text
        # took effect — this is the safe cell of the matrix: holding the lease
        # protects every field, not only the one the holder itself is editing.
        assert stored["text"] == "A's edit"
        assert stored["geometry"] == {"x": 0, "y": 0, "w": 160, "h": 96}

    # --- style category, leased: a non-holder's style-only write is refused
    # the same way a text or geometry write is (the check has no per-category
    # special case).
    async def test_non_holder_style_edit_is_rejected_while_leased(self):
        mgr = _manager()
        s = await self._seeded_shape(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["shape-1"]}]
        )
        with pytest.raises(LeaseConflict):
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "shape-1",
                            "type": "shape",
                            "style": {"fill": "#ef4444"},
                        },
                    }
                ],
            )
        stored = next(a for a in s.state["annotations"] if a["id"] == "shape-1")
        assert stored["style"] == {"fill": "#94a3b8"}

    # --- lock category, leased: a non-holder's lock toggle is an
    # annotation_updated like any other write, so it is refused too — "locked"
    # is not a separate op type with its own bypass.
    async def test_non_holder_lock_toggle_is_rejected_while_leased(self):
        mgr = _manager()
        s = await self._seeded_shape(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["shape-1"]}]
        )
        with pytest.raises(LeaseConflict):
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "shape-1",
                            "type": "shape",
                            "locked": True,
                        },
                    }
                ],
            )
        stored = next(a for a in s.state["annotations"] if a["id"] == "shape-1")
        assert stored.get("locked") is not True

    # --- delete category, leased, on a generic (non-note) kind: mirrors
    # test_non_holder_delete_is_rejected above (which uses `note`) to confirm
    # the same rule for a GENERIC_OVERLAY_TYPES kind.
    async def test_non_holder_delete_of_a_shape_is_rejected_while_leased(self):
        mgr = _manager()
        s = await self._seeded_shape(mgr)
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["shape-1"]}]
        )
        with pytest.raises(LeaseConflict):
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [{"op": "annotation_deleted", "annotation_id": "shape-1"}],
            )
        assert any(a["id"] == "shape-1" for a in s.state["annotations"])


class TestPerKindReconnectCatchUpAndLocks:
    """Per-kind reconnect/catch-up, duplicate-op-suppression and lock-
    ownership audit (task-annotation-shared-session-realtime remaining_scope
    item 2) for the six v1 kinds beyond note/label/group — GraphCanvasRemote.
    test.jsx already covers those three at the canvas-application layer, and
    ``TestClaimEnforcement``/``TestCatchUp`` above already cover this
    protocol layer for ``note`` specifically.

    ``_claimed_annotation_target`` and ``SessionStore.apply_state_op`` are
    keyed only on an annotation's ``id``, never on its ``type``/``kind`` (see
    ``session_manager.py``), so in principle every one of these should already
    hold uniformly — but that is exactly the kind of claim a per-kind test
    audit exists to verify rather than assume, since the type-agnostic
    ``sessionSyncClient.test.js`` coverage cannot see a kind-specific gap one
    layer up (the canvas-side application logic GraphCanvasRemote.test.jsx
    covers separately). No gap was found for any of the six: every test below
    passes against the existing implementation.
    """

    # --- (a) reconnect / catch-up: a create made while a second client is
    # "disconnected" (never subscribed, so it never saw the op live) is
    # returned intact by catch_up once it asks for everything since its last
    # known seq — the same mechanism TestCatchUp proves for plain node ops. ---
    async def test_catch_up_returns_a_missed_create_for_text(self):
        await self._assert_catch_up_returns_a_missed_create("text")

    async def test_catch_up_returns_a_missed_create_for_shape(self):
        await self._assert_catch_up_returns_a_missed_create("shape")

    async def test_catch_up_returns_a_missed_create_for_icon(self):
        await self._assert_catch_up_returns_a_missed_create("icon")

    async def test_catch_up_returns_a_missed_create_for_vote_dot(self):
        await self._assert_catch_up_returns_a_missed_create("vote_dot")

    async def test_catch_up_returns_a_missed_create_for_image(self):
        await self._assert_catch_up_returns_a_missed_create("image")

    async def test_catch_up_returns_a_missed_create_for_freehand(self):
        await self._assert_catch_up_returns_a_missed_create("freehand")

    async def _assert_catch_up_returns_a_missed_create(self, kind):
        mgr = _manager()
        s = mgr.create_session()
        since_seq = s.seq
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": _kind_annotation(kind, "ann-1"),
                }
            ],
        )
        cu = mgr.catch_up(s.id, since_seq)
        assert cu["type"] == "catch_up"
        # Ring-buffer entries (session_store.py's `applied` dict) are the flat
        # op result shape — `entry["op"]` is the op-type string and
        # `entry["annotation"]` the resulting annotation — not a nested
        # `{"op": {...}}` envelope; matches the existing
        # `[op["seq"] for op in cu["ops"]]` read in TestCatchUp above.
        matches = [
            entry
            for entry in cu["ops"]
            if entry.get("op") == "annotation_created"
            and entry.get("annotation", {}).get("id") == "ann-1"
        ]
        assert len(matches) == 1
        assert matches[0]["annotation"]["type"] == kind

    # --- (b) duplicate-op suppression: a retried/re-delivered create with the
    # same id is an idempotent upsert, never a second appended annotation —
    # this is what makes a lost-response client retry, or the "delivered
    # twice around the catch-up boundary" case R15 in sessionSyncClient.js
    # documents, safe regardless of kind. ---
    async def test_a_resent_create_is_idempotent_for_text(self):
        await self._assert_resent_create_is_idempotent("text")

    async def test_a_resent_create_is_idempotent_for_shape(self):
        await self._assert_resent_create_is_idempotent("shape")

    async def test_a_resent_create_is_idempotent_for_icon(self):
        await self._assert_resent_create_is_idempotent("icon")

    async def test_a_resent_create_is_idempotent_for_vote_dot(self):
        await self._assert_resent_create_is_idempotent("vote_dot")

    async def test_a_resent_create_is_idempotent_for_image(self):
        await self._assert_resent_create_is_idempotent("image")

    async def test_a_resent_create_is_idempotent_for_freehand(self):
        await self._assert_resent_create_is_idempotent("freehand")

    async def _assert_resent_create_is_idempotent(self, kind):
        mgr = _manager()
        s = mgr.create_session()
        op = {"op": "annotation_created", "annotation": _kind_annotation(kind, "ann-1")}
        await mgr.apply_ops(s.id, "c1", 0, [op])
        # The exact same op, resent (a retried batch after a lost response).
        await mgr.apply_ops(s.id, "c1", 0, [dict(op)])
        matching = [a for a in s.state["annotations"] if a["id"] == "ann-1"]
        assert len(matching) == 1

    # --- (c) lock ownership: another client's live claim blocks a write to
    # this kind identically to every other kind. ---
    async def test_non_holder_update_is_rejected_for_text(self):
        await self._assert_non_holder_update_is_rejected("text")

    async def test_non_holder_update_is_rejected_for_shape(self):
        await self._assert_non_holder_update_is_rejected("shape")

    async def test_non_holder_update_is_rejected_for_icon(self):
        await self._assert_non_holder_update_is_rejected("icon")

    async def test_non_holder_update_is_rejected_for_vote_dot(self):
        await self._assert_non_holder_update_is_rejected("vote_dot")

    async def test_non_holder_update_is_rejected_for_image(self):
        await self._assert_non_holder_update_is_rejected("image")

    async def test_non_holder_update_is_rejected_for_freehand(self):
        await self._assert_non_holder_update_is_rejected("freehand")

    async def _assert_non_holder_update_is_rejected(self, kind):
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": _kind_annotation(kind, "ann-1"),
                }
            ],
        )
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["ann-1"]}]
        )
        with pytest.raises(LeaseConflict) as exc_info:
            await mgr.apply_ops(
                s.id,
                "c2",
                0,
                [
                    {
                        "op": "annotation_updated",
                        "annotation": _kind_annotation(kind, "ann-1", variant="b"),
                    }
                ],
            )
        assert exc_info.value.annotation_id == "ann-1"
        assert exc_info.value.held_by == "c1"

    async def test_non_holder_delete_is_rejected_for_text(self):
        await self._assert_non_holder_delete_is_rejected("text")

    async def test_non_holder_delete_is_rejected_for_shape(self):
        await self._assert_non_holder_delete_is_rejected("shape")

    async def test_non_holder_delete_is_rejected_for_icon(self):
        await self._assert_non_holder_delete_is_rejected("icon")

    async def test_non_holder_delete_is_rejected_for_vote_dot(self):
        await self._assert_non_holder_delete_is_rejected("vote_dot")

    async def test_non_holder_delete_is_rejected_for_image(self):
        await self._assert_non_holder_delete_is_rejected("image")

    async def test_non_holder_delete_is_rejected_for_freehand(self):
        await self._assert_non_holder_delete_is_rejected("freehand")

    async def _assert_non_holder_delete_is_rejected(self, kind):
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id,
            "c1",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": _kind_annotation(kind, "ann-1"),
                }
            ],
        )
        await mgr.apply_ops(
            s.id, "c1", 0, [{"op": "edit_lease_acquired", "element_ids": ["ann-1"]}]
        )
        with pytest.raises(LeaseConflict):
            await mgr.apply_ops(
                s.id, "c2", 0, [{"op": "annotation_deleted", "annotation_id": "ann-1"}]
            )
        assert any(a["id"] == "ann-1" for a in s.state["annotations"])


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

    async def test_max_request_body_bytes_admits_the_larger_of_the_two_caps(self):
        """The REST layer's pre-parse body-size check (rest_api.py) cannot
        yet tell an ordinary batch from one carrying a validated embedded
        image — that needs the JSON decoded — so it has to use whichever cap
        is larger, not the flat one alone (that would 413 a real embedded-
        image batch before apply_ops ever got a chance to route it
        correctly)."""
        flat_larger = _manager(max_op_batch_bytes=999_999_999)
        assert (
            flat_larger.max_request_body_bytes
            == flat_larger.max_op_batch_bytes
            == 999_999_999
        )
        image_larger = _manager(max_op_batch_bytes=256)
        assert (
            image_larger.max_request_body_bytes
            == image_ingest.DEFAULT_MAX_SESSION_DOCUMENT_BYTES
        )

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


class TestApplyOpsImageBudgets:
    """``apply_ops`` is the browser's write path (``POST .../ops``, via
    sessionSyncClient.js). An embedded image annotation must route through
    the image/session/document budgets instead of the flat op-batch cap
    (smallfix-embedded-image-over-op-batch-cap-immovable — without this, a
    ~400KB image that ``create_image_annotation`` happily created becomes
    permanently unmovable, since every move/resize/relayer/lock re-sends the
    whole annotation and always exceeded the flat cap), while the *ops* path
    also needs its own per-image cap (nothing here calls
    ``image_ingest.optimize_image``) and a cumulative session/document budget
    that many small batches cannot walk past over time
    (smallfix-session-ops-path-ignores-image-budgets)."""

    async def test_browser_move_of_a_large_embedded_image_succeeds(self):
        """The exact shape sessionSyncClient.js sends on move/resize/
        relayer/lock: the whole annotation, embedded image included, as one
        `annotation_updated` op. A ``max_op_batch_bytes`` well under the
        image's size pins that this is no longer bounded by the flat cap."""
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        image = _image_annotation("img-1", data_bytes=400_000)
        mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            image,
            optimized_image_bytes=400_000,
            max_session_image_bytes=1_000_000,
        )
        moved = dict(image)
        moved["position"] = {"x": 40, "y": 60}

        res = await mgr.apply_ops(
            s.id,
            "browser-1",
            s.seq,
            [{"op": "annotation_updated", "annotation": moved}],
            max_session_image_bytes=1_000_000,
        )

        assert res["seq"] == 2
        assert mgr.get_session(s.id).state["annotations"][0]["position"] == {
            "x": 40,
            "y": 60,
        }

    async def test_non_image_ops_in_the_same_batch_are_unaffected(self):
        """A batch mixing an image echo with an ordinary op (the common case
        — a multi-select drag) must still apply both, with the non-image op
        still bounded by the flat cap it always was."""
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        image = _image_annotation("img-1", data_bytes=400_000)
        mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            image,
            optimized_image_bytes=400_000,
            max_session_image_bytes=1_000_000,
        )
        moved = dict(image)
        moved["position"] = {"x": 5, "y": 5}

        res = await mgr.apply_ops(
            s.id,
            "browser-1",
            s.seq,
            [
                {"op": "annotation_updated", "annotation": moved},
                {"op": "nodes_added", "node_ids": ["a"]},
            ],
            max_session_image_bytes=1_000_000,
        )

        assert res["seq"] == 3
        assert mgr.get_session(s.id).state["node_refs"] == ["a"]

    async def test_forged_oversized_image_op_is_rejected(self):
        """Neither this generic ops path nor ``upsert_annotation`` goes
        through ``image_ingest.optimize_image``'s own size enforcement, so a
        client posting an over-budget but correctly-prefixed payload
        directly must still be capped, not waved through as one validated
        image."""
        mgr = _manager()
        s = mgr.create_session()
        huge = _image_annotation(
            "img-1", data_bytes=image_ingest.DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES + 1
        )
        with pytest.raises(ImageBudgetExceeded):
            await mgr.apply_ops(
                s.id,
                "browser-1",
                s.seq,
                [{"op": "annotation_created", "annotation": huge}],
            )
        assert mgr.get_session(s.id).state["annotations"] == []

    async def test_cumulative_session_budget_closes_the_many_small_batches_path(
        self,
    ):
        """smallfix-session-ops-path-ignores-image-budgets: the flat cap only
        bounds *one* request. Many separate, individually-legal batches (each
        far under the flat op-batch cap on its own) must not be able to walk
        the session's total document size past its configured budget."""
        mgr = _manager()
        s = mgr.create_session()
        chunk = "x" * 2000

        async def _add(suffix, max_session_document_bytes):
            return await mgr.apply_ops(
                s.id,
                "browser-1",
                s.seq,
                [
                    {
                        "op": "annotation_created",
                        "annotation": {
                            "id": f"n{suffix}",
                            "type": "label",
                            "text": chunk,
                        },
                    }
                ],
                max_session_document_bytes=max_session_document_bytes,
            )

        # A generous budget lets two small, individually-legal batches
        # through, establishing the document size they leave behind.
        generous = 10_000_000
        await _add(0, generous)
        await _add(1, generous)
        size_after_two = len(json.dumps(mgr.get_session(s.id).to_dict()))
        seq_after_two = mgr.get_session(s.id).seq

        # Pin the budget to exactly what those two batches already used: a
        # third batch — on its own trivially under the flat op-batch cap —
        # must still be refused, because it is the *cumulative* session
        # document being checked now, not just this one request's size.
        with pytest.raises(ImageBudgetExceeded):
            await _add(2, size_after_two)

        assert mgr.get_session(s.id).seq == seq_after_two
        assert len(json.dumps(mgr.get_session(s.id).to_dict())) == size_after_two


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

    async def test_flat_cap_still_applies_to_a_large_non_image_annotation(self):
        """The image-budget carve-out below must not swallow the flat cap
        for everything else: smallfix-duplicate-image-annotation-op-cap
        fixes embedded images specifically, not oversized text/label
        payloads, which stay bounded by the small generic cap."""
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        with pytest.raises(OpBatchTooLarge):
            mgr.upsert_annotation(
                s.id, "mcp-agent", {"type": "label", "text": "x" * 1000}
            )
        assert s.state["annotations"] == []

    async def test_duplicate_style_copy_of_a_realistic_image_succeeds(self):
        """Regression test for smallfix-duplicate-image-annotation-op-cap:
        ``duplicate_annotation`` (mcp_tools.py) builds its copy by taking the
        stored annotation dict, swapping in a new id, and calling this same
        method — this pins that shape directly. Before the fix, a
        realistically sized embedded image (bigger than the 256-byte-here
        flat cap, still under the per-image budget) created fine via
        ``upsert_image_annotation`` but failed to duplicate with
        ``too_large``."""
        mgr = _manager(max_op_batch_bytes=256)
        s = mgr.create_session()
        original = _image_annotation("img-1", data_bytes=2000)
        mgr.upsert_image_annotation(
            s.id, "mcp-agent", original, optimized_image_bytes=2000
        )
        copy = dict(original)
        copy["id"] = "img-2"

        res = mgr.upsert_annotation(s.id, "mcp-agent", copy)

        assert res["annotation"]["id"] == "img-2"
        assert len(s.state["annotations"]) == 2

    async def test_forged_oversized_image_payload_is_still_capped(self):
        """Neither this generic path nor ``apply_ops`` (see
        ``TestApplyOpsImageBudgets``) goes through ``image_ingest.
        optimize_image``, so without an explicit per-image check here a
        caller could submit an over-budget but correctly-prefixed payload
        directly and have it waved through as if it were one validated
        image. The per-image cap in ``_check_image_budgets`` closes that."""
        mgr = _manager()
        s = mgr.create_session()
        huge = _image_annotation(
            "img-1", data_bytes=image_ingest.DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES + 1
        )
        with pytest.raises(ImageBudgetExceeded):
            mgr.upsert_annotation(s.id, "mcp-agent", huge)
        assert s.state["annotations"] == []


class TestUpsertImageAnnotation:
    """The synchronous image-annotation write path
    (``upsert_image_annotation``), including the image-specific session and
    document byte budgets it enforces instead of the generic op-batch cap
    (see ``TestOpBatchByteCap`` for that cap and the class docstring on
    ``upsert_image_annotation`` for why images need their own), and which
    bucket its rate limit is charged to on each of its two caller shapes."""

    async def test_image_ingest_throttles_on_the_rate_limit_key_not_the_client_id(
        self,
    ):
        """A caller that attributes its ops to a fixed marker (the REST image
        ingest endpoint does, so the pasting browser's SSE echo is not dropped)
        passes ``rate_limit_key`` so it is throttled per real originator rather
        than putting every such caller in the marker's one bucket."""
        mgr = _manager(bucket_capacity=1, bucket_refill_per_sec=0)
        mgr._image_bucket = _TokenBucket(1.0, 0.0)
        s = mgr.create_session()

        mgr.upsert_image_annotation(
            s.id,
            "human-image-ingest",
            _image_annotation("img-1", data_bytes=100),
            optimized_image_bytes=100,
            rate_limit_key="1.2.3.4",
        )
        with pytest.raises(RateLimited):
            mgr.upsert_image_annotation(
                s.id,
                "human-image-ingest",
                _image_annotation("img-2", data_bytes=100),
                optimized_image_bytes=100,
                rate_limit_key="1.2.3.4",
            )
        # Same marker, different originator: its own budget, and the op bucket
        # (still holding its single token) is not what is being spent here.
        mgr.upsert_image_annotation(
            s.id,
            "human-image-ingest",
            _image_annotation("img-3", data_bytes=100),
            optimized_image_bytes=100,
            rate_limit_key="5.6.7.8",
        )

    async def test_image_ingest_without_a_rate_limit_key_falls_back_to_op_bucket(self):
        """The MCP path passes no key and must keep drawing from the op bucket
        under its own client id — dropping that fallback would leave it
        unthrottled entirely."""
        mgr = _manager(bucket_capacity=1, bucket_refill_per_sec=0)
        s = mgr.create_session()

        mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            _image_annotation("img-1", data_bytes=100),
            optimized_image_bytes=100,
        )
        with pytest.raises(RateLimited):
            mgr.upsert_image_annotation(
                s.id,
                "mcp-agent",
                _image_annotation("img-2", data_bytes=100),
                optimized_image_bytes=100,
            )

    async def test_image_bucket_exhaustion_does_not_block_the_fallback_path(self):
        """The two buckets are independent: a spent source budget must not
        throttle a caller that keys on the op bucket instead."""
        mgr = _manager(bucket_capacity=2, bucket_refill_per_sec=0)
        mgr._image_bucket = _TokenBucket(0.0, 0.0)
        s = mgr.create_session()

        res = mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            _image_annotation("img-1", data_bytes=100),
            optimized_image_bytes=100,
        )

        assert res["annotation"]["id"] == "img-1"

    async def test_creates_and_broadcasts(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        res = mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            _image_annotation("img-1", data_bytes=100),
            optimized_image_bytes=100,
        )

        assert res["revision"] == s.seq == 1
        assert res["annotation"]["id"] == "img-1"
        assert s.state["annotations"][0]["type"] == "image"
        events = await _drain(sub)
        assert events[0]["op"]["op"] == "annotation_created"
        assert events[0]["seq"] == 1

    async def test_matching_id_upserts_in_place_and_excludes_old_copy_from_budget(
        self,
    ):
        """A same-id replace must budget the *new* image, not old+new — see
        ``exclude_id`` on ``_session_image_bytes``."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            _image_annotation("img-1", data_bytes=1000),
            optimized_image_bytes=1000,
            max_session_image_bytes=1500,
        )

        res = mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            _image_annotation("img-1", data_bytes=1200),
            optimized_image_bytes=1200,
            max_session_image_bytes=1500,
        )

        assert len(s.state["annotations"]) == 1
        assert res["revision"] == 2

    async def test_session_image_budget_rejects_before_writing(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            _image_annotation("img-1", data_bytes=800),
            optimized_image_bytes=800,
            max_session_image_bytes=1000,
        )
        seq_before = s.seq

        with pytest.raises(ImageBudgetExceeded):
            mgr.upsert_image_annotation(
                s.id,
                "mcp-agent",
                _image_annotation("img-2", data_bytes=300),
                optimized_image_bytes=300,
                max_session_image_bytes=1000,
            )

        assert s.seq == seq_before
        assert len(s.state["annotations"]) == 1

    async def test_document_budget_rejects_before_writing(self):
        mgr = _manager()
        s = mgr.create_session()
        seq_before = s.seq

        with pytest.raises(ImageBudgetExceeded):
            mgr.upsert_image_annotation(
                s.id,
                "mcp-agent",
                _image_annotation("img-1", data_bytes=500),
                optimized_image_bytes=500,
                max_session_image_bytes=10_000,
                max_session_document_bytes=200,
            )

        assert s.seq == seq_before
        assert s.state["annotations"] == []

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_image_annotation(
            s.id,
            "mcp-agent",
            _image_annotation("img-1", data_bytes=100),
            optimized_image_bytes=100,
        )
        with pytest.raises(RevisionConflict):
            mgr.upsert_image_annotation(
                s.id,
                "mcp-agent",
                _image_annotation("img-2", data_bytes=100),
                optimized_image_bytes=100,
                expected_revision=0,
            )
        assert len(s.state["annotations"]) == 1

    async def test_busy_when_session_lock_held(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr._lock(s.id).acquire()
        with pytest.raises(LayoutBusy):
            mgr.upsert_image_annotation(
                s.id,
                "mcp-agent",
                _image_annotation("img-1", data_bytes=100),
                optimized_image_bytes=100,
            )

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.upsert_image_annotation(
                "9999-9999",
                "mcp-agent",
                _image_annotation("img-1", data_bytes=100),
                optimized_image_bytes=100,
            )


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


class TestSetGroupMembers:
    """The synchronous MCP group-membership write path (``set_group_members``),
    wrapping the ``group_membership_changed`` op."""

    async def test_replaces_membership_and_broadcasts(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "group-1", "type": "group"})
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        res = mgr.set_group_members(s.id, "mcp-agent", "group-1", ["n1", "n2"])

        assert res["revision"] == s.seq
        assert res["annotation"]["member_node_ids"] == ["n1", "n2"]
        stored = s.state["annotations"][0]
        assert stored["member_node_ids"] == ["n1", "n2"]
        events = await _drain(sub)
        assert events[0]["op"]["op"] == "group_membership_changed"

    async def test_missing_group_raises_op_error(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            mgr.set_group_members(s.id, "mcp-agent", "ghost", ["n1"])

    async def test_non_group_annotation_id_raises_op_error(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        with pytest.raises(OpError):
            mgr.set_group_members(s.id, "mcp-agent", "note-1", ["n1"])

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "group-1", "type": "group"})
        with pytest.raises(RevisionConflict):
            mgr.set_group_members(
                s.id, "mcp-agent", "group-1", ["n1"], expected_revision=0
            )
        assert s.state["annotations"][0].get("member_node_ids") is None

    async def test_busy_when_session_lock_held(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "group-1", "type": "group"})
        await mgr._lock(s.id).acquire()
        with pytest.raises(LayoutBusy):
            mgr.set_group_members(s.id, "mcp-agent", "group-1", ["n1"])

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.set_group_members("9999-9999", "mcp-agent", "group-1", ["n1"])

    async def test_non_string_member_id_raises_op_error(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "group-1", "type": "group"})
        with pytest.raises(OpError):
            mgr.set_group_members(s.id, "mcp-agent", "group-1", [1, 2])

    async def test_membership_change_is_not_recorded_in_activity_log(self):
        """group_membership_changed is deliberately outside UNDOABLE_OPS
        (session_activity.py) — it does not become an undoable action."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "group-1", "type": "group"})
        before = len(s.activity_log)

        mgr.set_group_members(s.id, "mcp-agent", "group-1", ["n1"])

        assert len(s.activity_log) == before


class TestUndoLastAction:
    """Actor-scoped undo (``undo_last_action``) over the persistent activity log."""

    async def test_undo_create_removes_the_annotation(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})

        res = mgr.undo_last_action(s.id, "mcp-agent")

        assert s.state["annotations"] == []
        assert res["undone_op"] == "annotation_created"
        assert res["revision"] == s.seq

    async def test_undo_replay_broadcast_is_not_attributed_to_the_undoing_clients_own_id(
        self,
    ):
        """Regression for smallfix-undo-inverse-op-dropped-as-own-echo.

        sessionSyncClient.js drops any incoming SSE op whose ``client_id``
        matches the receiving browser's own — correct behaviour for a genuine
        self-authored echo (pinned by
        sessionSyncClient.test.js's "suppresses echoes of its own client").
        Before this fix, ``undo_last_action`` replayed the inverse op under
        the *undoing* client's own ``client_id``, so the undoing browser's
        own SSE subscription — the same one that receives every op it is
        connected for — received a broadcast it would filter out as an echo
        of its own action, even though the undo genuinely changed session
        state and every other client saw it. This pins that the broadcast is
        attributed to the dedicated ``_UNDO_REPLAY_CLIENT_ID`` marker
        instead, mirroring rest_api.py's ``_HUMAN_IMAGE_INGEST_CLIENT_ID``
        for the identical trap.
        """
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "c1", {"id": "note-1", "type": "note"})

        # The undoing client's own SSE subscription — the exact vantage point
        # sessionSyncClient.js's self-echo check filters from.
        sub, _ = mgr.connect(s.id, "c1", "Undoer")
        await _drain(sub)  # discard presence_joined

        mgr.undo_last_action(s.id, "c1")

        events = await _drain(sub)
        op_events = [e for e in events if e.get("type") == "op"]
        assert len(op_events) == 1
        assert op_events[0]["client_id"] == _UNDO_REPLAY_CLIENT_ID
        assert op_events[0]["client_id"] != "c1"
        assert op_events[0]["op"]["op"] == "annotation_deleted"

    async def test_undo_update_restores_prior_fields(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(
            s.id, "mcp-agent", {"id": "note-1", "type": "note", "text": "before"}
        )
        mgr.update_annotation(
            s.id, "mcp-agent", {"id": "note-1", "type": "note", "text": "after"}
        )

        mgr.undo_last_action(s.id, "mcp-agent")

        restored = next(a for a in s.state["annotations"] if a["id"] == "note-1")
        assert restored["text"] == "before"

    async def test_undo_delete_restores_the_annotation(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(
            s.id, "mcp-agent", {"id": "note-1", "type": "note", "text": "hello"}
        )
        mgr.delete_annotation(s.id, "mcp-agent", "note-1")
        assert s.state["annotations"] == []

        res = mgr.undo_last_action(s.id, "mcp-agent")

        assert res["undone_op"] == "annotation_deleted"
        restored = next(a for a in s.state["annotations"] if a["id"] == "note-1")
        assert restored["text"] == "hello"
        # The delete-guard memory must not block the id from being re-created
        # by anyone else afterwards either.
        assert "note-1" not in s._deleted_annotation_ids

    async def test_undo_of_a_freehand_move_restores_its_sampled_points(self):
        """A freehand stroke's shape lives in its ``points`` (absolute
        model-space), outside the geometry envelope every other type is moved
        by, so a move rewrites every point (``translate_freehand_points``).
        Undo therefore has to put the points back as well as the position:
        an inverse op narrowed to the patched envelope fields would restore
        the position and leave the stroke still drawn at the moved
        coordinates. Only the full-prior inverse
        (``session_store.apply_state_op``) gets this right, which is what
        this pins — docs/ANNOTATION_CONTRACT.md's `freehand` Activity/undo
        cell rests on it.
        """
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(
            s.id,
            "actor-a",
            build_annotation(
                type="freehand",
                x=0,
                y=0,
                content={
                    "points": [
                        {"x": 0, "y": 0, "pressure": 0.4},
                        {"x": 10, "y": 10, "pressure": 0.8},
                    ]
                },
                annotation_id="freehand-1",
            ),
        )
        existing = next(a for a in s.state["annotations"] if a["id"] == "freehand-1")
        mgr.update_annotation(
            s.id, "actor-a", build_annotation_patch(existing, x=100, y=50)
        )
        moved = next(a for a in s.state["annotations"] if a["id"] == "freehand-1")
        assert moved["points"] == [
            {"x": 100, "y": 50, "pressure": 0.4},
            {"x": 110, "y": 60, "pressure": 0.8},
        ]

        mgr.undo_last_action(s.id, "actor-a")

        restored = next(a for a in s.state["annotations"] if a["id"] == "freehand-1")
        assert restored["points"] == [
            {"x": 0, "y": 0, "pressure": 0.4},
            {"x": 10, "y": 10, "pressure": 0.8},
        ]
        assert restored["position"] == {"x": 0, "y": 0}
        assert restored["geometry"]["x"] == 0 and restored["geometry"]["y"] == 0

    async def test_undo_of_a_freehand_delete_restores_its_points_and_pressure(self):
        """The delete inverse replays the whole removed annotation, so a
        restored stroke has to come back drawable — points and their optional
        per-point pressure included, not just the envelope.
        """
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(
            s.id,
            "actor-a",
            build_annotation(
                type="freehand",
                x=0,
                y=0,
                content={
                    "points": [
                        {"x": 0, "y": 0, "pressure": 0.4},
                        {"x": 4, "y": 7, "pressure": 0.9},
                    ],
                    "strokeWidth": 3,
                },
                annotation_id="freehand-1",
            ),
        )
        mgr.delete_annotation(s.id, "actor-a", "freehand-1")
        assert s.state["annotations"] == []

        mgr.undo_last_action(s.id, "actor-a")

        restored = next(a for a in s.state["annotations"] if a["id"] == "freehand-1")
        assert restored["points"] == [
            {"x": 0, "y": 0, "pressure": 0.4},
            {"x": 4, "y": 7, "pressure": 0.9},
        ]
        assert restored["strokeWidth"] == 3

    async def test_undo_node_moved_restores_prior_position(self):
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

    async def test_undo_edges_dimmed_restores_them(self):
        """task-session-focus-dimming-controls: dim/restore round-trips through
        the same undo path as hide/show, without touching graph data."""
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "mcp-agent", None, [{"op": "edges_dimmed", "edge_ids": ["e1", "e2"]}]
        )

        mgr.undo_last_action(s.id, "mcp-agent")

        assert s.state["dimmed_edge_ids"] == []

    async def test_undo_edge_intensity_set_restores_prior_value(self):
        mgr = _manager()
        s = mgr.create_session()
        await mgr.apply_ops(
            s.id, "mcp-agent", None, [{"op": "edge_intensity_set", "value": 0.3}]
        )
        await mgr.apply_ops(
            s.id, "mcp-agent", None, [{"op": "edge_intensity_set", "value": 0.9}]
        )

        mgr.undo_last_action(s.id, "mcp-agent")

        assert s.state["edge_intensity"] == 0.3

    async def test_no_undoable_action_for_actor_with_no_history(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(NoUndoableAction):
            mgr.undo_last_action(s.id, "mcp-agent")

    async def test_undo_is_rate_limited_like_other_write_paths(self):
        mgr = _manager(bucket_capacity=0, bucket_refill_per_sec=0)
        s = mgr.create_session()
        with pytest.raises(RateLimited):
            mgr.undo_last_action(s.id, "mcp-agent")

    async def test_undo_is_scoped_to_the_requesting_actor(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "actor-a", {"id": "note-1", "type": "note"})
        with pytest.raises(NoUndoableAction):
            mgr.undo_last_action(s.id, "actor-b")
        # actor-a's own action is untouched and still undoable.
        mgr.undo_last_action(s.id, "actor-a")
        assert s.state["annotations"] == []

    async def test_conflict_when_annotation_changed_by_someone_else_since(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(
            s.id, "actor-a", {"id": "note-1", "type": "note", "text": "mine"}
        )
        mgr.update_annotation(
            s.id, "actor-b", {"id": "note-1", "type": "note", "text": "theirs"}
        )

        with pytest.raises(UndoConflict):
            mgr.undo_last_action(s.id, "actor-a")
        # The conflicted undo must not have mutated anything.
        current = next(a for a in s.state["annotations"] if a["id"] == "note-1")
        assert current["text"] == "theirs"

    async def test_double_undo_by_same_actor_falls_back_to_the_older_action(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-2", "type": "note"})

        mgr.undo_last_action(s.id, "mcp-agent")
        ids = {a["id"] for a in s.state["annotations"]}
        assert ids == {"note-1"}

        mgr.undo_last_action(s.id, "mcp-agent")
        assert s.state["annotations"] == []

        with pytest.raises(NoUndoableAction):
            mgr.undo_last_action(s.id, "mcp-agent")

    async def test_unknown_session_raises(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.undo_last_action("9999-9999", "mcp-agent")

    async def test_expected_revision_conflict(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        with pytest.raises(RevisionConflict):
            mgr.undo_last_action(s.id, "mcp-agent", expected_revision=0)

    async def test_busy_when_session_lock_held(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        await mgr._lock(s.id).acquire()
        with pytest.raises(LayoutBusy):
            mgr.undo_last_action(s.id, "mcp-agent")

    async def test_undo_broadcasts_the_inverse_op(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        sub, _ = mgr.connect(s.id, "c1", "A")
        await _drain(sub)

        mgr.undo_last_action(s.id, "mcp-agent")

        events = await _drain(sub)
        assert events[0]["op"]["op"] == "annotation_deleted"

    async def test_undo_itself_is_not_undoable(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        mgr.undo_last_action(s.id, "mcp-agent")
        with pytest.raises(NoUndoableAction):
            mgr.undo_last_action(s.id, "mcp-agent")

    async def test_list_activity_returns_newest_first(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-1", "type": "note"})
        mgr.upsert_annotation(s.id, "mcp-agent", {"id": "note-2", "type": "note"})

        records = mgr.list_activity(s.id)

        assert [r["affected"]["id"] for r in records] == ["note-2", "note-1"]
