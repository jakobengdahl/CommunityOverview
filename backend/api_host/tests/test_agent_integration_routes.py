"""The agents routes never disclose an MCP integration's subprocess argv."""

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.config import (
    AGENTS_WORKSPACE_ENV_VAR,
    AgentsSettings,
    MCPIntegration,
    MCPTransport,
)
from backend.agents.registry import AgentRegistry
from backend.api_host.agent_routes import register_agent_routes

WORKSPACE_MARKER = "/home/svc-agent-user/private-workspace-marker"
ARGV_MARKER = "--argv-marker-token"


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENTS_WORKSPACE_ENV_VAR, WORKSPACE_MARKER)
    # A configured Brave key adds a fifth default integration; pin the set.
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    # The built-in defaults put the resolved workspace root into the FS argv;
    # add an operator-supplied stdio integration on top of them.
    integrations = AgentsSettings._get_default_integrations() + [
        MCPIntegration(
            id="CUSTOM",
            name="Custom",
            description="Operator tool",
            transport=MCPTransport.STDIO,
            command=["/opt/tools/custom-mcp", ARGV_MARKER, WORKSPACE_MARKER],
        )
    ]
    settings = AgentsSettings(
        enabled=False,
        mcp_integrations=integrations,
        run_history_db=str(tmp_path / "runs.db"),
        governance_db=str(tmp_path / "gov.db"),
    )
    registry = AgentRegistry(
        settings=settings, graph_storage=MagicMock(), graph_service=MagicMock()
    )
    app = FastAPI()
    register_agent_routes(
        app,
        agent_registry=registry,
        graph_service=MagicMock(),
        federation_manager=MagicMock(),
    )
    return TestClient(app)


def test_integrations_response_never_contains_argv_or_workspace_path(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/agents/integrations")

    assert resp.status_code == 200
    body = resp.text
    assert WORKSPACE_MARKER not in body
    assert ARGV_MARKER not in body
    assert "/opt/tools/custom-mcp" not in body
    assert all("command" not in item for item in resp.json())


def test_integrations_response_keeps_fields_the_agent_dialog_reads(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)

    items = {i["id"]: i for i in client.get("/agents/integrations").json()}

    assert {"GRAPH", "WEB", "FS", "CUSTOM"} <= set(items)
    assert items["CUSTOM"]["description"] == "Operator tool"
    assert items["FS"]["transport"] == "stdio"


def test_status_response_never_contains_argv_or_workspace_path(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/agents/status")

    assert resp.status_code == 200
    integrations = resp.json()["mcp_integrations"]
    assert len(integrations) == 4
    assert all("command" not in item for item in integrations)
    assert WORKSPACE_MARKER not in resp.text
    assert ARGV_MARKER not in resp.text
    assert "/opt/tools/custom-mcp" not in resp.text
