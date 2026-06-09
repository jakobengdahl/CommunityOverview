"""
Targeted tests for the tenant/deployment context metadata seam.

Covers:
- REST endpoint GET /tenant-context
- MCP tool get_tenant_context registration and invocation
- MCP discovery inventory exposure
"""

import pytest

from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.config import AppConfig


@pytest.fixture
def app_client(tmp_path):
    graph_file = str(tmp_path / "graph.json")
    config = AppConfig(auth_enabled=False, graph_file=graph_file)
    app = create_app(config)
    return TestClient(app), app


class TestTenantContextRestEndpoint:
    """Tests for GET /api/tenant-context REST endpoint."""

    def test_endpoint_returns_200(self, app_client):
        client, _ = app_client
        response = client.get("/api/tenant-context")
        assert response.status_code == 200

    def test_endpoint_returns_expected_shape(self, app_client):
        client, _ = app_client
        result = client.get("/api/tenant-context").json()
        assert set(result.keys()) == {"tenant_id", "tenant_name", "environment"}

    def test_endpoint_defaults_when_env_unset(self, app_client, monkeypatch):
        monkeypatch.delenv("COMMUNITYOVERVIEW_TENANT_ID", raising=False)
        monkeypatch.delenv("COMMUNITYOVERVIEW_TENANT_NAME", raising=False)
        monkeypatch.delenv("COMMUNITYOVERVIEW_ENVIRONMENT", raising=False)

        client, _ = app_client
        result = client.get("/api/tenant-context").json()

        assert result["tenant_id"] == ""
        assert result["tenant_name"] == ""
        assert result["environment"] == "local"

    def test_endpoint_reflects_env_overrides(self, app_client, monkeypatch):
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_ID", "rest-tenant")
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_NAME", "REST Tenant")
        monkeypatch.setenv("COMMUNITYOVERVIEW_ENVIRONMENT", "production")

        client, _ = app_client
        result = client.get("/api/tenant-context").json()

        assert result == {
            "tenant_id": "rest-tenant",
            "tenant_name": "REST Tenant",
            "environment": "production",
        }


class TestTenantContextMcpTool:
    """Tests for get_tenant_context MCP tool registration and invocation."""

    def test_tool_is_registered_in_tools_map(self, app_client):
        _, app = app_client
        assert "get_tenant_context" in app.state.tools_map

    def test_tool_is_callable(self, app_client):
        _, app = app_client
        fn = app.state.tools_map["get_tenant_context"]
        result = fn()
        assert set(result.keys()) == {"tenant_id", "tenant_name", "environment"}

    def test_tool_via_execute_tool_endpoint(self, app_client):
        client, _ = app_client
        response = client.post("/execute_tool", json={
            "tool_name": "get_tenant_context",
            "arguments": {}
        })
        assert response.status_code == 200
        result = response.json()
        assert "tenant_id" in result
        assert "tenant_name" in result
        assert "environment" in result

    def test_tool_in_safe_tools_allowlist(self, app_client, monkeypatch):
        """get_tenant_context is accessible without authentication (public read-only)."""
        monkeypatch.delenv("COMMUNITYOVERVIEW_TENANT_ID", raising=False)

        client, _ = app_client
        # Unauthenticated call must succeed (app has auth_enabled=False, safe tools accessible)
        response = client.post("/execute_tool", json={
            "tool_name": "get_tenant_context",
            "arguments": {}
        })
        assert response.status_code == 200

    def test_tool_env_override_via_execute_tool(self, app_client, monkeypatch):
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_ID", "mcp-tenant")
        monkeypatch.setenv("COMMUNITYOVERVIEW_ENVIRONMENT", "staging")

        client, _ = app_client
        response = client.post("/execute_tool", json={
            "tool_name": "get_tenant_context",
            "arguments": {}
        })
        assert response.status_code == 200
        result = response.json()
        assert result["tenant_id"] == "mcp-tenant"
        assert result["environment"] == "staging"

    def test_tool_appears_in_mcp_discovery_inventory(self, app_client):
        client, _ = app_client
        response = client.get("/mcp")
        assert response.status_code == 200
        result = response.json()
        assert "get_tenant_context" in result["available_tools"]
