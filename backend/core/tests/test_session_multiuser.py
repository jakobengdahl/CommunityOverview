"""
Multi-client integration test for shared sessions (step 8 hardening).

Drives two clients through one session at the ``SessionManager`` + event-bus
level — the deterministic core of the Playwright multi-context e2e (which
exercises the same scenarios through the real UI, but can't run in the core CI).
Covers the collaboration path end to end: presence, node add/move fan-out,
annotation create, selection claims/markers, rename, delete broadcast, and
reconnect catch-up.
"""

import pytest

from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
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


class TestTwoClientsOneSession:
    async def test_full_collaboration_flow(self):
        mgr = _manager()
        session = mgr.create_session(name="Shared")
        sid = session.id

        # Two browsers join the same session.
        sub_a, member_a = mgr.connect(sid, "A", "Alice")
        sub_b, member_b = mgr.connect(sid, "B", "Bob")

        # Presence: each client is on the roster with a distinct colour.
        roster = {m["client_id"]: m for m in mgr.roster(sid)}
        assert set(roster) == {"A", "B"}
        assert roster["A"]["color"] != roster["B"]["color"]

        # B saw A's earlier join and its own; drain presence noise before ops.
        await _drain(sub_a)
        await _drain(sub_b)

        # A adds two nodes and moves one — both clients receive both ops in order.
        await mgr.apply_ops(
            sid,
            "A",
            0,
            [
                {"op": "nodes_added", "node_ids": ["node-1", "node-2"]},
                {
                    "op": "node_moved",
                    "node_id": "node-1",
                    "position": {"x": 10, "y": 20},
                },
            ],
        )
        assert _ops(await _drain(sub_a)) == ["nodes_added", "node_moved"]
        assert _ops(await _drain(sub_b)) == ["nodes_added", "node_moved"]

        # Server converged: node refs + position are what both will render.
        state = mgr.get_session(sid).state
        assert state["node_refs"] == ["node-1", "node-2"]
        assert state["positions"]["node-1"] == {"x": 10.0, "y": 20.0}

        # B creates a sticky note; A receives it with a server-assigned id.
        await mgr.apply_ops(
            sid,
            "B",
            0,
            [
                {
                    "op": "annotation_created",
                    "annotation": {
                        "kind": "note",
                        "text": "hi",
                        "position": {"x": 1, "y": 2},
                    },
                },
            ],
        )
        a_events = await _drain(sub_a)
        created = [
            e for e in a_events if e.get("op", {}).get("op") == "annotation_created"
        ]
        assert len(created) == 1
        assert isinstance(created[0]["op"]["annotation"]["id"], str)
        await _drain(sub_b)

        # A selects node-1 → advisory claim; B sees the marker, server tracks it.
        await mgr.apply_ops(
            sid, "A", 0, [{"op": "selection_claimed", "element_ids": ["node-1"]}]
        )
        b_claim = [
            e
            for e in await _drain(sub_b)
            if e.get("op", {}).get("op") == "selection_claimed"
        ]
        assert b_claim and b_claim[0]["op"]["element_ids"] == ["node-1"]
        assert mgr.claimed_elements(sid) == ["node-1"]
        await _drain(sub_a)

        # Rename fans out to both.
        mgr.rename_session(sid, "Renamed")
        assert any(e["type"] == "session_renamed" for e in await _drain(sub_a))
        assert any(e["type"] == "session_renamed" for e in await _drain(sub_b))

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

    async def test_disconnect_releases_claims_and_presence(self):
        mgr = _manager()
        sid = mgr.create_session().id
        sub_a, _ = mgr.connect(sid, "A", "Alice")
        sub_b, _ = mgr.connect(sid, "B", "Bob")
        await _drain(sub_a)
        await _drain(sub_b)

        await mgr.apply_ops(
            sid, "A", 0, [{"op": "selection_claimed", "element_ids": ["node-1"]}]
        )
        assert mgr.claimed_elements(sid) == ["node-1"]

        # A leaves: its claim is released and B is told, so no element freezes.
        mgr.disconnect(sid, "A", sub_a)
        assert mgr.claimed_elements(sid) == []
        b_events = await _drain(sub_b)
        assert any(e.get("op", {}).get("op") == "selection_released" for e in b_events)
        assert any(e["type"] == "presence_left" for e in b_events)
        assert {m["client_id"] for m in mgr.roster(sid)} == {"B"}

    async def test_delete_broadcasts_before_teardown(self):
        mgr = _manager()
        sid = mgr.create_session().id
        sub_a, _ = mgr.connect(sid, "A", "Alice")
        await _drain(sub_a)

        mgr.delete_session(sid, deleted_by="A")
        events = await _drain(sub_a)
        deleted = [e for e in events if e["type"] == "session_deleted"]
        assert deleted and deleted[0]["deleted_by"] == "A"
        assert mgr.get_session(sid) is None
