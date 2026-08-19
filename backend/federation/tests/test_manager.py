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
#
# They use httpx.MockTransport rather than another loopback HTTPServer because
# only a mock transport can script a multi-request sequence (sync, then fail,
# then recover) and assert that some calls make no request at all. The loopback
# tests above stay as they are: they exercise the real transport stack, which a
# mock cannot.
# ---------------------------------------------------------------------------

_REMOTE_GRAPH_URL = "https://federated.invalid/graph.json"
# Deliberately not the shipped default (config.py: default_timeout_ms = 1200).
# A configured value identical to the default cannot distinguish "the timeout
# came from config" from "the timeout is hardcoded".
_TIMEOUT_MS = 900


def _single_graph_config(
    graph_json_url=_REMOTE_GRAPH_URL, enabled=True, timeout_ms=_TIMEOUT_MS
):
    return FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "max_traversal_depth": 1,
                "default_timeout_ms": timeout_ms,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": enabled,
                        # A graph with only an MCP endpoint is the real shape of
                        # the "no graph_json_url" case — the config layer rejects
                        # a graph with no endpoint URL at all.
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

    def assert_requested_once(self):
        """The remote was asked for exactly the configured URL, by GET, with the
        configured timeout applied — the fetch details no assertion on the
        response body can pin."""
        assert [str(request.url) for request in self.requests] == [_REMOTE_GRAPH_URL]
        assert self.requests[0].method == "GET"
        assert self.requests[0].extensions["timeout"] == {
            "connect": _TIMEOUT_MS / 1000.0,
            "read": _TIMEOUT_MS / 1000.0,
            "write": _TIMEOUT_MS / 1000.0,
            "pool": _TIMEOUT_MS / 1000.0,
        }


class _StubHttpxModule:
    """Stands in for the module-level ``httpx`` name in manager.py.

    ``_fetch_graph_payload`` constructs its own ``httpx.AsyncClient()`` when no
    client is passed in; replacing the module attribute is the only way to bind
    a mock transport to that branch without opening a real socket. Every test
    below installs it, so a regression that opens a client where it should not
    is caught rather than quietly reaching the network.
    """

    def __init__(self, transport):
        self._transport = transport
        self.clients_opened = 0

    # Deliberately exposes AsyncClient alone. If manager.py grows a reference to
    # another httpx attribute, these tests break loudly with AttributeError
    # rather than quietly passing against an unexercised path.
    def AsyncClient(self, **kwargs):
        self.clients_opened += 1
        return httpx.AsyncClient(transport=self._transport, **kwargs)


def _sealed(monkeypatch, remote):
    """Install the stub module so no test can fall through to a real client."""
    stub = _StubHttpxModule(remote.transport)
    monkeypatch.setattr(manager_module, "httpx", stub)
    return stub


_ONE_NODE = {"nodes": [{"id": "n1", "type": "Actor", "name": "First"}], "edges": []}


_SECOND_GRAPH_URL = "https://federated-b.invalid/graph.json"


def _two_graph_config():
    """Every other federation fixture in this repo configures a single graph,
    which cannot distinguish "resolves the named graph" from "resolves the only
    graph"."""
    return FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "max_traversal_depth": 1,
                "default_timeout_ms": _TIMEOUT_MS,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "endpoints": {"graph_json_url": _REMOTE_GRAPH_URL},
                    },
                    {
                        "graph_id": "other-main",
                        "display_name": "Other",
                        "enabled": True,
                        "endpoints": {"graph_json_url": _SECOND_GRAPH_URL},
                    },
                ],
            }
        }
    )


class _PerUrlTransport:
    """Mock transport serving a different payload per URL, so a request to the
    wrong endpoint is visible rather than indistinguishable."""

    def __init__(self, payloads):
        self._payloads = payloads
        self.requests = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=self._payloads[str(request.url)])


def _two_graph_remote():
    return _PerUrlTransport(
        {
            _REMOTE_GRAPH_URL: {
                "nodes": [{"id": "a1", "type": "Actor", "name": "From A"}],
                "edges": [],
            },
            _SECOND_GRAPH_URL: {
                "nodes": [
                    {"id": "b1", "type": "Actor", "name": "From B"},
                    {"id": "b2", "type": "Actor", "name": "Also From B"},
                ],
                "edges": [],
            },
        }
    )


@pytest.mark.asyncio
async def test_sync_graph_fetches_and_caches_only_the_graph_it_was_named(monkeypatch):
    remote = _two_graph_remote()
    _sealed(monkeypatch, remote)
    manager = FederationManager(_two_graph_config())

    async with httpx.AsyncClient(transport=remote.transport) as client:
        result = await manager.sync_graph("other-main", client)

    assert result["success"] is True
    assert result["graph_id"] == "other-main"
    assert result["nodes"] == 2

    # Resolving the wrong graph would fetch one endpoint and write the payload
    # into another graph's cache — a silent cross-graph poisoning.
    assert [str(request.url) for request in remote.requests] == [_SECOND_GRAPH_URL]

    by_id = {graph["graph_id"]: graph for graph in manager.get_status()["graphs"]}
    assert by_id["other-main"]["cached_nodes"] == 2
    assert by_id["other-main"]["status"] == "healthy"
    assert by_id["esam-main"]["cached_nodes"] == 0
    assert by_id["esam-main"]["status"] == "offline"
    assert manager.get_cached_node("federated::other-main::b1") is not None
    assert manager.get_cached_node("federated::esam-main::b1") is None


@pytest.mark.asyncio
async def test_sync_graph_declines_an_unknown_id_rather_than_syncing_another_graph(
    monkeypatch,
):
    remote = _two_graph_remote()
    _sealed(monkeypatch, remote)
    manager = FederationManager(_two_graph_config())

    result = await manager.sync_graph("no-such-graph")

    assert result["success"] is False
    assert "no-such-graph" in result["error"]
    assert remote.requests == []
    assert all(graph["cached_nodes"] == 0 for graph in manager.get_status()["graphs"])


@pytest.mark.asyncio
async def test_sync_graph_loads_remote_nodes_and_edges_into_the_cache(monkeypatch):
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
    stub = _sealed(monkeypatch, remote)
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
    remote.assert_requested_once()
    assert stub.clients_opened == 0


@pytest.mark.asyncio
async def test_sync_graph_opens_its_own_client_when_none_is_supplied(monkeypatch):
    remote = _ScriptedTransport((200, _ONE_NODE))
    stub = _sealed(monkeypatch, remote)

    manager = FederationManager(_single_graph_config())
    result = await manager.sync_graph("esam-main")

    assert result["success"] is True
    assert result["nodes"] == 1
    assert stub.clients_opened == 1
    # The self-opened branch is a separate code path from the supplied-client
    # branch, so it needs its own proof that it requests the right thing.
    remote.assert_requested_once()


@pytest.mark.asyncio
async def test_sync_graph_reuses_a_supplied_client_instead_of_opening_its_own(
    monkeypatch,
):
    remote = _ScriptedTransport((200, _ONE_NODE))
    stub = _sealed(monkeypatch, remote)

    manager = FederationManager(_single_graph_config())
    async with httpx.AsyncClient(transport=remote.transport) as client:
        result = await manager.sync_graph("esam-main", client)

    assert result["success"] is True
    assert result["nodes"] == 1
    assert stub.clients_opened == 0
    assert len(remote.requests) == 1


@pytest.mark.parametrize("status_code", [401, 404, 500])
@pytest.mark.asyncio
async def test_sync_graph_degrades_and_keeps_the_cache_on_an_error_response(
    monkeypatch, status_code
):
    remote = _ScriptedTransport(
        (200, _ONE_NODE),
        (status_code, {"error": "upstream failure"}),
    )
    _sealed(monkeypatch, remote)

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
    # or a transient remote outage becomes indistinguishable from the remote
    # having deleted every node it served. 4xx matters as much as 5xx here: a
    # federated graph that starts answering 401 must not be read as "empty".
    assert graph_status["cached_nodes"] == 1
    assert node_events == []


@pytest.mark.parametrize("status_code", [401, 404, 500])
@pytest.mark.asyncio
async def test_sync_graph_degrades_on_an_error_response_from_its_own_client(
    monkeypatch, status_code
):
    """The self-opened branch has its own raise_for_status, so the parametrised
    test above — which supplies a client — cannot reach it. It needs the same
    status range: 4xx is the case that matters most, and it matters at both
    call sites."""
    remote = _ScriptedTransport((status_code, {"error": "upstream failure"}))
    stub = _sealed(monkeypatch, remote)
    manager = FederationManager(_single_graph_config())

    result = await manager.sync_graph("esam-main")

    assert result["success"] is False
    assert stub.clients_opened == 1
    assert manager.get_status()["graphs"][0]["status"] == "degraded"


@pytest.mark.asyncio
async def test_sync_graph_degrades_when_the_remote_is_unreachable(monkeypatch):
    remote = _ScriptedTransport(httpx.ConnectError("connection refused"))
    _sealed(monkeypatch, remote)
    manager = FederationManager(_single_graph_config())

    async with httpx.AsyncClient(transport=remote.transport) as client:
        result = await manager.sync_graph("esam-main", client)

    assert result["success"] is False
    assert result["error"]

    graph_status = manager.get_status()["graphs"][0]
    assert graph_status["status"] == "degraded"
    assert graph_status["cached_nodes"] == 0


@pytest.mark.asyncio
async def test_sync_graph_clears_the_recorded_error_once_a_later_sync_succeeds(
    monkeypatch,
):
    remote = _ScriptedTransport(
        (500, {"error": "upstream failure"}),
        (200, _ONE_NODE),
    )
    _sealed(monkeypatch, remote)
    manager = FederationManager(_single_graph_config())

    async with httpx.AsyncClient(transport=remote.transport) as client:
        assert (await manager.sync_graph("esam-main", client))["success"] is False
        assert manager.get_status()["graphs"][0]["last_error"]
        assert (await manager.sync_graph("esam-main", client))["success"] is True

    # A recovered graph must stop reporting the stale failure, or an operator
    # dashboard reading get_status() shows an error that is no longer true.
    graph_status = manager.get_status()["graphs"][0]
    assert graph_status["status"] == "healthy"
    assert graph_status["last_error"] is None


@pytest.mark.asyncio
async def test_sync_graph_swaps_the_cache_before_it_announces_the_change(monkeypatch):
    remote = _ScriptedTransport((200, _ONE_NODE))
    _sealed(monkeypatch, remote)

    observed = []

    def _on_node_event(op, before, after):
        # A subscriber that reacts to a create by reading the node back must
        # find it there. Emitting before the swap would hand out a stale cache.
        observed.append(manager.get_cached_node(after.id))

    manager = FederationManager(_single_graph_config(), on_node_event=_on_node_event)

    async with httpx.AsyncClient(transport=remote.transport) as client:
        await manager.sync_graph("esam-main", client)

    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].id == "federated::esam-main::n1"


@pytest.mark.asyncio
async def test_sync_graph_emits_node_and_edge_events_for_the_cache_swap(monkeypatch):
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
    _sealed(monkeypatch, remote)

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
async def test_sync_graph_currently_re_emits_an_update_for_an_unchanged_node(
    monkeypatch,
):
    """Documents current behaviour, which is wrong but out of scope to change here.

    Node.from_dict stamps a fresh created_at/updated_at every time _build_cache
    rebuilds the cache, and _emit_node_events compares nodes with to_dict(), so
    the comparison never matches and every cached node is re-announced as an
    update on every sync — a scheduled graph emits a full update storm per
    interval even when the remote payload is byte-identical. Tracked separately
    as smallfix-federation-unchanged-node-emits-update; this test is the one to
    flip when that is fixed.
    """
    remote = _ScriptedTransport((200, _ONE_NODE), (200, _ONE_NODE))
    _sealed(monkeypatch, remote)

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
async def test_sync_graph_currently_treats_a_payload_without_nodes_as_an_empty_graph(
    monkeypatch,
):
    """Documents current behaviour rather than endorsing it.

    A 200 carrying neither key empties the cache and reports success, so a
    remote that starts serving `{}` — malformed, truncated, or an error
    envelope — silently drops every federated node and announces it as
    deletions. That is the same outcome the non-2xx path deliberately guards
    against, reached through a response the guard never sees. Tracked
    separately as smallfix-federation-empty-payload-wipes-cache; this test is
    the one to flip when that is decided.
    """
    remote = _ScriptedTransport((200, _ONE_NODE), (200, {}))
    _sealed(monkeypatch, remote)

    node_events = []
    manager = FederationManager(
        _single_graph_config(),
        on_node_event=lambda op, before, after: node_events.append(op),
    )

    async with httpx.AsyncClient(transport=remote.transport) as client:
        await manager.sync_graph("esam-main", client)
        node_events.clear()
        result = await manager.sync_graph("esam-main", client)

    assert result == {
        "success": True,
        "graph_id": "esam-main",
        "nodes": 0,
        "edges": 0,
    }
    assert manager.get_status()["graphs"][0]["cached_nodes"] == 0
    assert node_events == ["delete"]


@pytest.mark.asyncio
async def test_sync_graph_degrades_without_a_request_when_no_graph_json_url_is_set(
    monkeypatch,
):
    remote = _ScriptedTransport(httpx.ConnectError("the network must not be touched"))
    stub = _sealed(monkeypatch, remote)

    manager = FederationManager(_single_graph_config(graph_json_url=None))
    result = await manager.sync_graph("esam-main")

    assert result["success"] is False
    assert result["error"]
    assert manager.get_status()["graphs"][0]["status"] == "degraded"
    assert stub.clients_opened == 0
    assert remote.requests == []


@pytest.mark.asyncio
async def test_sync_graph_declines_unknown_and_disabled_graphs_without_a_request(
    monkeypatch,
):
    remote = _ScriptedTransport(httpx.ConnectError("the network must not be touched"))
    stub = _sealed(monkeypatch, remote)

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
