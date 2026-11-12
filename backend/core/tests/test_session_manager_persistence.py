# covers: backend/core/session_manager.py
"""
Persistence and ring-buffer tests for multi-user shared sessions (step 8 hardening).

Tests ring buffer wrapping, activity log caps, reconnection catch-up, and
snapshot fallback — covering the bounded structures that keep a session's
op history usable after restarts and reconnects.
"""

import pytest

from backend.core.session_activity import DEFAULT_MAX_ACTIVITY_RECORDS
from backend.core.session_store import (
    _DEFAULT_RING_SIZE,
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.core.session_manager import SessionManager

pytestmark = pytest.mark.asyncio


def _manager() -> SessionManager:
    return SessionManager(SessionStore(InMemorySessionPersistenceBackend()))


async def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def _ops(events):
    return [e["op"]["op"] for e in events if e.get("type") == "op"]


class TestSustainedMovesPersistence:
    async def test_sustained_moves_persist_mirror_and_survive_reload(self, tmp_path):
        """Node moves keep persisting + mirroring after a long op sequence.

        Backbone invariant behind the "shared session silently loses node-move
        persistence over time" bug: after a sustained sequence of ops (here well
        past the op ring buffer and the activity-log cap, from both clients),
        every node's latest position must be (a) converged in server state, (b)
        delivered to *both* clients, and (c) reloadable from disk by a fresh
        store — the exact three
        things the founder report found broken ("not reflected in the other
        client's view … reloading shows none of the moves were stored").
        """
        # Generous rate limit so the tight loop measures persistence/mirroring,
        # not the per-client token bucket (which real drag cadence never hits).
        # Small ring and activity caps let a short sequence of one-op batches
        # wrap both bounded structures; the production defaults are pinned by
        # test_default_caps_wrap_and_hold_their_bounds below.
        bound = 20
        mgr = SessionManager(
            SessionStore(
                FileSessionPersistenceBackend(tmp_path),
                ring_size=bound,
                max_activity_records=bound,
            ),
            bucket_capacity=100_000,
            bucket_refill_per_sec=100_000,
        )
        sid = mgr.create_session(name="Shared").id
        sub_a, _ = mgr.connect(sid, "A", "Alice")
        sub_b, _ = mgr.connect(sid, "B", "Bob")
        await _drain(sub_a)
        await _drain(sub_b)

        await mgr.apply_ops(
            sid,
            "A",
            0,
            [{"op": "nodes_added", "node_ids": [f"n{i}" for i in range(5)]}],
        )

        last: dict[str, dict[str, float]] = {}
        moves = 3 * bound  # forces the ring and activity log to trim mid-session
        for i in range(moves):
            who = "A" if i % 2 == 0 else "B"
            nid = f"n{i % 5}"
            pos = {"x": float(i), "y": float(i * 2)}
            await mgr.apply_ops(
                sid, who, None, [{"op": "node_moved", "node_id": nid, "position": pos}]
            )
            last[nid] = pos

        # Both bounded structures really wrapped, so (a)-(c) hold past a trim.
        ring = mgr.store.ring(sid)
        assert len(ring) == bound and ring[0]["seq"] > 1
        log = mgr.get_session(sid).activity_log
        # Membership has its own retention budget; the initial add survives
        # while the move history keeps exactly its newest window.
        assert len(log) == bound + 1
        assert log[0]["op"] == "nodes_added" and log[0]["seq"] == 1
        assert [r["op"] for r in log[1:]] == ["node_moved"] * bound
        assert [r["seq"] for r in log[1:]] == list(range(moves - bound + 2, moves + 2))

        # (a) Server state converged to each node's final move.
        state = mgr.get_session(sid).state
        for nid, pos in last.items():
            assert state["positions"][nid] == pos

        # (b) Both clients received every move (fan-out never stopped).
        a_moves = [
            e for e in await _drain(sub_a) if e.get("op", {}).get("op") == "node_moved"
        ]
        b_moves = [
            e for e in await _drain(sub_b) if e.get("op", {}).get("op") == "node_moved"
        ]
        assert len(a_moves) == moves
        assert len(b_moves) == moves

        # (c) A fresh store (simulating a browser reload / process restart) reads
        # the persisted positions back from disk.
        reloaded = SessionStore(FileSessionPersistenceBackend(tmp_path)).get(sid)
        for nid, pos in last.items():
            assert reloaded.state["positions"][nid] == pos

    async def test_default_caps_wrap_and_hold_their_bounds(self, tmp_path):
        """The sustained-moves test above injects small caps; this pins the
        real defaults: a store built without caps wraps both structures at
        the default constants and keeps exactly the newest window. Moves go in multi-op batches to keep it
        fast; each batch still takes the rollback and snapshot path."""
        mgr = SessionManager(
            SessionStore(FileSessionPersistenceBackend(tmp_path)),
            bucket_capacity=100_000,
            bucket_refill_per_sec=100_000,
        )
        sid = mgr.create_session(name="Shared").id
        await mgr.apply_ops(
            sid, "A", 0, [{"op": "nodes_added", "node_ids": ["n0", "n1"]}]
        )

        moves = max(_DEFAULT_RING_SIZE, DEFAULT_MAX_ACTIVITY_RECORDS) + 20
        batch = 50
        for start in range(0, moves, batch):
            await mgr.apply_ops(
                sid,
                "A" if start % 2 == 0 else "B",
                None,
                [
                    {
                        "op": "node_moved",
                        "node_id": f"n{i % 2}",
                        "position": {"x": float(i), "y": 0.0},
                    }
                    for i in range(start, min(start + batch, moves))
                ],
            )

        session = mgr.get_session(sid)
        ring = mgr.store.ring(sid)
        assert len(ring) == _DEFAULT_RING_SIZE
        assert ring[-1]["seq"] == session.seq == moves + 1
        assert ring[0]["seq"] == session.seq - _DEFAULT_RING_SIZE + 1
        log = session.activity_log
        assert len(log) == DEFAULT_MAX_ACTIVITY_RECORDS + 1
        assert log[0]["op"] == "nodes_added" and log[0]["seq"] == 1
        assert [r["op"] for r in log[1:]] == [
            "node_moved"
        ] * DEFAULT_MAX_ACTIVITY_RECORDS
        assert [r["seq"] for r in log[1:]] == list(
            range(session.seq - DEFAULT_MAX_ACTIVITY_RECORDS + 1, session.seq + 1)
        )
        assert log[-1]["seq"] == session.seq
        reloaded = SessionStore(FileSessionPersistenceBackend(tmp_path)).get(sid)
        assert [r["id"] for r in reloaded.activity_log] == [r["id"] for r in log]
        assert reloaded.state["positions"]["n1"] == {"x": moves - 1.0, "y": 0.0}


class TestReconnectCatchUp:
    async def test_reconnect_catches_up_missed_ops(self):
        mgr = _manager()
        sid = mgr.create_session().id

        sub_a, _ = mgr.connect(sid, "A", "Alice")
        await _drain(sub_a)

        # A works while B is disconnected.
        await mgr.apply_ops(
            sid, "A", 0, [{"op": "nodes_added", "node_ids": ["node-1"]}]
        )
        seq_before = mgr.get_session(sid).seq

        # B (re)connects passing its last-known seq: gets the missed op, not a
        # full snapshot, because the ring still proves continuity.
        catch_up = mgr.catch_up(sid, 0)
        assert catch_up["type"] == "catch_up"
        assert [o["op"] for o in catch_up["ops"]] == ["nodes_added"]
        assert catch_up["seq"] == seq_before

    async def test_reconnect_falls_back_to_snapshot_when_ring_trimmed(self):
        mgr = SessionManager(
            SessionStore(InMemorySessionPersistenceBackend(), ring_size=2)
        )
        sid = mgr.create_session().id
        for i in range(5):
            await mgr.apply_ops(
                sid, "A", 0, [{"op": "nodes_added", "node_ids": [f"n{i}"]}]
            )

        # A client stuck at seq 0 cannot be served from a 2-entry ring → snapshot.
        catch_up = mgr.catch_up(sid, 0)
        assert catch_up["type"] == "snapshot"
        assert catch_up["session"]["state"]["node_refs"] == [f"n{i}" for i in range(5)]
