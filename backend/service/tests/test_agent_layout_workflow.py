"""
End-to-end verification of the agent-driven layout workflow against the MCP
visualization-layout contract (``docs/MCP_VISUALIZATION_LAYOUT_CONTRACT.md``).

Where ``test_mcp_layout_tools.py`` unit-tests each tool in isolation, this module
proves the *workflow* an MCP-connected assistant actually runs: read the session
geometry, compute a collision-free left-to-right DAG layout from the advertised
node size, and apply it as one atomic animated batch — then it checks the
contract invariants that make that workflow reliable (§2 coordinate model, §3
collision spacing, §5-§7 movement/atomicity/concurrency, §8 reserved
locks/viewport, §9-§10 animation seam and broadcast shape, §11 errors, §12
reversibility).

The ``AgentLayoutClient`` below is the "agent fixture" the contract's test task
calls for: it reads dimensions, calculates a left-to-right DAG layout and applies
it, using only what the MCP tools return (never hard-coding the node size).
"""

import os
from collections import defaultdict, deque
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_manager import SessionManager, _TokenBucket
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.service import GraphService, register_mcp_tools


@pytest.fixture
def layout_tools(tmp_path):
    """tools_map wired to an in-memory shared-session manager, plus that manager."""
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


def _session_with_nodes(manager, node_ids):
    session = manager.create_session()
    manager.store.apply_state_op(session, {"op": "nodes_added", "node_ids": node_ids})
    manager.store.persist(session)
    return session


# --------------------------------------------------------------------------- #
# The agent fixture: reads geometry, computes a left-to-right DAG, applies it.
# --------------------------------------------------------------------------- #


class AgentLayoutClient:
    """A minimal stand-in for an MCP-connected assistant arranging a session.

    It talks only through the two layout tools and derives spacing from the
    server-advertised ``assumed_node_size`` (contract §3) — it never hard-codes a
    node dimension, so if the default box changes the agent still spaces cleanly.
    """

    def __init__(self, tools_map, session_id):
        self.tools = tools_map
        self.session_id = session_id

    def read(self):
        return self.tools["get_visualization_layout"](session_id=self.session_id)

    def left_to_right_dag(self, edges, *, gap=60):
        """Compute + apply a left-to-right DAG layout. Returns (result, plan)."""
        layout = self.read()
        size = layout["assumed_node_size"]
        node_ids = [n["id"] for n in layout["nodes"]]

        out = defaultdict(list)
        indeg = {n: 0 for n in node_ids}
        for src, dst in edges:
            out[src].append(dst)
            indeg[dst] += 1

        # Longest-path rank via Kahn's algorithm — rank = distance from a root, so
        # every edge points strictly rightwards (child rank > parent rank).
        rank = {n: 0 for n in node_ids}
        queue = deque([n for n in node_ids if indeg[n] == 0])
        while queue:
            u = queue.popleft()
            for v in out[u]:
                rank[v] = max(rank[v], rank[u] + 1)
                indeg[v] -= 1
                if indeg[v] == 0:
                    queue.append(v)

        step_x = size["width"] + gap
        step_y = size["height"] + gap
        slot = defaultdict(int)
        positions = {}
        for node_id in node_ids:  # stable read order → deterministic y-stacking
            r = rank[node_id]
            positions[node_id] = {"x": r * step_x, "y": slot[r] * step_y}
            slot[r] += 1

        result = self.tools["apply_visualization_layout"](
            session_id=self.session_id,
            positions=positions,
            expected_revision=layout["revision"],
        )
        return result, {"positions": positions, "rank": rank, "size": size}


def _boxes_overlap(a, b, size):
    """True when two top-left-anchored boxes of ``size`` overlap (contract §3)."""
    return (
        abs(a["x"] - b["x"]) < size["width"] and abs(a["y"] - b["y"]) < size["height"]
    )


# --------------------------------------------------------------------------- #
# §2-§4 — coordinate model and the read projection
# --------------------------------------------------------------------------- #


class TestGeometryModel:
    def test_unset_positions_are_null_not_origin(self, layout_tools):
        # §2: a node added but never positioned reads null, not (0, 0).
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a", "b"])
        layout = tools_map["get_visualization_layout"](session_id=session.id)
        for node in layout["nodes"]:
            assert node["x"] is None and node["y"] is None

    def test_read_projection_shape(self, layout_tools):
        # §4: the documented fields are present; viewport is deliberately absent (§8).
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        layout = tools_map["get_visualization_layout"](session_id=session.id)
        for field in (
            "session_id",
            "revision",
            "node_count",
            "nodes",
            "assumed_node_size",
            "coordinate_space",
            "connected_clients",
        ):
            assert field in layout
        assert "viewport" not in layout
        assert set(layout["assumed_node_size"]) == {"width", "height"}

    def test_positions_are_zoom_independent_model_space(self, layout_tools):
        # §2: the tool takes no zoom/viewport input and echoes back exactly the
        # model-space numbers written — reading "at a different zoom" is the same
        # read, so the coordinates cannot drift with a client's zoom.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 137, "y": 42}}
        )
        first = tools_map["get_visualization_layout"](session_id=session.id)
        second = tools_map["get_visualization_layout"](session_id=session.id)
        assert first["nodes"] == second["nodes"]
        node = {n["id"]: n for n in first["nodes"]}["a"]
        assert node["x"] == 137.0 and node["y"] == 42.0
        assert "top-left" in first["coordinate_space"]


# --------------------------------------------------------------------------- #
# §3, §5, §6 — the DAG agent workflow: collision-free, left-to-right, atomic
# --------------------------------------------------------------------------- #


class TestAgentDagLayout:
    def _diamond(self, manager):
        # a -> b, a -> c, b -> d, c -> d  (ranks: a=0, b=c=1, d=2)
        session = _session_with_nodes(manager, ["a", "b", "c", "d"])
        return session, [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]

    def test_agent_produces_left_to_right_ranks(self, layout_tools):
        tools_map, manager = layout_tools
        session, edges = self._diamond(manager)
        agent = AgentLayoutClient(tools_map, session.id)
        result, plan = agent.left_to_right_dag(edges)

        assert result["success"] is True
        assert result["moved"] == 4
        pos = plan["positions"]
        # Every edge points strictly rightwards: child x > parent x (§ left-to-right).
        for src, dst in edges:
            assert pos[dst]["x"] > pos[src]["x"]
        # The two mid-rank nodes share a column but are stacked, not co-located.
        assert pos["b"]["x"] == pos["c"]["x"]
        assert pos["b"]["y"] != pos["c"]["y"]

    def test_agent_layout_is_collision_free(self, layout_tools):
        # §3: spacing derived from assumed_node_size leaves no overlapping boxes.
        tools_map, manager = layout_tools
        session, edges = self._diamond(manager)
        agent = AgentLayoutClient(tools_map, session.id)
        _, plan = agent.left_to_right_dag(edges)
        pos, size = plan["positions"], plan["size"]
        ids = list(pos)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                assert not _boxes_overlap(pos[ids[i]], pos[ids[j]], size), (
                    f"{ids[i]} and {ids[j]} overlap"
                )

    def test_batch_is_one_atomic_revision_bump(self, layout_tools):
        # §6: the whole arrange is a single layout_applied op — seq advances once,
        # not once per node, so browsers see one transition.
        tools_map, manager = layout_tools
        session, edges = self._diamond(manager)
        before = tools_map["get_visualization_layout"](session_id=session.id)[
            "revision"
        ]
        agent = AgentLayoutClient(tools_map, session.id)
        result, _ = agent.left_to_right_dag(edges)
        assert result["revision"] == before + 1

    def test_large_batch_applies_atomically(self, layout_tools):
        # §6: a large arrange lands as one op (one seq bump), not one per node.
        # The size here (200) is the default per-client token-bucket burst — the
        # largest single write the rate limiter allows before an agent must pace,
        # which is a tighter bound than the 500-node hard cap (see too_large).
        tools_map, manager = layout_tools
        ids = [f"n{i}" for i in range(200)]
        session = _session_with_nodes(manager, ids)
        before = session.seq
        positions = {n: {"x": i * 10, "y": 0} for i, n in enumerate(ids)}
        result = tools_map["apply_visualization_layout"](
            session_id=session.id, positions=positions
        )
        assert result["success"] is True
        assert result["moved"] == 200
        assert result["revision"] == before + 1


# --------------------------------------------------------------------------- #
# §7 — optimistic concurrency (stale revision)
# --------------------------------------------------------------------------- #


class TestConcurrency:
    def test_stale_revision_is_rejected_and_agent_can_re_read(self, layout_tools):
        # §7: an agent holding an old revision loses to a concurrent write and
        # gets the current revision back so it can re-read and retry.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        stale = tools_map["get_visualization_layout"](session_id=session.id)["revision"]
        # Someone else moves the node, advancing the revision.
        tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 1, "y": 1}}
        )
        conflict = tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={"a": {"x": 9, "y": 9}},
            expected_revision=stale,
        )
        assert conflict["success"] is False
        assert conflict["error"] == "revision_conflict"
        # Re-read gives the fresh revision; retry with it succeeds.
        fresh = tools_map["get_visualization_layout"](session_id=session.id)["revision"]
        retry = tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={"a": {"x": 9, "y": 9}},
            expected_revision=fresh,
        )
        assert retry["success"] is True
        assert manager.get_session(session.id).state["positions"]["a"] == {
            "x": 9.0,
            "y": 9.0,
        }


# --------------------------------------------------------------------------- #
# §8 — reserved semantics: locks not enforced, hidden reported, no viewport
# --------------------------------------------------------------------------- #


class TestReservedSemantics:
    def test_no_locked_flag_and_moves_are_never_lock_blocked(self, layout_tools):
        # §8: locking is reserved/client-side — the read exposes no locked field
        # and a write is never rejected on lock grounds.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        layout = tools_map["get_visualization_layout"](session_id=session.id)
        assert all("locked" not in node for node in layout["nodes"])
        moved = tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 3, "y": 4}}
        )
        assert moved["success"] is True

    def test_hidden_nodes_are_reported_and_still_movable(self, layout_tools):
        # §8: hidden state is surfaced; a hidden node can still be repositioned.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a", "b"])
        manager.store.apply_state_op(session, {"op": "nodes_hidden", "node_ids": ["b"]})
        manager.store.persist(session)
        layout = tools_map["get_visualization_layout"](session_id=session.id)
        assert {n["id"]: n["hidden"] for n in layout["nodes"]} == {
            "a": False,
            "b": True,
        }
        result = tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"b": {"x": 7, "y": 8}}
        )
        assert result["success"] is True


# --------------------------------------------------------------------------- #
# §9-§10 — animation seam and broadcast op shape
# --------------------------------------------------------------------------- #


class TestAnimationBroadcast:
    def test_broadcast_shape_matches_contract(self, layout_tools):
        # §10: one op event, client_id mcp-agent, op layout_applied, absolute
        # positions, animation hint carried, seq == new revision.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        sub = manager.bus.subscribe(session.id)
        result = tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={"a": {"x": 12, "y": 34}},
            animate=True,
            duration_ms=250,
            easing="ease-in-out",
        )
        event = sub.queue.get_nowait()
        assert event["type"] == "op"
        assert event["client_id"] == "mcp-agent"
        assert event["op"]["op"] == "layout_applied"
        assert event["op"]["positions"]["a"] == {"x": 12.0, "y": 34.0}
        assert event["op"]["animation"] == {
            "animate": True,
            "duration_ms": 250,
            "easing": "ease-in-out",
        }
        assert event["op"]["seq"] == result["revision"]
        assert event["seq"] == result["revision"]

    def test_deltas_are_resolved_to_absolute_in_broadcast(self, layout_tools):
        # §10: consumers must all apply the same coordinates, so deltas are
        # resolved server-side before broadcast.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 100, "y": 100}}
        )
        sub = manager.bus.subscribe(session.id)
        tools_map["apply_visualization_layout"](
            session_id=session.id, deltas={"a": {"dx": 5, "dy": -20}}
        )
        event = sub.queue.get_nowait()
        assert event["op"]["positions"]["a"] == {"x": 105.0, "y": 80.0}

    def test_animate_false_hint_still_carried(self, layout_tools):
        # §9: reduced motion is a *client* decision; the tool always carries the
        # hint it was given (here animate=False) rather than deciding for the client.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        sub = manager.bus.subscribe(session.id)
        tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 1, "y": 1}}, animate=False
        )
        event = sub.queue.get_nowait()
        assert event["op"]["animation"]["animate"] is False


# --------------------------------------------------------------------------- #
# §11 — error model
# --------------------------------------------------------------------------- #


class TestErrorModel:
    def test_neither_positions_nor_deltas(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        result = tools_map["apply_visualization_layout"](session_id=session.id)
        assert result["success"] is False

    def test_both_positions_and_deltas(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        result = tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={"a": {"x": 1, "y": 1}},
            deltas={"a": {"dx": 1, "dy": 1}},
        )
        assert result["success"] is False

    def test_non_numeric_coordinate_rejected(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        result = tools_map["apply_visualization_layout"](
            session_id=session.id, deltas={"a": {"dx": "east", "dy": 1}}
        )
        assert result["success"] is False

    def test_over_node_cap_is_too_large(self, layout_tools):
        tools_map, manager = layout_tools
        ids = [f"n{i}" for i in range(501)]
        session = _session_with_nodes(manager, ids)
        result = tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={n: {"x": 0, "y": 0} for n in ids},
        )
        assert result["success"] is False
        assert result["error"] == "too_large"

    def test_rate_limited_is_reported(self, layout_tools):
        # An exhausted token bucket surfaces a retryable rate_limited error (§11).
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        manager._bucket = _TokenBucket(0.0, 0.0)  # no tokens, no refill
        result = tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 1, "y": 1}}
        )
        assert result["success"] is False
        assert result["error"] == "rate_limited"

    def test_unknown_and_invalid_sessions(self, layout_tools):
        tools_map, _ = layout_tools
        assert (
            tools_map["apply_visualization_layout"](
                session_id="nope", positions={"a": {"x": 1, "y": 1}}
            )["success"]
            is False
        )
        assert (
            tools_map["apply_visualization_layout"](
                session_id="9999-9999", positions={"a": {"x": 1, "y": 1}}
            )["success"]
            is False
        )


# --------------------------------------------------------------------------- #
# §12 — reversibility (undo without a server-side undo payload)
# --------------------------------------------------------------------------- #


class TestReversibility:
    def test_read_then_write_restores_prior_positions(self, layout_tools):
        # §12: capture positions, arrange, then write the captured ones back.
        tools_map, manager = layout_tools
        session, edges = (
            _session_with_nodes(manager, ["a", "b", "c", "d"]),
            [
                ("a", "b"),
                ("a", "c"),
                ("b", "d"),
                ("c", "d"),
            ],
        )
        # Give every node a known starting position first.
        start = {n: {"x": float(i), "y": float(i)} for i, n in enumerate("abcd")}
        tools_map["apply_visualization_layout"](session_id=session.id, positions=start)
        before = tools_map["get_visualization_layout"](session_id=session.id)
        captured = {n["id"]: {"x": n["x"], "y": n["y"]} for n in before["nodes"]}

        AgentLayoutClient(tools_map, session.id).left_to_right_dag(edges)
        # Restore.
        restore = tools_map["apply_visualization_layout"](
            session_id=session.id, positions=captured
        )
        assert restore["success"] is True
        after = tools_map["get_visualization_layout"](session_id=session.id)
        assert {n["id"]: {"x": n["x"], "y": n["y"]} for n in after["nodes"]} == captured

    def test_negated_deltas_reverse_a_move(self, layout_tools):
        # §12: because deltas are additive, the negated delta undoes the move.
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 50, "y": 50}}
        )
        tools_map["apply_visualization_layout"](
            session_id=session.id, deltas={"a": {"dx": 15, "dy": -25}}
        )
        tools_map["apply_visualization_layout"](
            session_id=session.id, deltas={"a": {"dx": -15, "dy": 25}}
        )
        assert manager.get_session(session.id).state["positions"]["a"] == {
            "x": 50.0,
            "y": 50.0,
        }
