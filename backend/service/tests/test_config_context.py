"""Targeted tests for public config-context introspection."""

import pytest
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.config import AppConfig


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    for var in (
        "COMMUNITYOVERVIEW_TENANT_ID",
        "COMMUNITYOVERVIEW_TENANT_NAME",
        "COMMUNITYOVERVIEW_ENVIRONMENT",
        "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR",
        "SCHEMA_FILE",
        "GRAPH_SCHEMA_CONFIG",
        "FEDERATION_FILE",
        "GRAPH_FEDERATION_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)

    graph_file = str(tmp_path / "graph.json")
    config = AppConfig(auth_enabled=False, graph_file=graph_file)
    app = create_app(config)
    return TestClient(app), app


class TestConfigContextRestEndpoint:
    def test_defaults_to_public_default_paths(self, app_client):
        client, _ = app_client

        response = client.get("/api/config-context")

        assert response.status_code == 200
        result = response.json()
        assert result["tenant_config_dir_configured"] is False
        assert result["schema_config_source"] == "default"
        assert result["federation_config_source"] == "default"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result

    def test_resolves_paths_from_tenant_config_dir(self, tmp_path, monkeypatch):
        tenant_dir = tmp_path / "tenant-config"
        tenant_dir.mkdir()
        (tenant_dir / "schema_config.json").write_text("{}", encoding="utf-8")
        (tenant_dir / "federation_config.json").write_text(
            '{"federation": {}}', encoding="utf-8"
        )
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_CONFIG_DIR", str(tenant_dir))

        config = AppConfig(auth_enabled=False, graph_file=str(tmp_path / "graph.json"))
        app = create_app(config)
        client = TestClient(app)

        result = client.get("/api/config-context").json()

        assert result["tenant_config_dir_configured"] is True
        assert result["schema_config_source"] == "tenant_config_dir"
        assert result["federation_config_source"] == "tenant_config_dir"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result

    def test_explicit_env_vars_override_tenant_config_dir(self, tmp_path, monkeypatch):
        tenant_dir = tmp_path / "tenant-config"
        tenant_dir.mkdir()
        explicit_schema = tmp_path / "schema-explicit.json"
        explicit_federation = tmp_path / "federation-explicit.json"
        explicit_schema.write_text("{}", encoding="utf-8")
        explicit_federation.write_text('{"federation": {}}', encoding="utf-8")

        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_CONFIG_DIR", str(tenant_dir))
        monkeypatch.setenv("SCHEMA_FILE", str(explicit_schema))
        monkeypatch.setenv("FEDERATION_FILE", str(explicit_federation))

        config = AppConfig(auth_enabled=False, graph_file=str(tmp_path / "graph.json"))
        app = create_app(config)
        client = TestClient(app)

        result = client.get("/api/config-context").json()

        assert result["tenant_config_dir_configured"] is True
        assert result["schema_config_source"] == "explicit_env"
        assert result["federation_config_source"] == "explicit_env"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result


class TestConfigContextMcpTool:
    def test_tool_is_registered(self, app_client):
        _, app = app_client
        assert "get_config_context" in app.state.tools_map

    def test_tool_is_safe_and_accessible_via_execute_tool(self, app_client):
        client, _ = app_client

        response = client.post(
            "/execute_tool", json={"tool_name": "get_config_context", "arguments": {}}
        )

        assert response.status_code == 200
        result = response.json()
        assert "schema_config_source" in result
        assert "federation_config_source" in result
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result

    def test_tool_appears_in_mcp_discovery_inventory(self, app_client):
        client, _ = app_client

        response = client.get("/mcp")

        assert response.status_code == 200
        assert "get_config_context" in response.json()["available_tools"]
