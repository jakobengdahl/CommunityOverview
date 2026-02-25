"""
Tests for Basic Authentication in the app host.
"""

import pytest
import base64
from fastapi.testclient import TestClient
from backend.api_host import create_app, AppConfig

@pytest.fixture
def auth_enabled_app(temp_graph_file, temp_static_dirs) -> TestClient:
    """Create a TestClient with authentication enabled."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        auth_enabled=True,
        auth_username="admin",
        auth_password="secretpassword",
    )
    app = create_app(config)
    return TestClient(app)

def test_auth_required(auth_enabled_app):
    """Endpoints should return 401 if no authentication is provided."""
    # /api/search requires auth
    response = auth_enabled_app.post("/api/search", json={"query": "test"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"

def test_auth_success(auth_enabled_app):
    """Successful authentication with valid credentials."""
    credentials = base64.b64encode(b"admin:secretpassword").decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}

    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 200

def test_auth_invalid_password(auth_enabled_app):
    """Failed authentication with invalid password."""
    credentials = base64.b64encode(b"admin:wrongpassword").decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}

    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"

def test_auth_invalid_username(auth_enabled_app):
    """Failed authentication with invalid username."""
    credentials = base64.b64encode(b"wronguser:secretpassword").decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}

    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"

def test_auth_invalid_format(auth_enabled_app):
    """Failed authentication with invalid header format."""
    headers = {"Authorization": "Bearer some-token"}

    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401

def test_auth_not_required_on_health(auth_enabled_app):
    """Health check endpoint should not require authentication."""
    response = auth_enabled_app.get("/health")
    assert response.status_code == 200

def test_auth_not_required_on_info(auth_enabled_app):
    """Info endpoint should not require authentication."""
    response = auth_enabled_app.get("/info")
    assert response.status_code == 200
