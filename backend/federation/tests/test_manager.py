"""Tests for federation manager cache sync and search behavior."""

import json
import threading
import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2 as httpx

from backend.core.models import Node, NodeType
from backend.federation import manager as manager_module
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager


class _Handler(BaseHTTPRequestHandler):
    payload = {
        "nodes": [
            {
                "id": "remote-1",
                "type": "Actor",
                "name": "eSam",
                "description": "External organization",
                "summary": "Federated node",
                "tags": ["external"],
            }
        ],
        "edges": [],
    }

    def do_GET(self):
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _start_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.mark.asyncio
async def test_sync_and_search_remote_graph_json():
    server = _start_server()
    try:
        port = server.server_address[1]
        config = FederationFileConfig.model_validate(
            {
                "federation": {
                    "enabled": True,
                    "max_traversal_depth": 1,
                    "graphs": [
                        {
                            "graph_id": "esam-main",
                            "display_name": "eSam",
                            "enabled": True,
                            "endpoints": {
                                "graph_json_url": f"http://127.0.0.1:{port}/graph.json"
                            },
                        }
                    ],
                }
            }
        )

        manager = FederationManager(config)
        sync = await manager.sync_all()

        assert sync["success"] is True
        result = manager.search_nodes(query="esam", node_types=None, limit=10)
        assert len(result["nodes"]) == 1
        node = result["nodes"][0]
        assert node.metadata["origin_graph_id"] == "esam-main"
        assert node.metadata["is_federated"] is True
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_sync_degrades_when_unreachable_url():
    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "broken",
                        "display_name": "Broken",
                        "enabled": True,
                        "endpoints": {
                            "graph_json_url": "http://127.0.0.1:1/graph.json"
                        },
                    }
                ],
            }
        }
    )

    manager = FederationManager(config)
    sync = await manager.sync_all()
    assert sync["success"] is False

    status = manager.get_status()
    assert status["graphs"][0]["status"] == "degraded"


def test_score_node_match_alias_beats_description():
    """A federated node matched only via an alias must outrank a description-only match."""
    alias_node = Node(
        id="a",
        type=NodeType.ACTOR,
        name="Unrelated",
        description="unrelated",
        aliases=["esam"],
    )
    desc_node = Node(
        id="d",
        type=NodeType.ACTOR,
        name="Another",
        description="part of the esam network",
    )
    alias_score = FederationManager._score_node_match(alias_node, "esam")
    desc_score = FederationManager._score_node_match(desc_node, "esam")
    assert alias_score > desc_score


def test_score_node_match_real_name_beats_alias():
    """A real-name match must outrank an alias-only match in federated search too."""
    name_node = Node(id="n", type=NodeType.ACTOR, name="Nordic esam", description="x")
    alias_node = Node(
        id="a", type=NodeType.ACTOR, name="Unrelated", description="x", aliases=["esam"]
    )
    assert FederationManager._score_node_match(
        name_node, "esam"
    ) > FederationManager._score_node_match(alias_node, "esam")


def test_score_node_match_alias_does_not_lift_above_stronger_name():
    """A federated node matching on both name (contains) and an exact alias must not
    outscore a node whose name is an exact match — name and alias combine with max()."""
    name_plus_alias = Node(
        id="x",
        type=NodeType.ACTOR,
        name="Global esam network",
        description="x",
        aliases=["esam"],
    )
    exact_name = Node(id="y", type=NodeType.ACTOR, name="esam", description="x")
    assert FederationManager._score_node_match(
        exact_name, "esam"
    ) > FederationManager._score_node_match(name_plus_alias, "esam")


def test_scheduler_starts_for_scheduled_graph():
    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "sched",
                        "display_name": "Scheduled",
                        "enabled": True,
                        "sync": {
                            "mode": "scheduled",
                            "interval_seconds": 10,
                            "on_startup": False,
                            "on_demand": True,
                        },
                        "endpoints": {
                            "graph_json_url": "http://127.0.0.1:1/graph.json"
                        },
                    }
                ],
            }
        }
    )

    manager = FederationManager(config)
    manager.start()
    try:
        status = manager.get_status()
        assert status["scheduler_running"] is True
    finally:
        manager.stop()


def test_sync_emits_node_events_for_cache_changes():
    events = []

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )

    manager = FederationManager(
        config,
        on_node_event=lambda op, before, after: events.append(
            (op, before.id if before else None, after.id if after else None)
        ),
    )

    graph = config.federation.graphs[0]

    first_nodes, _ = manager._build_cache(
        graph,
        [{"id": "remote-1", "type": "Actor", "name": "Version One"}],
        [],
    )
    second_nodes, _ = manager._build_cache(
        graph,
        [
            {"id": "remote-1", "type": "Actor", "name": "Version Two"},
            {"id": "remote-2", "type": "Actor", "name": "New"},
        ],
        [],
    )

    manager._emit_node_events({}, first_nodes)
    manager._emit_node_events(first_nodes, second_nodes)

    assert ("create", None, "federated::esam-main::remote-1") in events
    assert (
        "update",
        "federated::esam-main::remote-1",
        "federated::esam-main::remote-1",
    ) in events
    assert ("create", None, "federated::esam-main::remote-2") in events


def test_sync_emits_edge_events_for_cache_changes():
    edge_events = []

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )

    manager = FederationManager(
        config,
        on_edge_event=lambda op, before, after: edge_events.append(
            (op, before.id if before else None, after.id if after else None)
        ),
    )

    graph = config.federation.graphs[0]
    _, first_edges = manager._build_cache(
        graph,
        [
            {"id": "n1", "type": "Actor", "name": "A"},
            {"id": "n2", "type": "Actor", "name": "B"},
        ],
        [{"id": "e1", "source": "n1", "target": "n2", "type": "RELATES_TO"}],
    )

    _, second_edges = manager._build_cache(
        graph,
        [
            {"id": "n1", "type": "Actor", "name": "A"},
            {"id": "n2", "type": "Actor", "name": "B"},
        ],
        [{"id": "e1", "source": "n1", "target": "n2", "type": "PART_OF"}],
    )

    manager._emit_edge_events({}, first_edges)
    manager._emit_edge_events(first_edges, second_edges)

    # create on first sync + delete/create replace semantics on update
    assert any(
        evt[0] == "create" and evt[2] == "federated::esam-main::e1"
        for evt in edge_events
    )
    assert any(
        evt[0] == "delete" and evt[1] == "federated::esam-main::e1"
        for evt in edge_events
    )


# ---------------------------------------------------------------------------
# sync_graph — the single path by which a remote graph enters the local cache.
#
# These drive sync_graph end to end over a mocked transport. The older event
# tests above call _build_cache / _emit_node_events directly, so they cannot
# tell whether sync_graph still wires fetch, cache swap and event emission
# together; that wiring is what these cover.
# ---------------------------------------------------------------------------

_REMOTE_GRAPH_URL = "https://federated.invalid/graph.json"


def _single_graph_config(graph_json_url=_REMOTE_GRAPH_URL, enabled=True):
    return FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "max_traversal_depth": 1,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": enabled,
                        # A graph with only an MCP endpoint is the real shape
                        # of the "no graph_json_url" case — the config layer
                        # rejects a graph with no endpoint URL at all.
                        "endpoints": (
                            {"graph_json_url": graph_json_url}
                            if graph_json_url
                            else {"mcp_url": "https://federated.invalid/mcp"}
                        ),
                    }
                ],
            }
        }
    )


class _ScriptedTransport:
    """Mock transport replaying scripted steps in order, repeating the last.

    A step is either ``(status_code, json_payload)`` or an exception instance,
    which is raised to simulate a transport-level failure.
    """

    def __init__(self, *steps):
        self._steps = list(steps)
        self.requests = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        self.requests.append(request)
        step = self._steps[min(len(self.requests), len(self._steps)) - 1]
        if isinstance(step, Exception):
            raise step
        status_code, payload = step
        return httpx.Response(status_code, json=payload)


class _StubHttpxModule:
    """Stands in for the module-level ``httpx`` name in manager.py.

    ``_fetch_graph_payload`` constructs its own ``httpx.AsyncClient()`` when no
    client is passed in; replacing the module attribute is the only way to bind
    a mock transport to that branch without opening a real socket.
    """

    def __init__(self, transport):
        self._transport = transport
        self.clients_opened = 0

    def AsyncClient(self, **kwargs):
        self.clients_opened += 1
        return httpx.AsyncClient(transport=self._transport, **kwargs)


@pytest.mark.asyncio
async def test_sync_graph_loads_remote_nodes_and_edges_into_the_cache():
    remote = _ScriptedTransport(
        (
            200,
            {
                "nodes": [
                    {"id": "n1", "type": "Actor", "name": "First"},
                    {"id": "n2", "type": "Actor", "name": "Second"},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "type": "RELATES_TO"}
                ],
            },
        )
    )
    manager = FederationManager(_single_graph_config())

    async with httpx.AsyncClient(transport=remote.transport) as client:
        result = await manager.sync_graph("esam-main", client)

    assert result == {
        "success": True,
        "graph_id": "esam-main",
        "nodes": 2,
        "edges": 1,
    }

    graph_status = manager.get_status()["graphs"][0]
    assert graph_status["status"] == "healthy"
    assert graph_status["cached_nodes"] == 2
    assert graph_status["cached_edges"] == 1
    assert graph_status["last_synced_at"] is not None
    assert graph_status["last_error"] is None
    assert [str(request.url) for request in remote.requests] == [_REMOTE_GRAPH_URL]


@pytest.mark.asyncio
async def test_sync_graph_opens_its_own_client_when_none_is_supplied(monkeypatch):
    remote = _ScriptedTransport(
        (200, {"nodes": [{"id": "n1", "type": "Actor", "name": "First"}], "edges": []})
    )
    stub = _StubHttpxModule(remote.transport)
    monkeypatch.setattr(manager_module, "httpx", stub)

    manager = FederationManager(_single_graph_config())
    result = await manager.sync_graph("esam-main")

    assert result["success"] is True
    assert result["nodes"] == 1
    assert stub.clients_opened == 1
    assert len(remote.requests) == 1


@pytest.mark.asyncio
async def test_sync_graph_reuses_a_supplied_client_instead_of_opening_its_own(
    monkeypatch,
):
    remote = _ScriptedTransport((200, {"nodes": [], "edges": []}))
    stub = _StubHttpxModule(remote.transport)
    monkeypatch.setattr(manager_module, "httpx", stub)

    manager = FederationManager(_single_graph_config())
    async with httpx.AsyncClient(transport=remote.transport) as client:
        result = await manager.sync_graph("esam-main", client)

    assert result["success"] is True
    assert stub.clients_opened == 0
    assert len(remote.requests) == 1


@pytest.mark.asyncio
async def test_sync_graph_degrades_and_keeps_the_cache_when_the_remote_returns_5xx():
    remote = _ScriptedTransport(
        (200, {"nodes": [{"id": "n1", "type": "Actor", "name": "First"}], "edges": []}),
        (500, {"error": "upstream failure"}),
    )
    node_events = []
    manager = FederationManager(
        _single_graph_config(),
        on_node_event=lambda op, before, after: node_events.append(op),
    )

    async with httpx.AsyncClient(transport=remote.transport) as client:
        first = await manager.sync_graph("esam-main", client)
        assert first["success"] is True
        node_events.clear()
        result = await manager.sync_graph("esam-main", client)

    assert result["success"] is False
    assert result["graph_id"] == "esam-main"
    assert result["error"]

    graph_status = manager.get_status()["graphs"][0]
    assert graph_status["status"] == "degraded"
    assert graph_status["last_error"]
    # A failed sync must leave the last good cache in place and emit nothing,
    # or a transient remote outage would be indistinguishable from the remote
    # having deleted every node it once served.
    assert graph_status["cached_nodes"] == 1
    assert node_events == []


@pytest.mark.asyncio
async def test_sync_graph_degrades_when_the_remote_is_unreachable():
    remote = _ScriptedTransport(httpx.ConnectError("connection refused"))
    manager = FederationManager(_single_graph_config())

    async with httpx.AsyncClient(transport=remote.transport) as client:
        result = await manager.sync_graph("esam-main", client)

    assert result["success"] is False
    assert result["error"]

    graph_status = manager.get_status()["graphs"][0]
    assert graph_status["status"] == "degraded"
    assert graph_status["cached_nodes"] == 0


@pytest.mark.asyncio
async def test_sync_graph_emits_node_and_edge_events_for_the_cache_swap():
    remote = _ScriptedTransport(
        (
            200,
            {
                "nodes": [
                    {"id": "n1", "type": "Actor", "name": "First"},
                    {"id": "n2", "type": "Actor", "name": "Second"},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "type": "RELATES_TO"}
                ],
            },
        ),
        (
            200,
            {
                "nodes": [
                    {"id": "n2", "type": "Actor", "name": "Second"},
                    {"id": "n3", "type": "Actor", "name": "Third"},
                ],
                "edges": [
                    {"id": "e2", "source": "n2", "target": "n3", "type": "RELATES_TO"}
                ],
            },
        ),
    )

    def _record(sink):
        return lambda op, before, after: sink.append(
            (op, before.id if before else None, after.id if after else None)
        )

    node_events = []
    edge_events = []
    manager = FederationManager(
        _single_graph_config(),
        on_node_event=_record(node_events),
        on_edge_event=_record(edge_events),
    )

    async with httpx.AsyncClient(transport=remote.transport) as client:
        await manager.sync_graph("esam-main", client)

        assert set(node_events) == {
            ("create", None, "federated::esam-main::n1"),
            ("create", None, "federated::esam-main::n2"),
        }
        assert edge_events == [("create", None, "federated::esam-main::e1")]

        node_events.clear()
        edge_events.clear()
        await manager.sync_graph("esam-main", client)

    assert ("create", None, "federated::esam-main::n3") in node_events
    assert ("delete", "federated::esam-main::n1", None) in node_events
    # n2 survives the swap, so it must never be reported as removed.
    assert not any(
        event[0] == "delete" and event[1] == "federated::esam-main::n2"
        for event in node_events
    )
    assert set(edge_events) == {
        ("create", None, "federated::esam-main::e2"),
        ("delete", "federated::esam-main::e1", None),
    }


@pytest.mark.asyncio
async def test_sync_graph_re_emits_an_update_for_a_node_the_remote_did_not_change():
    """Documents current behaviour, which is wrong but out of scope to change here.

    Node.from_dict stamps a fresh created_at/updated_at every time _build_cache
    rebuilds the cache, and _emit_node_events compares nodes with to_dict(), so
    the comparison never matches and every cached node is re-announced as an
    update on every sync — a scheduled graph emits a full update storm per
    interval even when the remote payload is byte-identical. Tracked separately
    as smallfix-federation-unchanged-node-emits-update; this test is the one to
    flip when that is fixed.
    """
    payload = {
        "nodes": [{"id": "n1", "type": "Actor", "name": "First"}],
        "edges": [],
    }
    remote = _ScriptedTransport((200, payload), (200, payload))

    node_events = []
    manager = FederationManager(
        _single_graph_config(),
        on_node_event=lambda op, before, after: node_events.append(op),
    )

    async with httpx.AsyncClient(transport=remote.transport) as client:
        await manager.sync_graph("esam-main", client)
        node_events.clear()
        await manager.sync_graph("esam-main", client)

    assert node_events == ["update"]


@pytest.mark.asyncio
async def test_sync_graph_degrades_without_a_request_when_no_graph_json_url_is_set(
    monkeypatch,
):
    remote = _ScriptedTransport(httpx.ConnectError("the network must not be touched"))
    stub = _StubHttpxModule(remote.transport)
    monkeypatch.setattr(manager_module, "httpx", stub)

    manager = FederationManager(_single_graph_config(graph_json_url=None))
    result = await manager.sync_graph("esam-main")

    assert result["success"] is False
    assert manager.get_status()["graphs"][0]["status"] == "degraded"
    assert stub.clients_opened == 0
    assert remote.requests == []


@pytest.mark.asyncio
async def test_sync_graph_declines_unknown_and_disabled_graphs_without_a_request(
    monkeypatch,
):
    remote = _ScriptedTransport(httpx.ConnectError("the network must not be touched"))
    stub = _StubHttpxModule(remote.transport)
    monkeypatch.setattr(manager_module, "httpx", stub)

    manager = FederationManager(_single_graph_config(enabled=False))

    unknown = await manager.sync_graph("no-such-graph")
    disabled = await manager.sync_graph("esam-main")

    assert unknown["success"] is False
    assert "no-such-graph" in unknown["error"]
    assert disabled["success"] is False
    assert "esam-main" in disabled["error"]

    # A disabled graph keeps its "disabled" status rather than being flipped to
    # "degraded", and an unknown graph_id has no cache entry to degrade at all.
    assert manager.get_status()["graphs"][0]["status"] == "disabled"
    assert stub.clients_opened == 0
    assert remote.requests == []
