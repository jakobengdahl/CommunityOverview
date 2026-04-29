"""Targeted tests for request actor/scope REST and MCP introspection."""

import pytest
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.config import AppConfig


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
            "source": "override",
        }

    def test_tools_appear_in_mcp_discovery_inventory(self, app_client):
        client, _ = app_client
        result = client.get("/mcp").json()
        assert "get_request_actor" in result["available_tools"]
        assert "get_request_scope" in result["available_tools"]


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
