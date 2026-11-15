# covers: backend/core/session_manager.py
"""Server-owned membership recording for pushes and direct writes."""

import asyncio
import threading

import pytest

from backend.core.session_activity import MAX_ACTIVITY_NODE_IDS
from backend.core.session_manager import AUTO_ADD_ACTOR_PREFIX, _TokenBucket
from backend.core.tests.test_session_manager import _manager, _drain

pytestmark = pytest.mark.asyncio


class TestServerMembership:
    @pytest.mark.parametrize(
        "node_count,exhausted", [(201, False), (501, False), (1, True)]
    )
    async def test_additive_push_does_not_charge_mcp_budget(
        self, node_count, exhausted
    ):
        mgr = _manager()
        s = mgr.create_session()
        subs = [mgr.connect(s.id, client, client)[0] for client in ("c1", "c2")]
        for sub in subs:
            await _drain(sub)
        actor = "mcp-agent"
        if exhausted:
            # Freeze refill so even the one-node push would fail if charged.
            mgr._mcp_bucket = _TokenBucket(capacity=200, refill_per_sec=0)
            assert mgr._mcp_bucket.consume(actor, 200)
        node_ids = [f"n{i}" for i in range(node_count)]
        command = {
            "tool": "add_to_visualization",
            "result": {
                "action": "add_to_visualization",
                "nodes": [{"id": node_id} for node_id in node_ids],
            },
        }

        assert mgr.push_command(s.id, command) is True

        assert s.state["node_refs"] == node_ids
        assert len(s.activity_log) == 1
        assert s.activity_log[0]["op"] == "nodes_added"
        assert s.activity_log[0]["actor"] == actor
        assert s.activity_log[0]["affected"] == {
            "kind": "nodes",
            "count": node_count,
            "ids": node_ids[:MAX_ACTIVITY_NODE_IDS],
        }
        for sub in subs:
            events = await _drain(sub)
            assert [event["type"] for event in events] == ["op", "op", "command"]
            assert events[0]["op"]["node_ids"] == node_ids
            assert events[2]["command"] == command
        # Delivery neither spends a fresh budget nor replenishes an empty one.
        assert mgr._mcp_bucket.consume(actor, 200) is (not exhausted)

    @pytest.mark.parametrize("off_loop", [False, True])
    @pytest.mark.parametrize("clear_after_add", [False, True])
    async def test_additive_push_waits_for_inflight_batch(
        self, monkeypatch, clear_after_add, off_loop
    ):
        """A push, from the loop or a worker thread, applies after the batch."""
        mgr = _manager()
        mgr.set_event_loop(asyncio.get_running_loop())
        s = mgr.create_session()
        subs = [mgr.connect(s.id, client, client)[0] for client in ("c1", "c2")]
        for sub in subs:
            await _drain(sub)
        entered = threading.Event()
        proceed = threading.Event()
        original = mgr.store.persist_snapshot

        def blocked_persist(snapshot):
            entered.set()
            assert proceed.wait(5)
            return original(snapshot)

        monkeypatch.setattr(mgr.store, "persist_snapshot", blocked_persist)
        batch = asyncio.create_task(
            mgr.apply_ops(
                s.id, "c1", None, [{"op": "nodes_added", "node_ids": ["existing"]}]
            )
        )
        command = {
            "tool": "add_to_visualization",
            "result": {
                "action": "add_to_visualization",
                "nodes": [{"id": "existing"}, {"id": "new"}, {"id": "new"}],
            },
        }
        try:
            assert await asyncio.to_thread(entered.wait, 2)

            async def push(cmd):
                if not off_loop:
                    return mgr.push_command(s.id, cmd)
                accepted = await asyncio.to_thread(mgr.push_command, s.id, cmd)
                await asyncio.sleep(0)  # the hop onto the loop
                return accepted

            assert await push(command) is True
            clear = {
                "tool": "clear_visualization",
                "result": {"action": "clear_visualization"},
            }
            if clear_after_add:
                assert await push(clear) is True
            pending = list(mgr._pending_pushes)
            assert len(pending) == (2 if clear_after_add else 1)
            for sub in subs:
                assert await _drain(sub) == []
        finally:
            proceed.set()
            await batch
        await asyncio.gather(*pending)

        assert s.state["node_refs"] == ([] if clear_after_add else ["existing", "new"])
        assert [r["actor"] for r in s.activity_log] == ["c1", "mcp-agent"] + (
            ["mcp-agent"] if clear_after_add else []
        )
        for sub in subs:
            events = await _drain(sub)
            assert [event["type"] for event in events] == (
                ["op", "op", "op", "command", "op", "command"]
                if clear_after_add
                else ["op", "op", "op", "command"]
            )
            if clear_after_add:
                assert events[4]["op"]["op"] == "nodes_removed"
                assert events[4]["seq"] == 4
                assert events[5]["command"] == clear
            assert [event["seq"] for event in events[:3]] == [1, 2, 3]
            assert events[1]["op"]["node_ids"] == ["new"]
            assert events[2]["op"]["op"] == "layout_applied"
            assert events[3]["command"] == command
        assert mgr.catch_up(s.id, 0)["seq"] == (4 if clear_after_add else 3)

    @pytest.mark.parametrize("viewers", [0, 2])
    async def test_clear_push_records_server_removal_once(self, viewers):
        mgr = _manager()
        s = mgr.create_session()
        ids = [f"n{i}" for i in range(501)]
        mgr._add_node_refs(s, "mcp-agent", ids)
        subs = [mgr.connect(s.id, f"c{i}", "Alice")[0] for i in range(viewers)]
        for sub in subs:
            await _drain(sub)
        command = {
            "tool": "clear_visualization",
            "result": {"action": "clear_visualization"},
        }
        assert mgr.push_command(s.id, command)
        assert s.state["node_refs"] == []
        record = s.activity_log[-1]
        assert (record["op"], record["actor"]) == ("nodes_removed", "mcp-agent")
        assert record["affected"] == {
            "kind": "nodes",
            "count": len(ids),
            "ids": ids[:MAX_ACTIVITY_NODE_IDS],
        }
        for sub in subs:
            events = await _drain(sub)
            assert [event["type"] for event in events] == ["op", "command"]
            assert events[0]["op"]["node_ids"] == ids
        # Clearing an empty view changes nothing and records nothing.
        assert mgr.push_command(s.id, command)
        assert len(s.activity_log) == 2

    @pytest.mark.parametrize(
        "source,tool,action,expected,record",
        [
            ("push", "get_saved_view", "load_visualization", ["new"], "nodes_replaced"),
            (
                "push",
                "get_saved_view",
                "replace_visualization",
                ["new"],
                "nodes_replaced",
            ),
            (
                "push",
                "search_graph",
                "update_in_visualization",
                ["old", "new"],
                "nodes_added",
            ),
            (
                "push",
                "session_auto_add_agent",
                "add_to_visualization",
                ["old", "new"],
                "nodes_added",
            ),
            ("browser", None, "nodes_replaced", ["new"], "nodes_replaced"),
        ],
    )
    async def test_membership_change_is_one_record_whatever_the_viewers(
        self, source, tool, action, expected, record
    ):
        """A saved-view load or auto-add is one op and one record, by its author."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.add_node_refs(s.id, "owner", ["old"])
        mgr.apply_layout(s.id, "owner", positions={"old": {"x": 1, "y": 2}})
        browsers = [f"browser{i}" for i in range(3)]
        subs = [mgr.connect(s.id, b, b)[0] for b in browsers]
        for sub in subs:
            await _drain(sub)
        if source == "browser":
            actor = "browser0"
            await mgr.apply_ops(
                s.id, actor, None, [{"op": action, "node_ids": ["new", "new"]}]
            )
        else:
            actor = (
                f"{AUTO_ADD_ACTOR_PREFIX}agent-1"
                if tool == "session_auto_add_agent"
                else "mcp-agent"
            )
            result = {
                "action": action,
                "nodes": [{"id": "new"}, {"id": "c", "type": "Community"}],
                "agent_id": "agent-1",
            }
            assert mgr.push_command(s.id, {"tool": tool, "result": result})
        assert s.state["node_refs"] == expected
        assert ("old" in s.state["positions"]) == ("old" in expected)
        # A pushed add is placed once, here, not by each viewer saving its own.
        placed = source == "push" and record == "nodes_added"
        assert s.state["positions"].get("new") == (
            {"x": 281.0, "y": 2.0} if placed else None
        )
        assert [(r["op"], r["actor"]) for r in s.activity_log[2:]] == [(record, actor)]
        for sub in subs:
            events = await _drain(sub)
            ops = [e["op"]["op"] for e in events if e["type"] == "op"]
            assert ops == [record] + (["layout_applied"] if placed else [])

    @pytest.mark.parametrize("via", ["push", "browser"])
    async def test_saved_view_load_stores_its_layout_in_its_one_record(self, via):
        """Viewers render a load's layout from the session; only the replace records."""
        mgr = _manager()
        s = mgr.create_session()
        mgr.add_node_refs(s.id, "owner", ["a", "b"])
        mgr.apply_layout(s.id, "owner", positions={"a": {"x": 5, "y": 5}})
        mgr.apply_layout(s.id, "owner", positions={"b": {"x": 7, "y": 7}})
        mgr.store.apply_state_op(s, {"op": "nodes_hidden", "node_ids": ["a"]})
        for ann_id in ("kept", "dropped"):
            mgr.store.apply_state_op(
                s,
                {
                    "op": "annotation_created",
                    "annotation": {"id": ann_id, "type": "note"},
                },
            )
        subs = [mgr.connect(s.id, f"c{i}", "Alice")[0] for i in range(2)]
        for sub in subs:
            await _drain(sub)
        records = len(s.activity_log)
        result = {
            "action": "load_visualization",
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "positions": {
                "a": {"x": 1, "y": 2},
                "b": {"x": 7, "y": 7},
                "c": {"x": 3, "y": 4},
                "group-1": {"x": 0, "y": 0},
                "gone": {"x": 9, "y": 9},
            },
            "hidden_node_ids": ["c", "gone"],
        }
        view_annotations = [
            {"id": "kept", "type": "note"},
            {"id": "new", "type": "note", "text": "x"},
            {"id": "bad", "type": "nonsense"},
        ]
        if via == "push":
            actor, fan_out = "mcp-agent", "mcp-agent"
            assert mgr.push_command(s.id, {"tool": "get_saved_view", "result": result})
        else:
            actor, fan_out = "c0", "view-load"
            op = {
                "op": "nodes_replaced",
                "node_ids": [n["id"] for n in result["nodes"]],
                "positions": result["positions"],
                "hidden_node_ids": result["hidden_node_ids"],
                "annotations": view_annotations,
            }
            await mgr.apply_ops(s.id, actor, None, [op])

        assert s.state["positions"] == {
            "a": {"x": 1.0, "y": 2.0},
            "b": {"x": 7.0, "y": 7.0},
            "c": {"x": 3.0, "y": 4.0},
        }
        assert s.state["hidden_node_ids"] == ["c"]
        # A browser's load replaces the overlays too; a push leaves them.
        assert [a["id"] for a in s.state["annotations"]] == (
            ["kept", "dropped"] if via == "push" else ["kept", "new"]
        )
        assert [(r["op"], r["actor"]) for r in s.activity_log[records:]] == [
            ("nodes_replaced", actor)
        ]
        layout = ["layout_applied", "nodes_shown", "nodes_hidden"]
        for sub in subs:
            events = await _drain(sub)
            assert [
                (e["op"]["op"], e["client_id"]) if e["type"] == "op" else e["type"]
                for e in events
            ] == [("nodes_replaced", actor)] + [(o, fan_out) for o in layout] + (
                ["command"]
                if via == "push"
                else [("annotation_deleted", fan_out), ("annotation_created", fan_out)]
            )
            assert events[1]["op"]["positions"] == {
                "a": {"x": 1.0, "y": 2.0},
                "c": {"x": 3.0, "y": 4.0},
            }

    async def test_direct_user_and_mcp_changes_keep_their_actor(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.connect(s.id, "user", "Alice")
        for actor in ("user", "mcp-agent"):
            mgr.add_node_refs(s.id, actor, [actor, actor])
            mgr.add_node_refs(s.id, actor, [actor])
            mgr.remove_node_refs(s.id, actor, [actor, actor])
            mgr.remove_node_refs(s.id, actor, [actor])
        assert [(r["op"], r["actor"], r["affected"]) for r in s.activity_log] == [
            (op, actor, {"kind": "nodes", "count": 1, "ids": [actor]})
            for actor in ("user", "mcp-agent")
            for op in ("nodes_added", "nodes_removed")
        ]
