"""Targeted tests for request actor/scope REST and MCP introspection."""

from unittest.mock import patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.config import AppConfig
from backend.authorization import GraphAccessNarrowing, GraphAuthorizationContext, GraphAuthorizationDecision
from backend.core import Edge, Node, NodeType
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    for var in (
        "COMMUNITYOVERVIEW_ACTOR_ID",
        "COMMUNITYOVERVIEW_ACTOR_TYPE",
        "COMMUNITYOVERVIEW_AUTH_SOURCE",
        "COMMUNITYOVERVIEW_WORKSPACE_ID",
        "COMMUNITYOVERVIEW_WORKSPACE_KIND",
        "COMMUNITYOVERVIEW_GRAPH_SCOPE_ID",
        "COMMUNITYOVERVIEW_AUTHORIZATION_MODE",
    ):
        monkeypatch.delenv(var, raising=False)

    graph_file = str(tmp_path / "graph.json")
    config = AppConfig(auth_enabled=False, graph_file=graph_file)
    app = create_app(config)
    return TestClient(app), app


class CaptureAuthorizationHook:
    def __init__(self, *, allow: bool):
        self.allow = allow
        self.seen_contexts = []

    def evaluate(self, context: GraphAuthorizationContext) -> GraphAuthorizationDecision:
        self.seen_contexts.append(context)
        if self.allow:
            return GraphAuthorizationDecision(allowed=True)
        return GraphAuthorizationDecision(
            allowed=False,
            reason="blocked for test",
            mode="test",
            source="test",
        )


class SelectionAwareNarrowingHook:
    def __init__(self):
        self.seen_contexts = []

    def evaluate(self, context: GraphAuthorizationContext) -> GraphAuthorizationDecision:
        self.seen_contexts.append(context)
        selected_graph_id = context.scope.get("graph_id", "")
        workspace_id = context.scope.get("workspace_id", "")
        if not selected_graph_id or not workspace_id:
            return GraphAuthorizationDecision(allowed=True, mode="selection-aware", source="test")
        return GraphAuthorizationDecision(
            allowed=True,
            mode="selection-aware",
            source="test",
            graph_access=GraphAccessNarrowing(
                enabled=True,
                allow_local_graph=False,
                include_graph_ids=(selected_graph_id,),
            ),
        )


def _install_multi_graph_fixture(app):
    graph_service = app.state.graph_service
    graph_service.storage.add_nodes(
        [Node(id="local-1", type=NodeType.ACTOR, name="Local result")],
        [],
    )

    graph_service.storage.add_nodes(
        [
            Node(
                id="alpha-ref",
                type=NodeType.ACTOR,
                name="Alpha export",
                metadata={"origin_graph_id": "graph-alpha", "is_federated_reference": True},
            ),
            Node(
                id="beta-ref",
                type=NodeType.ACTOR,
                name="Beta export",
                metadata={"origin_graph_id": "graph-beta", "is_federated_reference": True},
            ),
        ],
        [Edge(source="alpha-ref", target="beta-ref", type="RELATES_TO")],
    )

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "graph-alpha",
                    "display_name": "Alpha",
                    "enabled": True,
                    "capabilities": {"allow_adopt": True},
                    "endpoints": {"graph_json_url": "https://example.invalid/alpha.json"},
                },
                {
                    "graph_id": "graph-beta",
                    "display_name": "Beta",
                    "enabled": True,
                    "capabilities": {"allow_adopt": True},
                    "endpoints": {"graph_json_url": "https://example.invalid/beta.json"},
                },
            ],
        }
    })
    manager = FederationManager(config)
    for graph, node_id, name in (
        (config.federation.graphs[0], "remote-1", "Alpha result"),
        (config.federation.graphs[1], "remote-2", "Beta result"),
    ):
        cache_nodes, _ = manager._build_cache(
            graph,
            [{"id": node_id, "type": "Actor", "name": name}],
            [],
        )
        manager._cache[graph.graph_id].nodes = cache_nodes

    graph_service._federation_manager = manager
    graph_service._authorization_hook = SelectionAwareNarrowingHook()


class TestRequestActorRestEndpoint:
    def test_endpoint_returns_default_shape(self, app_client):
        client, _ = app_client
        result = client.get("/api/request-actor").json()
        assert result == {
            "actor_type": "",
            "is_authenticated": False,
            "auth_source": "anonymous",
            "has_actor": False,
            "source": "default",
        }

    def test_endpoint_reflects_request_headers(self, app_client):
        client, _ = app_client
        result = client.get(
            "/api/request-actor",
            headers={
                "X-CommunityOverview-Actor-Id": "rest-actor",
                "X-CommunityOverview-Actor-Type": "member",
                "X-CommunityOverview-Auth-Source": "iap",
            },
        ).json()
        assert result == {
            "actor_type": "member",
            "is_authenticated": True,
            "auth_source": "iap",
            "has_actor": True,
            "source": "request",
        }


class TestRequestScopeRestEndpoint:
    def test_endpoint_returns_default_shape(self, app_client):
        client, _ = app_client
        result = client.get("/api/request-scope").json()
        assert result == {
            "workspace_kind": "",
            "has_workspace": False,
            "has_graph": False,
            "has_selection": False,
            "selection_mode": "default",
            "selection_source": "default",
            "source": "default",
        }

    def test_endpoint_reflects_request_headers(self, app_client):
        client, _ = app_client
        result = client.get(
            "/api/request-scope",
            headers={
                "X-CommunityOverview-Workspace-Id": "workspace-rest",
                "X-CommunityOverview-Workspace-Kind": "personal",
                "X-CommunityOverview-Graph-Id": "graph-rest",
            },
        ).json()
        assert result == {
            "workspace_kind": "personal",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "workspace_graph",
            "selection_source": "request",
            "source": "request",
        }


class TestRequestSelectionRestEndpoint:
    def test_endpoint_returns_default_shape(self, app_client):
        client, _ = app_client
        result = client.get("/api/request-selection").json()
        assert result == {
            "workspace_kind": "",
            "has_workspace": False,
            "has_graph": False,
            "has_selection": False,
            "selection_mode": "default",
            "selection_source": "default",
            "source": "default",
        }

    def test_endpoint_reflects_request_headers(self, app_client):
        client, _ = app_client
        result = client.get(
            "/api/request-selection",
            headers={
                "X-CommunityOverview-Workspace-Id": "workspace-rest",
                "X-CommunityOverview-Workspace-Kind": "personal",
                "X-CommunityOverview-Graph-Id": "graph-rest",
            },
        ).json()
        assert result == {
            "workspace_kind": "personal",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "workspace_graph",
            "selection_source": "request",
            "source": "request",
        }


class TestRequestContextMcpTools:
    def test_actor_tool_is_registered_and_safe(self, app_client):
        client, app = app_client
        assert "get_request_actor" in app.state.tools_map

        response = client.post("/execute_tool", json={
            "tool_name": "get_request_actor",
            "arguments": {"actor_id": "mcp-actor", "actor_type": "member", "auth_source": "test"},
        })
        assert response.status_code == 200
        assert response.json() == {
            "actor_type": "member",
            "is_authenticated": True,
            "auth_source": "test",
            "has_actor": True,
            "source": "override",
        }

    def test_scope_tool_is_registered_and_safe(self, app_client):
        client, app = app_client
        assert "get_request_scope" in app.state.tools_map

        response = client.post("/execute_tool", json={
            "tool_name": "get_request_scope",
            "arguments": {
                "workspace_id": "workspace-mcp",
                "workspace_kind": "team",
                "graph_id": "graph-mcp",
            },
        })
        assert response.status_code == 200
        assert response.json() == {
            "workspace_kind": "team",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "workspace_graph",
            "selection_source": "override",
            "source": "override",
        }

    def test_selection_tool_is_registered_and_safe(self, app_client):
        client, app = app_client
        assert "get_request_selection" in app.state.tools_map

        response = client.post("/execute_tool", json={
            "tool_name": "get_request_selection",
            "arguments": {
                "workspace_id": "workspace-mcp",
                "workspace_kind": "team",
                "graph_id": "graph-mcp",
            },
        })
        assert response.status_code == 200
        assert response.json() == {
            "workspace_kind": "team",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "workspace_graph",
            "selection_source": "override",
            "source": "override",
        }

    def test_tools_appear_in_mcp_discovery_inventory(self, app_client):
        client, _ = app_client
        result = client.get("/mcp").json()
        assert "get_request_actor" in result["available_tools"]
        assert "get_request_scope" in result["available_tools"]
        assert "get_request_selection" in result["available_tools"]

    def test_mcp_transport_request_binds_request_authorization_headers(self, tmp_path, monkeypatch):
        for var in (
            "COMMUNITYOVERVIEW_ACTOR_ID",
            "COMMUNITYOVERVIEW_ACTOR_TYPE",
            "COMMUNITYOVERVIEW_AUTH_SOURCE",
            "COMMUNITYOVERVIEW_WORKSPACE_ID",
            "COMMUNITYOVERVIEW_WORKSPACE_KIND",
            "COMMUNITYOVERVIEW_GRAPH_SCOPE_ID",
            "COMMUNITYOVERVIEW_AUTHORIZATION_MODE",
        ):
            monkeypatch.delenv(var, raising=False)

        hook = CaptureAuthorizationHook(allow=True)
        holder = {}

        async def fake_mcp_transport_app(scope, receive, send):
            result = holder["app"].state.graph_service.search_graph(query="", limit=1)
            response = JSONResponse(result)
            await response(scope, receive, send)

        with patch("backend.api_host.server.FastMCP.sse_app", return_value=fake_mcp_transport_app):
            config = AppConfig(auth_enabled=False, graph_file=str(tmp_path / "graph.json"))
            app = create_app(config)
            holder["app"] = app
            app.state.graph_service._authorization_hook = hook
            client = TestClient(app)

        response = client.post(
            "/mcp",
            headers={
                "X-CommunityOverview-Actor-Id": "transport-actor",
                "X-CommunityOverview-Actor-Type": "member",
                "X-CommunityOverview-Auth-Source": "transport",
                "X-CommunityOverview-Workspace-Id": "workspace-mcp",
                "X-CommunityOverview-Workspace-Kind": "team",
                "X-CommunityOverview-Graph-Id": "graph-mcp",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        assert response.status_code == 200
        assert len(hook.seen_contexts) == 1
        context = hook.seen_contexts[0]
        assert context.actor["actor_id"] == "transport-actor"
        assert context.actor["actor_type"] == "member"
        assert context.actor["auth_source"] == "transport"
        assert context.scope["workspace_id"] == "workspace-mcp"
        assert context.scope["workspace_kind"] == "team"
        assert context.scope["graph_id"] == "graph-mcp"


class TestAuthorizationNarrowingRestAndMcp:
    def test_rest_search_is_narrowed_to_selected_graph(self, app_client):
        client, app = app_client
        _install_multi_graph_fixture(app)

        response = client.post(
            "/api/search",
            headers={
                "X-CommunityOverview-Workspace-Id": "workspace-rest",
                "X-CommunityOverview-Workspace-Kind": "team",
                "X-CommunityOverview-Graph-Id": "graph-beta",
            },
            json={"query": "result", "node_types": ["Actor"], "limit": 10},
        )

        assert response.status_code == 200
        payload = response.json()
        assert [node["name"] for node in payload["nodes"]] == ["Beta result"]
        assert payload["federation"]["federated_nodes"] == 1

    def test_execute_tool_search_is_narrowed_to_selected_graph(self, app_client):
        client, app = app_client
        _install_multi_graph_fixture(app)

        response = client.post(
            "/execute_tool",
            headers={
                "X-CommunityOverview-Workspace-Id": "workspace-mcp",
                "X-CommunityOverview-Workspace-Kind": "team",
                "X-CommunityOverview-Graph-Id": "graph-alpha",
            },
            json={
                "tool_name": "search_graph",
                "arguments": {"query": "result", "node_types": ["Actor"], "limit": 10},
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert [node["name"] for node in payload["nodes"]] == ["Alpha result"]
        assert payload["federation"]["federated_nodes"] == 1

    def test_export_graph_is_narrowed_to_selected_graph(self, app_client):
        client, app = app_client
        _install_multi_graph_fixture(app)

        response = client.get(
            "/export_graph",
            headers={
                "X-CommunityOverview-Workspace-Id": "workspace-export",
                "X-CommunityOverview-Workspace-Kind": "team",
                "X-CommunityOverview-Graph-Id": "graph-alpha",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert [node["name"] for node in payload["nodes"]] == ["Alpha export"]
        assert payload["edges"] == []
        assert payload["total_nodes"] == 1
        assert payload["total_edges"] == 0


class TestAuthorizationHookRestAndMcp:
    def test_rest_mutation_is_blocked_in_read_only_mode(self, app_client, monkeypatch):
        monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "read-only")
        client, _ = app_client

        response = client.post("/api/nodes", json={
            "nodes": [{"type": "Actor", "name": "Blocked via REST"}],
            "edges": [],
        })

        assert response.status_code == 403
        assert "mutations are disabled" in response.json()["detail"].lower()

    def test_execute_tool_read_is_blocked_in_deny_all_mode(self, app_client, monkeypatch):
        monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "deny-all")
        client, _ = app_client

        response = client.post("/execute_tool", json={
            "tool_name": "search_graph",
            "arguments": {
                "query": "",
                "limit": 5,
            },
        })

        assert response.status_code == 403
        assert response.json()["error_code"] == "access_denied"
        assert response.json()["authorization"]["action"] == "read"

    def test_rest_read_remains_permitted_in_read_only_mode(self, app_client, monkeypatch):
        monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "read-only")
        client, _ = app_client

        response = client.post("/api/search", json={"query": "", "limit": 5})

        assert response.status_code == 200
        assert "nodes" in response.json()

    def test_export_graph_uses_request_bound_authorization_and_returns_403(self, app_client):
        client, app = app_client
        hook = CaptureAuthorizationHook(allow=False)
        app.state.graph_service._authorization_hook = hook

        response = client.get(
            "/export_graph",
            headers={
                "X-CommunityOverview-Actor-Id": "export-actor",
                "X-CommunityOverview-Actor-Type": "member",
                "X-CommunityOverview-Auth-Source": "export-test",
                "X-CommunityOverview-Workspace-Id": "workspace-export",
                "X-CommunityOverview-Workspace-Kind": "team",
                "X-CommunityOverview-Graph-Id": "graph-export",
            },
        )

        assert response.status_code == 403
        assert response.json()["error_code"] == "access_denied"
        assert len(hook.seen_contexts) == 1
        context = hook.seen_contexts[0]
        assert context.target == "export_graph"
        assert context.actor["actor_id"] == "export-actor"
        assert context.actor["actor_type"] == "member"
        assert context.actor["auth_source"] == "export-test"
        assert context.scope["workspace_id"] == "workspace-export"
        assert context.scope["workspace_kind"] == "team"
        assert context.scope["graph_id"] == "graph-export"
