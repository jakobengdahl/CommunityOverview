import pytest
import uuid
from fastapi.testclient import TestClient
from backend.api_host.server import create_app
from backend.api_host.config import AppConfig

# Mock environment to ensure predictable config
@pytest.fixture
def unauthenticated_app():
    config = AppConfig(
        auth_enabled=False,
        graph_file="test_graph_unauth.json"
    )
    # We use a temporary graph file or rely on the fact that tests usually mock storage
    # But create_app creates real storage if not provided.
    # Ideally we should mock GraphStorage, but for integration testing the endpoint logic,
    # using a test file path is okay (it will be created in root or backend dir).
    return create_app(config=config)

@pytest.fixture
def authenticated_app():
    config = AppConfig(
        auth_enabled=True,
        auth_username="admin",
        auth_password="password",
        graph_file="test_graph_auth.json"
    )
    return create_app(config=config)

def test_unauthenticated_safe_tool(unauthenticated_app):
    client = TestClient(unauthenticated_app)
    response = client.post("/execute_tool", json={
        "tool_name": "list_node_types",
        "arguments": {}
    })
    assert response.status_code == 200
    assert "node_types" in response.json()

def test_unauthenticated_get_capabilities_safe_tool(unauthenticated_app):
    client = TestClient(unauthenticated_app)
    response = client.post("/execute_tool", json={
        "tool_name": "get_capabilities",
        "arguments": {}
    })
    assert response.status_code == 200
    assert "capabilities" in response.json()


def test_unauthenticated_get_runtime_info_safe_tool(unauthenticated_app, monkeypatch):
    monkeypatch.setenv("COMMUNITYOVERVIEW_RUNTIME_MODE", "hosted")
    monkeypatch.setenv("COMMUNITYOVERVIEW_ENABLED_EXTENSIONS", "federation,analytics")

    client = TestClient(unauthenticated_app)
    response = client.post("/execute_tool", json={
        "tool_name": "get_runtime_info",
        "arguments": {}
    })
    assert response.status_code == 200
    assert response.json() == {
        "runtime_mode": "hosted",
        "enabled_extensions": ["federation", "analytics"],
    }

def test_unauthenticated_unsafe_tool_blocked(unauthenticated_app):
    client = TestClient(unauthenticated_app)
    # add_nodes is NOT in SAFE_TOOLS
    response = client.post("/execute_tool", json={
        "tool_name": "add_nodes",
        "arguments": {
            "nodes": [{"id": "test", "type": "Actor", "name": "Test"}],
            "edges": []
        }
    })
    assert response.status_code == 403
    assert "requires authentication" in response.json()["error"]

def test_authenticated_unsafe_tool_allowed(authenticated_app):
    client = TestClient(authenticated_app)
    # Using correct credentials
    auth = ("admin", "password")
    node_id = f"test-auth-{uuid.uuid4()}"

    response = client.post("/execute_tool", json={
        "tool_name": "add_nodes",
        "arguments": {
            "nodes": [{"id": node_id, "type": "Actor", "name": "Test Auth"}],
            "edges": []
        }
    }, auth=auth)

    assert response.status_code != 403
    assert response.status_code != 401
    # It should be 200 if the tool executes successfully
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True

def test_authenticated_no_creds_blocked(authenticated_app):
    client = TestClient(authenticated_app)
    response = client.post("/execute_tool", json={
        "tool_name": "add_nodes",
        "arguments": {}
    })
    # Should be blocked by middleware (401)
    assert response.status_code == 401


def test_unauthenticated_get_tenant_context_safe_tool(unauthenticated_app):
    """get_tenant_context is in SAFE_TOOLS and accessible without authentication."""
    client = TestClient(unauthenticated_app)
    response = client.post("/execute_tool", json={
        "tool_name": "get_tenant_context",
        "arguments": {}
    })
    assert response.status_code == 200
    result = response.json()
    assert "tenant_id" in result
    assert "tenant_name" in result
    assert "environment" in result


def test_unauthenticated_get_tenant_context_default_values(unauthenticated_app, monkeypatch):
    """get_tenant_context returns safe defaults when env vars are unset."""
    monkeypatch.delenv("COMMUNITYOVERVIEW_TENANT_ID", raising=False)
    monkeypatch.delenv("COMMUNITYOVERVIEW_TENANT_NAME", raising=False)
    monkeypatch.delenv("COMMUNITYOVERVIEW_ENVIRONMENT", raising=False)

    client = TestClient(unauthenticated_app)
    response = client.post("/execute_tool", json={
        "tool_name": "get_tenant_context",
        "arguments": {}
    })
    assert response.status_code == 200
    result = response.json()
    assert result["tenant_id"] == ""
    assert result["tenant_name"] == ""
    assert result["environment"] == "local"


def test_unauthenticated_get_tenant_context_env_override(unauthenticated_app, monkeypatch):
    """get_tenant_context reflects env var overrides."""
    monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_ID", "demo-tenant")
    monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_NAME", "Demo Org")
    monkeypatch.setenv("COMMUNITYOVERVIEW_ENVIRONMENT", "staging")

    client = TestClient(unauthenticated_app)
    response = client.post("/execute_tool", json={
        "tool_name": "get_tenant_context",
        "arguments": {}
    })
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "demo-tenant",
        "tenant_name": "Demo Org",
        "environment": "staging",
    }
