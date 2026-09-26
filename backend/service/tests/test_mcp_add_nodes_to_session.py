"""
Tests for the ``add_nodes_to_session`` MCP tool.

The tool places a known set of nodes on a session's canvas by id, instead of
requiring a search whose results happen to be exactly that set. It is a thin
wrapper over ``SessionManager.add_node_refs``; the op semantics themselves are
covered in ``backend/core/tests/test_session_manager.py``.
"""

import inspect
import json
import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage, Node
from backend.core.session_auto_add import SessionAutoAddRegistry
from backend.core.session_manager import SessionManager, SessionNotFound, _TokenBucket
from backend.core.session_registry import SessionRegistry
from backend.core.tests.rate_buckets import bucket_attrs
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.runtime.authorization import (
    AUTHORIZATION_MODE_ENV,
    DefaultGraphAuthorizationHook,
)
from backend.service import GraphService, register_mcp_tools
from backend.service.tests.test_authorization import (
    ActionScopedNarrowingHook,
    FixedNarrowingHook,
    _make_multi_graph_service,
)


def _wire(storage, service, **manager_kwargs):
    manager = SessionManager(
        SessionStore(InMemorySessionPersistenceBackend()), **manager_kwargs
    )
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


@pytest.fixture
def tools(tmp_path):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    tools_map, manager = _wire(storage, service)
    tools_map["add_nodes"](
        nodes=[
            {"id": "alpha", "type": "Initiative", "name": "Alpha"},
            {"id": "beta", "type": "Actor", "name": "Beta"},
            {"id": "gamma", "type": "Actor", "name": "Gamma"},
        ],
        edges=[],
    )
    return tools_map, manager


def _session(manager):
    return manager.create_session().id


class _EqualToAlpha:
    def __eq__(self, other):
        return other == "alpha" or isinstance(other, _EqualToAlpha)

    def __hash__(self):
        return hash("alpha")

    def __str__(self):
        raise _NoStringForm()


class _UnhashingUnprintable:
    def __hash__(self):
        raise ValueError("no hash")

    def __str__(self):
        raise _NoStringForm()


class _HashableDict(dict):
    def __hash__(self):
        return 1


class _UnhashingDict(dict):
    def __hash__(self):
        raise ValueError("no hash")


class _RecordingBucket:
    def __init__(self):
        self.consumed = []
        self.keys = []

    def consume(self, key, amount):
        self.consumed.append(amount)
        self.keys.append(key)
        return True


def _record_every_bucket(manager):
    buckets = {attr: _RecordingBucket() for attr in bucket_attrs(manager)}
    for attr, bucket in buckets.items():
        setattr(manager, attr, bucket)
    return buckets


class _NoStringForm(Exception):
    pass


class _Unprintable:
    __hash__ = None

    def __str__(self):
        raise _NoStringForm()


class TestAddNodesToSession:
    def test_named_nodes_become_the_sessions_nodes(self, tools):
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "beta"]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha", "beta"]
        assert result["node_count"] == 2
        assert manager.get_session(sid).state["node_refs"] == ["alpha", "beta"]

    def test_node_count_means_the_same_in_every_session_tool(self, tools):
        """``node_count`` counts the session's node references, hidden included.

        get_visualization_session_state used to report the visible count under
        the same name, so an agent chaining the tools saw two numbers for one
        session. The visible count has its own field there instead.

        ``nodes_hidden`` accepts an id the session does not reference, so the
        hidden list also carries one: a count built from the visible and hidden
        lists instead of from ``node_refs`` then comes out one too high.
        """
        tools_map, manager = tools
        sid = _session(manager)
        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha", "beta"])
        session = manager.get_session(sid)
        manager.store.apply_state_op(
            session, {"op": "nodes_hidden", "node_ids": ["beta", "not-referenced"]}
        )
        manager.store.persist(session)
        assert "not-referenced" in session.state["hidden_node_ids"]

        added = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["gamma"])
        state = tools_map["get_visualization_session_state"](session_id=sid)
        layout = tools_map["get_visualization_layout"](session_id=sid)
        resource = tools_map["get_visualization_session"](session_id=sid)

        assert added["node_count"] == 3
        assert state["node_count"] == 3
        assert layout["node_count"] == 3
        assert resource["session"]["node_count"] == 3
        assert state["visible_node_count"] == 2
        assert state["visible_node_count"] == len(state["visible_node_ids"])

    def test_adding_is_additive_and_never_duplicates(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "beta"]
        )

        assert result["added"] == ["beta"]
        assert manager.get_session(sid).state["node_refs"] == ["alpha", "beta"]

    def test_adding_only_known_nodes_does_not_advance_the_revision(self, tools):
        """A no-op write must not make every other collaborator's revision stale."""
        tools_map, manager = tools
        sid = _session(manager)
        first = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        again = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert again["success"] is True
        assert again["added"] == []
        assert again["revision"] == first["revision"]

    def test_the_new_nodes_are_broadcast_to_connected_clients(self, tools):
        """Session state is server-owned: connected canvases learn about the add."""
        tools_map, manager = tools
        sid = _session(manager)
        published = []
        manager.bus.publish = lambda session_id, event: published.append(
            (session_id, event)
        )

        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert [sid for sid, _ in published] == [sid]
        op = published[0][1]["op"]
        assert op["op"] == "nodes_added"
        assert op["node_ids"] == ["alpha"]

    def test_new_node_edge_to_already_visible_node_hydrates_for_connected_client(
        self, tmp_path
    ):
        """End-to-end regression for the MCP live-push edge-rendering bug.

        A connected browser already has ``alpha`` on its canvas. An MCP
        client then adds ``beta`` — which the graph already connects to
        ``alpha`` — to the same open session. The op stream delivers only
        ``{op: nodes_added, node_ids: [beta]}`` (as in the broadcast test
        above); to render it the browser hydrates the new id one node at a
        time via ``get_node_details`` (see App.jsx's ``applyRemoteOp``). That
        call must return the alpha<->beta edge, or it never renders until the
        user separately expands the node.
        """
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service)
        tools_map["add_nodes"](
            nodes=[
                {"id": "alpha", "type": "Initiative", "name": "Alpha"},
                {"id": "beta", "type": "Actor", "name": "Beta"},
            ],
            edges=[{"source": "alpha", "target": "beta"}],
        )
        sid = _session(manager)
        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        published = []
        manager.bus.publish = lambda session_id, event: published.append(
            (session_id, event)
        )
        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["beta"])
        assert result["success"] is True
        op = published[0][1]["op"]
        assert op["op"] == "nodes_added"
        assert op["node_ids"] == ["beta"]

        # Simulates the connected browser's per-id hydration of the pushed node.
        hydrated = tools_map["get_node_details"](node_id="beta")
        assert hydrated["success"] is True
        assert any(
            {e["source"], e["target"]} == {"alpha", "beta"} for e in hydrated["edges"]
        )

    def test_unknown_ids_are_skipped_rather_than_referenced(self, tools):
        """A stale id must not leave a phantom reference in session state."""
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "ghost"]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert result["skipped"] == ["ghost"]
        assert manager.get_session(sid).state["node_refs"] == ["alpha"]

    def test_all_ids_unknown_is_an_error(self, tools):
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["ghost"])

        assert result["success"] is False
        assert result["error"] == "no_resolvable_nodes"
        assert result["skipped"] == ["ghost"]

    def test_stale_expected_revision_is_rejected(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["beta"], expected_revision=0
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        assert result["current_revision"] == manager.get_session(sid).seq
        assert manager.get_session(sid).state["node_refs"] == ["alpha"]

    def test_current_revision_is_accepted(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        first = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        result = tools_map["add_nodes_to_session"](
            session_id=sid,
            node_ids=["beta"],
            expected_revision=first["revision"],
        )

        assert result["success"] is True
        assert result["revision"] > first["revision"]

    def test_returned_revision_threads_into_a_layout_write(self, tools):
        """The point of the tool: populate then arrange, without a search."""
        tools_map, manager = tools
        sid = _session(manager)

        added = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "beta"]
        )
        moved = tools_map["apply_visualization_layout"](
            session_id=sid,
            positions={"alpha": {"x": 0, "y": 0}, "beta": {"x": 300, "y": 0}},
            expected_revision=added["revision"],
        )

        assert moved["success"] is True
        layout = tools_map["get_visualization_layout"](session_id=sid)
        assert {n["id"] for n in layout["nodes"]} == {"alpha", "beta"}

    def test_invalid_session_id(self, tools):
        tools_map, _ = tools
        result = tools_map["add_nodes_to_session"](
            session_id="nope", node_ids=["alpha"]
        )
        assert result["success"] is False
        assert "Invalid session ID" in result["error"]

    def test_unknown_session(self, tools):
        tools_map, _ = tools
        result = tools_map["add_nodes_to_session"](
            session_id="9999-9999", node_ids=["alpha"]
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_unknown_session_is_reported_when_no_id_resolves_either(self, tools):
        """no_resolvable_nodes must not mask the session-not-found error."""
        tools_map, _ = tools
        result = tools_map["add_nodes_to_session"](
            session_id="9999-9999", node_ids=["ghost"]
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_an_unknown_session_is_reported_before_any_node_is_resolved(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        lookups = []
        original = storage.get_node
        storage.get_node = lambda node_id: (lookups.append(node_id), original(node_id))[
            1
        ]
        tools_map, _ = _wire(storage, service)

        result = tools_map["add_nodes_to_session"](
            session_id="9999-9999", node_ids=["a", "b"]
        )

        assert "not found" in result["error"]
        assert lookups == []

    def test_empty_node_ids_is_rejected(self, tools):
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=[])

        assert result["success"] is False
        assert "node_ids" in result["error"]

    def test_a_repeated_id_is_added_and_reported_once(self, tools):
        """`added` and the broadcast must agree with the stored union."""
        tools_map, manager = tools
        sid = _session(manager)
        published = []
        manager.bus.publish = lambda session_id, event: published.append(event)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "alpha", "beta"]
        )

        assert result["added"] == ["alpha", "beta"]
        assert result["node_count"] == 2
        assert published[0]["op"]["node_ids"] == ["alpha", "beta"]
        assert manager.get_session(sid).state["node_refs"] == ["alpha", "beta"]

    def test_a_repeated_unresolvable_id_is_reported_once(self, tools):
        """`skipped` counts ids, like `added` — not occurrences."""
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["ghost", "ghost", "alpha"]
        )

        assert result["added"] == ["alpha"]
        assert result["skipped"] == ["ghost"]

    def test_a_non_string_id_is_skipped_not_an_exception(self, tools):
        """Arguments reach this tool unvalidated (POST /execute_tool)."""
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", {"id": "beta"}, 7]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert result["skipped"] == [{"id": "beta"}, 7]
        assert manager.get_session(sid).state["node_refs"] == ["alpha"]

    def test_an_oversized_batch_is_rejected_before_any_node_is_resolved(self, tmp_path):
        """The cap must bound the per-id resolve, not just the write after it."""
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        lookups = []
        original = storage.get_node
        storage.get_node = lambda node_id: (lookups.append(node_id), original(node_id))[
            1
        ]
        tools_map, manager = _wire(storage, service, max_ops_per_batch=2)
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["a", "b", "c"]
        )

        assert result["success"] is False
        assert result["error"] == "too_large"
        assert lookups == []

    def test_a_repeated_id_draws_one_token_from_the_rate_budget(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        manager._mcp_bucket = _TokenBucket(1.0, 0.0)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha"] * 10
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]

    def test_a_repeated_id_counts_once_against_the_batch_cap(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service, max_ops_per_batch=2)
        tools_map["add_nodes"](
            nodes=[
                {"id": "alpha", "type": "Initiative", "name": "Alpha"},
                {"id": "beta", "type": "Actor", "name": "Beta"},
            ],
            edges=[],
        )
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "alpha", "beta", "beta"]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha", "beta"]

    def test_repeated_unhashable_ids_are_skipped_once(self, tools):
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid,
            node_ids=["alpha", {"id": "b", "x": 1}, {"x": 1, "id": "b"}, [1], [1]],
        )

        assert result["success"] is True
        assert result["skipped"] == [{"id": "b", "x": 1}, [1]]

    def test_an_id_with_no_canonical_json_is_skipped_not_an_exception(self, tools):
        """An in-process caller can pass a value ``json.dumps`` rejects; the
        dedupe keys unhashable ids by their JSON, so it must not raise."""
        tools_map, manager = tools
        sid = _session(manager)
        cyclic = []
        cyclic.append(cyclic)
        mixed_keys = {1: "a", "b": 2}
        deep = []
        deep_hashable = ()
        for _ in range(5000):
            deep = [deep]
            deep_hashable = (deep_hashable,)
        unprintable = _Unprintable()
        unusual = [cyclic, mixed_keys, deep, deep_hashable, unprintable]

        result = tools_map["add_nodes_to_session"](
            session_id=sid,
            node_ids=["alpha", *unusual, cyclic, deep_hashable, "beta"],
        )

        assert result["success"] is True
        assert result["added"] == ["alpha", "beta"]
        assert len(result["skipped"]) == len(unusual)
        assert all(a is b for a, b in zip(result["skipped"], unusual))
        assert manager.get_session(sid).state["node_refs"] == ["alpha", "beta"]

    def test_a_repeat_of_a_hashable_id_is_dropped_before_it_is_encoded(self, tools):
        """Equal to an id already seen, it is the same id, as on main, even if
        it has no JSON form of its own."""
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", _EqualToAlpha()]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert result["skipped"] == []

    def test_an_id_whose_hash_raises_is_skipped_not_an_exception(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        unhashing = _UnhashingUnprintable()

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", unhashing]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert len(result["skipped"]) == 1
        assert result["skipped"][0] is unhashing

    def test_an_id_whose_hash_raises_is_deduplicated_by_its_sorted_json(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        first = _UnhashingDict({"id": "b", "x": 1})
        reordered = _UnhashingDict({"x": 1, "id": "b"})

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", first, reordered]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert len(result["skipped"]) == 1
        assert result["skipped"][0] is first

    def test_equal_hashable_ids_of_different_types_are_one_id(self, tools):
        """1, True and 1.0 are one id under hash equality, as on main, though
        their JSON forms differ."""
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=[1, True, 1.0, "a"]
        )

        assert result["error"] == "no_resolvable_nodes"
        assert result["skipped"] == [1, "a"]
        assert [type(node_id) for node_id in result["skipped"]] == [int, str]

    def test_ids_that_look_like_each_others_dedupe_keys_stay_distinct(self, tools):
        """A string, a tuple and a list that share a JSON rendering or a key
        tag are three different ids."""
        tools_map, manager = tools
        sid = _session(manager)
        node_ids = ['["x"]', ("u", '["x"]'), ["x"]]

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=node_ids)

        assert result["error"] == "no_resolvable_nodes"
        assert result["skipped"] == node_ids
        assert [type(node_id) for node_id in result["skipped"]] == [str, tuple, list]

    def test_an_unencodable_id_is_not_mistaken_for_an_id_equal_to_its_identity(
        self, tools
    ):
        tools_map, manager = tools
        sid = _session(manager)
        cyclic = []
        cyclic.append(cyclic)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", id(cyclic), cyclic]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert len(result["skipped"]) == 2
        assert result["skipped"][0] == id(cyclic)
        assert result["skipped"][1] is cyclic

    def test_unencodable_ids_equal_to_a_later_id_do_not_shadow_it(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        first, second = _EqualToAlpha(), _EqualToAlpha()

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=[first, second, "alpha"]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert len(result["skipped"]) == 2
        assert result["skipped"][0] is first
        assert result["skipped"][1] is second

    def test_every_distinct_id_the_caps_count_draws_from_the_rate_budget_once(
        self, tools
    ):
        """Skipped ids are charged too (they cost a lookup each); ids with no
        canonical JSON count against no cap and are not resolved, so they are
        not. The charge is taken once, not again by the write."""
        tools_map, manager = tools
        sid = _session(manager)
        buckets = _record_every_bucket(manager)
        cyclic = []
        cyclic.append(cyclic)

        result = tools_map["add_nodes_to_session"](
            session_id=sid,
            node_ids=["alpha", cyclic, _UnhashingUnprintable(), "ghost", "beta"],
        )

        assert result["success"] is True
        assert result["added"] == ["alpha", "beta"]
        assert {attr: bucket.consumed for attr, bucket in buckets.items()} == {
            attr: [3] if attr == "_mcp_bucket" else [] for attr in buckets
        }
        assert buckets["_mcp_bucket"].keys == ["mcp-agent:add_nodes_to_session"]

    def test_a_no_resolvable_nodes_call_draws_from_the_rate_budget(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        manager._mcp_bucket = _TokenBucket(2.0, 0.0)

        refused = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["ghost", "phantom", "ghost"]
        )
        spent = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert refused["error"] == "no_resolvable_nodes"
        assert spent["success"] is False
        assert spent["error"] == "rate_limited"
        assert manager.get_session(sid).state["node_refs"] == []

    def test_only_unencodable_ids_still_draw_one_unit(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        buckets = _record_every_bucket(manager)
        cyclic = []
        cyclic.append(cyclic)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=[cyclic])

        assert result["error"] == "no_resolvable_nodes"
        assert buckets["_mcp_bucket"].consumed == [1]

    def test_a_spent_budget_is_refused_before_any_id_is_resolved(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service)
        tools_map["add_nodes"](
            nodes=[{"id": "alpha", "type": "Initiative", "name": "Alpha"}], edges=[]
        )
        sid = _session(manager)
        manager._mcp_bucket = _TokenBucket(0.0, 0.0)
        resolved_with = []
        original = service.resolve_session_node_semantics

        def spy(node_ids, **kwargs):
            resolved_with.append(list(node_ids))
            return original(node_ids, **kwargs)

        service.resolve_session_node_semantics = spy

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert result == {
            "success": False,
            "error": "rate_limited",
            "message": "Too many session writes; slow down and retry.",
        }
        assert resolved_with == []
        assert manager.get_session(sid).state["node_refs"] == []

    def test_a_call_refused_before_the_resolve_draws_nothing(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service, max_ops_per_batch=1)
        sid = _session(manager)
        buckets = _record_every_bucket(manager)

        unknown = tools_map["add_nodes_to_session"](
            session_id="1234-5678-9012-3456", node_ids=["alpha"]
        )
        too_many = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "beta"]
        )

        assert "not found" in unknown["error"]
        assert too_many["error"] == "too_large"
        assert all(bucket.consumed == [] for bucket in buckets.values())

    def test_a_revision_conflict_is_charged_exactly_once(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])
        buckets = _record_every_bucket(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["beta", "ghost"], expected_revision=0
        )

        assert result["error"] == "revision_conflict"
        assert buckets["_mcp_bucket"].consumed == [2]

    def test_an_id_with_no_canonical_json_counts_against_no_cap_and_is_not_resolved(
        self, tmp_path
    ):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(
            storage, service, max_ops_per_batch=1, max_op_batch_bytes=9
        )
        tools_map["add_nodes"](
            nodes=[{"id": "alpha", "type": "Initiative", "name": "Alpha"}], edges=[]
        )
        sid = _session(manager)
        assert len(json.dumps(["alpha"])) == manager.max_op_batch_bytes
        resolved_with = []
        original = service.resolve_session_node_semantics

        def spy(node_ids, **kwargs):
            resolved_with.append(list(node_ids))
            return original(node_ids, **kwargs)

        service.resolve_session_node_semantics = spy
        cyclic = []
        cyclic.append(cyclic)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", cyclic]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert resolved_with == [["alpha"]]

    def test_only_ids_with_no_canonical_json_is_no_resolvable_nodes(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        cyclic = {}
        cyclic["self"] = cyclic

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=[cyclic])

        assert result["success"] is False
        assert result["error"] == "no_resolvable_nodes"
        assert len(result["skipped"]) == 1
        assert result["skipped"][0] is cyclic
        assert manager.get_session(sid).state["node_refs"] == []

    def test_an_oversized_byte_payload_is_rejected_before_any_node_is_resolved(
        self, tmp_path
    ):
        """The byte cap bounds the per-id resolve too, and says it is a size cap."""
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        lookups = []
        original = storage.get_node
        storage.get_node = lambda node_id: (lookups.append(node_id), original(node_id))[
            1
        ]
        tools_map, manager = _wire(storage, service, max_op_batch_bytes=50)
        sid = _session(manager)
        node_ids = ["x" * 30, "y" * 30]
        assert len(json.dumps(node_ids)) > 50

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=node_ids)

        assert result["success"] is False
        assert result["error"] == "too_large"
        assert "size cap" in result["message"]
        assert "Too many" not in result["message"]
        assert lookups == []

    @pytest.mark.parametrize("slack, succeeds", [(0, True), (-1, False)])
    def test_the_byte_cap_measures_the_ids_as_one_json_list(
        self, tmp_path, slack, succeeds
    ):
        """Brackets, separators and every id's encoding count exactly: escaped
        non-ASCII, an unhashable id, one encoded through ``default=str`` and a
        hashable dict whose keys do not sort included; a key-reordered repeat
        of the unhashable id counts once."""
        unique_ids = [
            "alpha",
            {"id": "b", "x": [1, 2]},
            "é",
            {1},
            _HashableDict({1: "a", "b": 2}),
        ]
        node_ids = unique_ids + [{"x": [1, 2], "id": "b"}]
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(
            storage,
            service,
            max_op_batch_bytes=len(json.dumps(unique_ids, default=str)) + slack,
        )
        tools_map["add_nodes"](
            nodes=[{"id": "alpha", "type": "Initiative", "name": "Alpha"}], edges=[]
        )
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=node_ids)

        if succeeds:
            assert result["success"] is True
            assert result["added"] == ["alpha"]
            assert result["skipped"] == unique_ids[1:]
        else:
            assert result["error"] == "too_large"

    def test_the_byte_cap_counts_a_repeated_id_once(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service, max_op_batch_bytes=50)
        long_id = "n" * 30
        tools_map["add_nodes"](
            nodes=[{"id": long_id, "type": "Actor", "name": "Long"}], edges=[]
        )
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=[long_id, long_id]
        )

        assert result["success"] is True
        assert result["added"] == [long_id]

    def test_a_single_id_over_the_byte_cap_is_rejected(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        lookups = []
        original = storage.get_node
        storage.get_node = lambda node_id: (lookups.append(node_id), original(node_id))[
            1
        ]
        tools_map, manager = _wire(storage, service, max_op_batch_bytes=20)
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["x" * 30])

        assert result["error"] == "too_large"
        assert "size cap" in result["message"]
        assert lookups == []

    def test_an_oversized_count_names_the_count_cap(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service, max_ops_per_batch=2)
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["a", "b", "c"]
        )

        assert result["error"] == "too_large"
        assert "Too many node ids" in result["message"]

    def test_deduplication_compares_each_id_a_bounded_number_of_times(self, tools):
        """The dedupe runs on the uncapped list, so it must stay linear.

        Checking membership in the growing result list instead of a set would
        still dedupe correctly — it is only quadratic, which no result shows.
        Counting ``__eq__`` calls makes that visible: a set compares an id only
        against the entries its hash lands on, a list against every one before it.
        """
        tools_map, manager = tools
        sid = _session(manager)
        comparisons = []

        class CountingId(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                comparisons.append(1)
                return str.__eq__(self, other)

        distinct = 400
        node_ids = [CountingId(f"id-{i}") for i in range(distinct)] * 2
        # Exactly the distinct count: the repeats must not be charged either.
        manager._mcp_bucket = _TokenBucket(float(distinct), 0.0)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=node_ids)

        assert result["error"] == "no_resolvable_nodes"
        assert len(result["skipped"]) == distinct
        assert len(comparisons) <= len(node_ids)

    def test_only_the_deduplicated_ids_are_resolved(self, tmp_path):
        """The per-id resolve is bounded by the caps only if it gets the ids the
        caps counted, not the raw list with its repeats."""
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service)
        tools_map["add_nodes"](
            nodes=[
                {"id": "alpha", "type": "Initiative", "name": "Alpha"},
                {"id": "beta", "type": "Actor", "name": "Beta"},
            ],
            edges=[],
        )
        sid = _session(manager)
        resolved_with = []
        original = service.resolve_session_node_semantics

        def spy(node_ids, **kwargs):
            resolved_with.append(list(node_ids))
            return original(node_ids, **kwargs)

        service.resolve_session_node_semantics = spy

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "alpha", "beta", "ghost", "ghost"]
        )

        assert result["added"] == ["alpha", "beta"]
        assert resolved_with == [["alpha", "beta", "ghost"]]

    def test_a_session_deleted_before_the_write_is_reported_as_not_found(
        self, tools, monkeypatch
    ):
        """The session can go between the upfront lookup and the write. That race
        must read exactly like the upfront not-found, not as a different error."""
        tools_map, manager = tools
        sid = _session(manager)

        def deleted_meanwhile(*args, **kwargs):
            raise SessionNotFound()

        monkeypatch.setattr(manager, "add_node_refs", deleted_meanwhile)
        raced = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])
        monkeypatch.undo()
        manager.store.delete(sid)
        upfront = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert raced == upfront
        assert raced["success"] is False
        assert f"Session '{sid}' not found" in raced["error"]

    def test_an_unadopted_federated_search_result_id_is_not_addable(self, tmp_path):
        """The tool sends agents to search_graph, which can return remote ids.

        An unadopted federated node lives only in the FederationManager's cache,
        so the projection cannot resolve it. Adoption is what changes that, and
        the next test covers the other side — together they pin the whole
        promise the docstring makes, not just its convenient half.
        """
        service = _make_multi_graph_service(tmp_path, DefaultGraphAuthorizationHook())
        tools_map, manager = _wire(None, service)
        sid = _session(manager)

        found = service.search_graph(query="Alpha result")
        federated_ids = [n["id"] for n in found["nodes"]]
        assert federated_ids == ["federated::graph-alpha::remote-1"]

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=federated_ids
        )

        assert result["success"] is False
        assert result["error"] == "no_resolvable_nodes"
        assert result["skipped"] == federated_ids
        assert manager.get_session(sid).state["node_refs"] == []

    def test_an_adopted_federated_id_becomes_addable(self, tmp_path):
        """Adoption writes a local reference under the *federated* id.

        So the same id that was skipped a moment ago now resolves and is added.
        The docstring says "unadopted" for exactly this reason; an absolute
        "federated ids are never addable" would be false here.
        """
        service = _make_multi_graph_service(tmp_path, DefaultGraphAuthorizationHook())
        tools_map, manager = _wire(None, service)
        sid = _session(manager)
        federated_id = "federated::graph-alpha::remote-1"

        assert service.adopt_federated_node(federated_id)["success"] is True

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=[federated_id]
        )

        assert result["success"] is True
        assert result["added"] == [federated_id]
        assert result["skipped"] == []
        assert manager.get_session(sid).state["node_refs"] == [federated_id]


class TestAuthorization:
    def test_read_only_mode_denies_the_write(self, tools, monkeypatch):
        """The tool goes through the same gate as the other session mutations."""
        tools_map, manager = tools
        sid = _session(manager)

        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")
        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert manager.get_session(sid).state["node_refs"] == []

    def test_read_only_mode_is_denied_before_the_session_lookup(
        self, tools, monkeypatch
    ):
        """The gate comes first, so a denied caller cannot probe which session
        ids exist. The later mutate-scoped resolve denies as well, which is why
        only a call that never reaches the resolve tells the two apart."""
        tools_map, manager = tools
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["add_nodes_to_session"](
            session_id="9999-9999", node_ids=["alpha"]
        )

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert manager.get_session("9999-9999") is None

    def test_read_only_mode_is_denied_before_the_batch_caps(
        self, tmp_path, monkeypatch
    ):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service, max_ops_per_batch=2)
        sid = _session(manager)
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["a", "b", "c"]
        )

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"

    def test_read_only_mode_is_denied_before_the_byte_cap(self, tmp_path, monkeypatch):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        tools_map, manager = _wire(storage, service, max_op_batch_bytes=50)
        sid = _session(manager)
        node_ids = ["x" * 30, "y" * 30]
        assert len(node_ids) <= manager.max_ops_per_batch
        assert len(json.dumps(node_ids)) > manager.max_op_batch_bytes
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=node_ids)

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"

    def test_a_node_outside_the_callers_graph_scope_is_not_added(self, tmp_path):
        """Graph-scope narrowing decides what may enter the session."""
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        storage.add_nodes(
            [
                Node(
                    id="mine",
                    type="Initiative",
                    name="Mine",
                    metadata={"origin_graph_id": "graph-alpha"},
                ),
                Node(
                    id="theirs",
                    type="Actor",
                    name="Theirs",
                    metadata={"origin_graph_id": "graph-beta"},
                ),
            ],
            [],
        )
        service = GraphService(
            storage,
            authorization_hook=FixedNarrowingHook(
                allow_local_graph=False, include_graph_ids=("graph-alpha",)
            ),
        )
        tools_map, manager = _wire(storage, service)
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["mine", "theirs"]
        )

        assert result["added"] == ["mine"]
        assert result["skipped"] == ["theirs"]
        assert manager.get_session(sid).state["node_refs"] == ["mine"]

    def test_a_readable_but_unmutable_node_is_not_added(self, tmp_path):
        """The write narrows by the *mutate* decision, not the read one.

        A hook may let a caller read a graph it may not write into. Filtering the
        ids by read visibility would put such a node into server-owned session
        state — the gap every sibling mutation closes by narrowing with its own
        decision.
        """
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        storage.add_nodes(
            [
                Node(
                    id="mine",
                    type="Initiative",
                    name="Mine",
                    metadata={"origin_graph_id": "graph-alpha"},
                ),
                Node(
                    id="readable",
                    type="Actor",
                    name="Readable but not mine to write",
                    metadata={"origin_graph_id": "graph-beta"},
                ),
            ],
            [],
        )
        hook = ActionScopedNarrowingHook(
            read_graph_ids=("graph-alpha", "graph-beta"),
            mutate_graph_ids=("graph-alpha",),
        )
        service = GraphService(storage, authorization_hook=hook)
        tools_map, manager = _wire(storage, service)
        sid = _session(manager)

        # The caller really can read it — that is what makes the seam matter.
        assert service.get_node_details("readable").get("success") is not False
        hook.seen_contexts.clear()

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["mine", "readable"]
        )

        assert result["added"] == ["mine"]
        assert result["skipped"] == ["readable"]
        assert manager.get_session(sid).state["node_refs"] == ["mine"]

        # Every evaluation this one call makes asks the hook the same question,
        # about the tool the caller invoked — not about the projection helper, a
        # target a deployment's hook has no reason to have heard of. Asserted as
        # a set: the invariant is that they agree, not how many there are, so
        # caching the decision would not have to break this test.
        assert hook.seen_contexts
        assert {(c.action, c.target) for c in hook.seen_contexts} == {
            ("mutate", "add_nodes_to_session")
        }


def test_every_session_tool_names_both_accepted_session_id_forms(tools):
    """SESSION_ID_RE accepts DDDD-DDDD as well as DDDD-DDDD-DDDD-DDDD, and the
    tools' own docstring examples use the short form, so an invalid-id error
    that names only the long form would steer a caller away from a valid id."""
    tools_map, _ = tools
    checked = []
    for name, tool in tools_map.items():
        params = inspect.signature(tool).parameters
        if "session_id" not in params:
            continue
        kwargs = {
            p.name: None
            for p in params.values()
            if p.default is inspect.Parameter.empty
        }
        kwargs["session_id"] = "nope"
        result = tool(**kwargs)
        assert "Invalid session ID format" in result["error"], name
        assert "DDDD-DDDD-DDDD-DDDD" in result["error"], name
        assert "older DDDD-DDDD form" in result["error"], name
        checked.append(name)
    assert "add_nodes_to_session" in checked
    assert "apply_visualization_layout" in checked
    assert "rename_visualization_session" in checked


def test_every_visualization_session_id_tool_names_both_accepted_forms(tmp_path):
    """The same rule for the tools that take ``visualization_session_id``.

    They validate the id only once a push registry (and, for the auto-add
    tools, an auto-add registry) is wired, so the ``session_id`` test above,
    which wires neither, never reaches their id check.
    """
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(
        mock_mcp,
        service,
        session_registry=SessionRegistry(),
        session_manager=SessionManager(
            SessionStore(InMemorySessionPersistenceBackend())
        ),
        auto_add_registry=SessionAutoAddRegistry(),
    )
    checked = []
    for name, tool in tools_map.items():
        params = inspect.signature(tool).parameters
        param = params.get("visualization_session_id")
        # Optional on the search/read tools, where no id means "do not push".
        if param is None or param.default is not inspect.Parameter.empty:
            continue
        kwargs = {
            p.name: None
            for p in params.values()
            if p.default is inspect.Parameter.empty
        }
        kwargs["visualization_session_id"] = "nope"
        result = tool(**kwargs)
        assert "Invalid session ID format" in result["error"], name
        assert "DDDD-DDDD-DDDD-DDDD" in result["error"], name
        assert "older DDDD-DDDD form" in result["error"], name
        checked.append(name)
    assert sorted(checked) == [
        "clear_visualization",
        "create_session_auto_add_agent",
        "list_session_auto_add_agents",
        "remove_session_auto_add_agent",
    ]
