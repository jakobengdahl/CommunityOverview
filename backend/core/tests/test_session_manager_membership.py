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
            assert [event["type"] for event in events] == ["op", "command"]
            assert events[0]["op"]["node_ids"] == node_ids
            assert events[1]["command"] == command
        # Delivery neither spends a fresh budget nor replenishes an empty one.
        assert mgr._mcp_bucket.consume(actor, 200) is (not exhausted)

    @pytest.mark.parametrize("clear_after_add", [False, True])
    async def test_additive_push_waits_for_inflight_batch(
        self, monkeypatch, clear_after_add
    ):
        mgr = _manager()
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
            assert mgr.push_command(s.id, command) is True
            clear = {
                "tool": "clear_visualization",
                "result": {"action": "clear_visualization"},
            }
            if clear_after_add:
                assert mgr.push_command(s.id, clear) is True
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
                ["op", "op", "command", "op", "command"]
                if clear_after_add
                else ["op", "op", "command"]
            )
            if clear_after_add:
                assert events[3]["op"]["op"] == "nodes_replaced"
                assert events[3]["seq"] == 3
                assert events[4]["command"] == clear
            assert [event["seq"] for event in events[:2]] == [1, 2]
            assert events[1]["op"]["node_ids"] == ["new"]
            assert events[2]["command"] == command
        assert mgr.catch_up(s.id, 0)["seq"] == (3 if clear_after_add else 2)

    @pytest.mark.parametrize("viewers", [0, 2])
    async def test_clear_push_is_one_replace_record(self, viewers):
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
        assert (record["op"], record["actor"]) == ("nodes_replaced", "mcp-agent")
        assert record["affected"] == {"kind": "nodes", "count": 0, "ids": []}
        for sub in subs:
            events = await _drain(sub)
            assert [event["type"] for event in events] == ["op", "command"]
            assert events[0]["op"]["node_ids"] == []
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
        assert [(r["op"], r["actor"]) for r in s.activity_log[2:]] == [(record, actor)]
        for sub in subs:
            events = await _drain(sub)
            ops = [e["op"]["op"] for e in events if e["type"] == "op"]
            assert ops == [record]

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
