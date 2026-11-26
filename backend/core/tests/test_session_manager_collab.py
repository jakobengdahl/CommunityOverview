# covers: backend/core/session_manager.py
"""
Multi-client collaboration tests (step 8 hardening).

Drives two clients through one session at the ``SessionManager`` + event-bus
level — the deterministic core of the Playwright multi-context e2e (which
exercises the same scenarios through the real UI, but can't run in the core CI).
Covers the collaboration path end to end: presence, node add/move fan-out,
annotation create, selection claims/markers, rename, delete broadcast, edge
ops, and reconnect race.
"""

import pytest

from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    OpError,
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

        # Rename fans out to both, routed through the op protocol (R8) so it
        # is sequenced and ring-buffered like any other state op.
        await mgr.rename_session(sid, "Renamed", client_id="A")
        assert any(
            e.get("op", {}).get("op") == "session_renamed" for e in await _drain(sub_a)
        )
        assert any(
            e.get("op", {}).get("op") == "session_renamed" for e in await _drain(sub_b)
        )

    async def test_edge_created_between_present_nodes_fans_out_to_all_clients(self):
        """A drag-drawn edge must render for *every* connected client.

        Regression for "edges sometimes don't appear in all connected clients
        even though all nodes are visible everywhere": edges live in the graph,
        not in session state, and are recovered by hydrating nodes — so an edge
        drawn between two nodes both already present triggers no node add and,
        without an explicit fan-out op, reaches nobody but the originator. The
        edges_added op carries the edge payload through to all subscribers while
        leaving session state (node_refs, positions, …) untouched.
        """
        mgr = _manager()
        sid = mgr.create_session(name="Shared").id
        sub_a, _ = mgr.connect(sid, "A", "Alice")
        sub_b, _ = mgr.connect(sid, "B", "Bob")

        # Both clients already share the two endpoint nodes.
        await mgr.apply_ops(
            sid, "A", 0, [{"op": "nodes_added", "node_ids": ["n1", "n2"]}]
        )
        await _drain(sub_a)
        await _drain(sub_b)
        state_before = mgr.get_session(sid).state["node_refs"][:]

        # A draws an edge between the two present nodes.
        edge = {"id": "e1", "source": "n1", "target": "n2", "type": "RELATES_TO"}
        result = await mgr.apply_ops(
            sid, "A", 0, [{"op": "edges_added", "edges": [edge]}]
        )

        # Fans out to the originator *and* the other client, edge payload intact.
        for sub in (sub_a, sub_b):
            events = [
                e
                for e in await _drain(sub)
                if e.get("op", {}).get("op") == "edges_added"
            ]
            assert len(events) == 1, "edges_added must reach every subscriber"
            assert events[0]["op"]["edges"] == [edge]

        # It advances the revision but stores no edge state (edges live in the
        # graph): node_refs are unchanged and there is no edge set on the session.
        assert result["applied"][0]["op"] == "edges_added"
        assert mgr.get_session(sid).state["node_refs"] == state_before
        assert "edge_refs" not in mgr.get_session(sid).state

    async def test_edges_added_requires_an_edges_list(self):
        mgr = _manager()
        sid = mgr.create_session().id
        with pytest.raises(OpError):
            await mgr.apply_ops(sid, "A", 0, [{"op": "edges_added", "node_ids": ["x"]}])

    async def test_edges_added_rejects_edges_without_a_string_id(self):
        mgr = _manager()
        sid = mgr.create_session().id
        for bad in (["e1"], [{"source": "a", "target": "b"}], [{"id": 5}]):
            with pytest.raises(OpError):
                await mgr.apply_ops(sid, "A", 0, [{"op": "edges_added", "edges": bad}])

    async def test_edge_removed_between_present_nodes_fans_out_to_all_clients(self):
        """A deleted edge must disappear for *every* connected client.

        Symmetric regression to the edges_added fan-out: an edge deleted between
        two nodes both already present triggers no node change and, without an
        explicit fan-out op, lingers on every canvas but the originator's until a
        reload. The edges_removed op carries the edge ids through to all
        subscribers while leaving session state untouched.
        """
        mgr = _manager()
        sid = mgr.create_session(name="Shared").id
        sub_a, _ = mgr.connect(sid, "A", "Alice")
        sub_b, _ = mgr.connect(sid, "B", "Bob")

        await mgr.apply_ops(
            sid, "A", 0, [{"op": "nodes_added", "node_ids": ["n1", "n2"]}]
        )
        await _drain(sub_a)
        await _drain(sub_b)
        state_before = mgr.get_session(sid).state["node_refs"][:]

        result = await mgr.apply_ops(
            sid, "A", 0, [{"op": "edges_removed", "edge_ids": ["e1"]}]
        )

        for sub in (sub_a, sub_b):
            events = [
                e
                for e in await _drain(sub)
                if e.get("op", {}).get("op") == "edges_removed"
            ]
            assert len(events) == 1, "edges_removed must reach every subscriber"
            assert events[0]["op"]["edge_ids"] == ["e1"]

        assert result["applied"][0]["op"] == "edges_removed"
        assert mgr.get_session(sid).state["node_refs"] == state_before

    async def test_edges_removed_requires_an_edge_id_list(self):
        mgr = _manager()
        sid = mgr.create_session().id
        for bad in ({"op": "edges_removed"}, {"op": "edges_removed", "edge_ids": [5]}):
            with pytest.raises(OpError):
                await mgr.apply_ops(sid, "A", 0, [bad])

    async def test_edge_updated_between_present_nodes_fans_out_to_all_clients(self):
        """A changed edge attribute must re-render for *every* connected client.

        Symmetric regression to the edges_added fan-out: an edge whose type
        changes between two nodes both already present triggers no node change
        and, without an explicit fan-out op, keeps showing the stale type on
        every canvas but the originator's until a reload. The edges_updated op
        carries the edge payload through while leaving session state untouched.
        """
        mgr = _manager()
        sid = mgr.create_session(name="Shared").id
        sub_a, _ = mgr.connect(sid, "A", "Alice")
        sub_b, _ = mgr.connect(sid, "B", "Bob")

        await mgr.apply_ops(
            sid, "A", 0, [{"op": "nodes_added", "node_ids": ["n1", "n2"]}]
        )
        await _drain(sub_a)
        await _drain(sub_b)
        state_before = mgr.get_session(sid).state["node_refs"][:]

        edge = {"id": "e1", "type": "DEPENDS_ON"}
        result = await mgr.apply_ops(
            sid, "A", 0, [{"op": "edges_updated", "edges": [edge]}]
        )

        for sub in (sub_a, sub_b):
            events = [
                e
                for e in await _drain(sub)
                if e.get("op", {}).get("op") == "edges_updated"
            ]
            assert len(events) == 1, "edges_updated must reach every subscriber"
            assert events[0]["op"]["edges"] == [edge]

        assert result["applied"][0]["op"] == "edges_updated"
        assert mgr.get_session(sid).state["node_refs"] == state_before

    async def test_edges_updated_requires_valid_edges(self):
        mgr = _manager()
        sid = mgr.create_session().id
        for bad in (
            {"op": "edges_updated", "edge_ids": ["e1"]},
            {"op": "edges_updated", "edges": [{"type": "X"}]},
            {"op": "edges_updated", "edges": [{"id": 5}]},
        ):
            with pytest.raises(OpError):
                await mgr.apply_ops(sid, "A", 0, [bad])

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

        await mgr.delete_session(sid, deleted_by="A")
        events = await _drain(sub_a)
        deleted = [e for e in events if e["type"] == "session_deleted"]
        assert deleted and deleted[0]["deleted_by"] == "A"
        assert mgr.get_session(sid) is None

    async def test_reconnect_race_does_not_clobber_roster_or_claims(self):
        """Fast reconnect: closing the old connection must not evict the client.

        Two-client session.  A's old SSE closes after the new one is open;
        B must never see a spurious presence_left for A, and A's claims must
        survive until the new SSE closes.
        """
        mgr = _manager()
        sid = mgr.create_session().id

        # A joins with old connection; B joins.
        sub_a_old, _ = mgr.connect(sid, "A", "Alice")
        sub_b, _ = mgr.connect(sid, "B", "Bob")
        await _drain(sub_a_old)
        await _drain(sub_b)

        # A claims a node.
        await mgr.apply_ops(
            sid, "A", 0, [{"op": "selection_claimed", "element_ids": ["node-1"]}]
        )
        await _drain(sub_a_old)
        await _drain(sub_b)

        # A reconnects: new SSE opens before old one closes.
        sub_a_new, _ = mgr.connect(sid, "A", "Alice")
        await _drain(sub_a_old)
        await _drain(sub_b)
        await _drain(sub_a_new)

        # Old SSE tears down.
        mgr.disconnect(sid, "A", sub_a_old)

        # B must NOT have received presence_left or selection_released for A.
        b_events = await _drain(sub_b)
        assert not any(
            e.get("type") == "presence_left" and e.get("client_id") == "A"
            for e in b_events
        )
        assert not any(
            e.get("op", {}).get("op") == "selection_released" for e in b_events
        )

        # Roster still has both A and B; claim is intact.
        roster_ids = {m["client_id"] for m in mgr.roster(sid)}
        assert roster_ids == {"A", "B"}
        assert mgr.claimed_elements(sid) == ["node-1"]

        # New SSE closes — now A truly departs.
        mgr.disconnect(sid, "A", sub_a_new)
        b_final = await _drain(sub_b)
        assert any(
            e.get("type") == "presence_left" and e.get("client_id") == "A"
            for e in b_final
        )
        assert {m["client_id"] for m in mgr.roster(sid)} == {"B"}
        assert mgr.claimed_elements(sid) == []
