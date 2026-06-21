import pytest
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

    response = client.post("/execute_tool", json={
        "tool_name": "add_nodes",
        "arguments": {
            "nodes": [{"id": "test-auth", "type": "Actor", "name": "Test Auth"}],
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
