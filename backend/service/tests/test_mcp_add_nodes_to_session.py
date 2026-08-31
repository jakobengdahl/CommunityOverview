"""
Tests for the ``add_nodes_to_session`` MCP tool.

The tool places a known set of nodes on a session's canvas by id, instead of
requiring a search whose results happen to be exactly that set. It is a thin
wrapper over ``SessionManager.add_node_refs``; the op semantics themselves are
covered in ``backend/core/tests/test_session_manager.py``.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage, Node
from backend.core.session_manager import SessionManager
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
