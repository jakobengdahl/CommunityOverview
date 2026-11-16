# covers: backend/core/session_manager.py
"""Server-owned membership recording for pushes and direct writes."""

import asyncio
import copy
import threading

import pytest

from backend.core.session_activity import MAX_ACTIVITY_NODE_IDS
from backend.core.image_ingest import (
    DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES,
    DEFAULT_MAX_SESSION_IMAGE_BYTES,
)
from backend.core.session_manager import (
    AUTO_ADD_ACTOR_PREFIX,
    ImageBudgetExceeded,
    LeaseConflict,
    _TokenBucket,
)
from backend.core.session_store import OpError
from backend.core.tests.test_session_manager import (
    _drain,
    _image_annotation,
    _manager,
)

pytestmark = pytest.mark.asyncio

# The largest padding-free image size within the per-image cap.
_LARGEST_EXACT_IMAGE = DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES // 3 * 3


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
                ["op", "op", "command", "op", "command"]
                if clear_after_add
                else ["op", "op", "command"]
            )
            if clear_after_add:
                assert events[3]["op"]["op"] == "nodes_removed"
                assert events[3]["seq"] == 3
                assert events[4]["command"] == clear
            assert [event["seq"] for event in events[:2]] == [1, 2]
            assert events[1]["op"]["node_ids"] == ["new"]
            assert events[2]["command"] == command
        assert mgr.catch_up(s.id, 0)["seq"] == (3 if clear_after_add else 2)

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
        # A node is placed once, by the server in the same op, not by each
        # viewer saving its own.
        placed = (
            {"x": 281.0, "y": 2.0}
            if record == "nodes_added"
            else {"x": 400.0, "y": 300.0}
        )
        assert s.state["positions"]["new"] == placed
        assert [(r["op"], r["actor"]) for r in s.activity_log[2:]] == [(record, actor)]
        for sub in subs:
            events = await _drain(sub)
            ops = [e["op"] for e in events if e["type"] == "op"]
            assert [op["op"] for op in ops] == [record]
            assert ops[0]["positions"]["new"] == placed

    @staticmethod
    async def _load_view(mgr, s, via, view):
        """Load a saved view as a push (get_saved_view) or a browser's replace."""
        ids = ["a", "b", "c"]
        if via == "push":
            result = {
                "action": "load_visualization",
                "nodes": [{"id": n} for n in ids],
                "positions": view["positions"],
                "hidden_node_ids": view["hidden_node_ids"],
                "annotation_document": {
                    "schema_version": 1,
                    "annotations": view["annotations"],
                },
            }
            assert mgr.push_command(s.id, {"tool": "get_saved_view", "result": result})
            return "mcp-agent"
        op = {"op": "nodes_replaced", "node_ids": ids, **view}
        await mgr.apply_ops(s.id, "c0", None, [op])
        return "c0"

    @staticmethod
    def _seeded_session():
        mgr = _manager()
        s = mgr.create_session()
        mgr.add_node_refs(s.id, "owner", ["a", "b", "d"])
        mgr.apply_layout(s.id, "owner", positions={"a": {"x": 5, "y": 5}})
        mgr.store.apply_state_op(s, {"op": "nodes_hidden", "node_ids": ["a"]})
        for ann_id in ("kept", "dropped"):
            mgr.store.apply_state_op(
                s,
                {
                    "op": "annotation_created",
                    "annotation": {"id": ann_id, "type": "note"},
                },
            )
        return mgr, s

    @pytest.mark.parametrize("via", ["push", "browser"])
    async def test_saved_view_load_is_one_op_and_one_record(self, via):
        """Membership, positions, hidden ids, groups and annotations: one op."""
        mgr, s = self._seeded_session()
        subs = [mgr.connect(s.id, f"c{i}", "Alice")[0] for i in range(2)]
        for sub in subs:
            await _drain(sub)
        records = len(s.activity_log)
        group = {"id": "g", "type": "group", "member_node_ids": ["a", "d"]}
        view = {
            "positions": {"a": {"x": 1, "y": 2}, "gone": {"x": 9, "y": 9}, "c": None},
            "hidden_node_ids": ["c", "gone"],
            "annotations": [{"id": "kept", "type": "note"}, group],
        }
        actor = await self._load_view(mgr, s, via, view)

        assert s.state["node_refs"] == ["a", "b", "c"]
        # b keeps its place; c, with none, is placed by the server.
        assert s.state["positions"] == {
            "a": {"x": 1.0, "y": 2.0},
            "b": {"x": 281.0, "y": 2.0},
            "c": {"x": 561.0, "y": 2.0},
        }
        assert s.state["hidden_node_ids"] == ["c"]
        assert [
            (a["id"], a.get("member_node_ids")) for a in s.state["annotations"]
        ] == [
            ("kept", None),
            ("g", ["a"]),
        ]
        assert [(r["op"], r["actor"]) for r in s.activity_log[records:]] == [
            ("nodes_replaced", actor)
        ]
        for sub in subs:
            events = await _drain(sub)
            assert [e["type"] for e in events] == ["op"] + (
                ["command"] if via == "push" else []
            )
            op = events[0]["op"]
            assert (op["op"], events[0]["client_id"]) == ("nodes_replaced", actor)
            assert op["positions"] == s.state["positions"]
            assert op["hidden_node_ids"] == ["c"]
            assert op["annotations"] == s.state["annotations"]
            assert op["removed_annotation_ids"] == ["dropped"]

        # Reloading a view brings back an annotation an earlier load deleted.
        await self._load_view(
            mgr, s, via, {**view, "annotations": [{"id": "dropped", "type": "note"}]}
        )
        assert [a["id"] for a in s.state["annotations"]] == ["dropped"]

    @pytest.mark.parametrize("via", ["push", "browser"])
    async def test_view_load_that_cannot_apply_applies_nothing(self, via):
        """One unreadable part refuses the whole load: no state, record or event."""
        mgr, s = self._seeded_session()
        sub = mgr.connect(s.id, "c0", "Alice")[0]
        await _drain(sub)
        before = copy.deepcopy((s.state, s.seq, s.activity_log))
        view = {
            "positions": {"a": {"x": 1, "y": 2}},
            "hidden_node_ids": [],
            "annotations": [{"id": "new", "type": "note"}, {"id": "x", "type": "nope"}],
        }
        with pytest.raises(OpError):
            await self._load_view(mgr, s, via, view)
        assert (s.state, s.seq, s.activity_log) == before
        assert await _drain(sub) == []

    @pytest.mark.parametrize("via", ["push", "browser"])
    @pytest.mark.parametrize("over", ["per_image", "session"])
    async def test_view_load_is_held_to_the_image_budgets(self, via, over):
        """One image over the per-image cap, or images each within it but
        together over the session budget, refuse a view load on both paths."""
        mgr, s = self._seeded_session()
        sub = mgr.connect(s.id, "c0", "Alice")[0]
        await _drain(sub)
        before = copy.deepcopy((s.state, s.seq, s.activity_log))
        if over == "per_image":
            sizes = [_LARGEST_EXACT_IMAGE + 3]
        else:
            count = DEFAULT_MAX_SESSION_IMAGE_BYTES // _LARGEST_EXACT_IMAGE
            sizes = [_LARGEST_EXACT_IMAGE] * (count + 1)
        images = [
            _image_annotation(f"img{i}", data_bytes=n) for i, n in enumerate(sizes)
        ]
        view = {"positions": {}, "hidden_node_ids": [], "annotations": images}
        with pytest.raises(ImageBudgetExceeded):
            await self._load_view(mgr, s, via, view)
        assert (s.state, s.seq, s.activity_log) == before
        assert await _drain(sub) == []

    @pytest.mark.parametrize("via", ["push", "browser"])
    async def test_view_load_with_an_image_within_the_per_image_cap_applies(self, via):
        """An image larger than the flat op-batch cap but within the per-image
        cap is admitted on both paths."""
        mgr, s = self._seeded_session()
        image = _image_annotation("img", data_bytes=_LARGEST_EXACT_IMAGE)
        view = {"positions": {}, "hidden_node_ids": [], "annotations": [image]}
        await self._load_view(mgr, s, via, view)
        assert [a["id"] for a in s.state["annotations"]] == ["img"]

    @pytest.mark.parametrize("via", ["push", "browser"])
    async def test_view_load_cannot_change_an_annotation_leased_by_another(self, via):
        """Dropping a field or changing the type of a leased annotation conflicts;
        an id listed twice is applied once, as its last entry."""
        mgr, s = self._seeded_session()
        for client in ("c0", "c9"):
            mgr.connect(s.id, client, client)
        leased = {"id": "leased", "type": "note", "text": "hi"}
        mgr.store.apply_state_op(s, {"op": "annotation_created", "annotation": leased})
        await mgr.apply_ops(
            s.id, "c9", 0, [{"op": "edit_lease_acquired", "element_ids": ["leased"]}]
        )
        view = {"positions": {}, "hidden_node_ids": []}
        before = copy.deepcopy((s.state, s.seq, s.activity_log))
        for changed in ({"id": "leased", "type": "note"}, {**leased, "type": "label"}):
            with pytest.raises(LeaseConflict):
                await self._load_view(mgr, s, via, {**view, "annotations": [changed]})
            assert (s.state, s.seq, s.activity_log) == before

        held = copy.deepcopy(s.state["annotations"][-1])
        twice = [{**leased, "text": "other"}, leased]
        await self._load_view(mgr, s, via, {**view, "annotations": twice})
        assert s.state["annotations"] == [held]

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
