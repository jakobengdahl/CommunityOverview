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
            "actor_id": "",
            "actor_type": "",
            "is_authenticated": False,
            "auth_source": "anonymous",
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
            "actor_id": "rest-actor",
            "actor_type": "member",
            "is_authenticated": True,
            "auth_source": "iap",
            "source": "request",
        }


class TestRequestScopeRestEndpoint:
    def test_endpoint_returns_default_shape(self, app_client):
        client, _ = app_client
        result = client.get("/api/request-scope").json()
        assert result == {
            "workspace_id": "",
            "workspace_kind": "",
            "graph_id": "",
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
            "workspace_id": "workspace-rest",
            "workspace_kind": "personal",
            "graph_id": "graph-rest",
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
            "actor_id": "mcp-actor",
            "actor_type": "member",
            "is_authenticated": True,
            "auth_source": "test",
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
            "workspace_id": "workspace-mcp",
            "workspace_kind": "team",
            "graph_id": "graph-mcp",
            "source": "override",
        }

    def test_tools_appear_in_mcp_discovery_inventory(self, app_client):
        client, _ = app_client
        result = client.get("/mcp").json()
        assert "get_request_actor" in result["available_tools"]
        assert "get_request_scope" in result["available_tools"]
