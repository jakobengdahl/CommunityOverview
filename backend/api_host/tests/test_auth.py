"""
Tests for authentication (Basic and Bearer) in the app host.
"""

import pytest
import base64
from fastapi.testclient import TestClient
from backend.api_host import create_app, AppConfig


@pytest.fixture
def auth_enabled_app(temp_graph_file, temp_static_dirs) -> TestClient:
    """TestClient with Basic auth enabled."""
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


@pytest.fixture
def bearer_auth_app(temp_graph_file, temp_static_dirs) -> TestClient:
    """TestClient with bearer-only auth enabled (no password)."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        auth_enabled=True,
        auth_bearer_token="test-bearer-token-123",
    )
    app = create_app(config)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------

def test_auth_required(auth_enabled_app):
    """Endpoints should return 401 if no authentication is provided."""
    response = auth_enabled_app.post("/api/search", json={"query": "test"})
    assert response.status_code == 401
    assert "Basic" in response.headers["WWW-Authenticate"]


def test_auth_success(auth_enabled_app):
    """Successful authentication with valid Basic credentials."""
    credentials = base64.b64encode(b"admin:secretpassword").decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}

    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 200


def test_auth_invalid_password(auth_enabled_app):
    """Failed Basic auth with wrong password."""
    credentials = base64.b64encode(b"admin:wrongpassword").decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}

    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"


def test_auth_invalid_username(auth_enabled_app):
    """Failed Basic auth with wrong username."""
    credentials = base64.b64encode(b"wronguser:secretpassword").decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}

    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"


def test_auth_unsupported_scheme_rejected(auth_enabled_app):
    """An unknown Authorization scheme returns 401."""
    headers = {"Authorization": "Digest nonce=xyz"}

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


def test_auth_not_required_on_ready(auth_enabled_app):
    """Readiness endpoint should not require authentication."""
    response = auth_enabled_app.get("/ready")
    assert response.status_code == 200


def test_auth_not_required_on_startup_diagnostics(auth_enabled_app):
    """Startup diagnostics endpoint should not require authentication."""
    response = auth_enabled_app.get("/diagnostics/startup")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Bearer auth
# ---------------------------------------------------------------------------

def test_bearer_auth_success(bearer_auth_app):
    """Valid bearer token grants access."""
    headers = {"Authorization": "Bearer test-bearer-token-123"}
    response = bearer_auth_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 200


def test_bearer_auth_wrong_token(bearer_auth_app):
    """Wrong bearer token returns 401."""
    headers = {"Authorization": "Bearer wrong-token"}
    response = bearer_auth_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401


def test_bearer_auth_no_header(bearer_auth_app):
    """Missing Authorization header returns 401 even in bearer-only mode."""
    response = bearer_auth_app.post("/api/search", json={"query": "test"})
    assert response.status_code == 401


def test_bearer_token_rejected_when_unconfigured(auth_enabled_app):
    """Bearer token is rejected when AUTH_BEARER_TOKEN is not set (Basic-only deployment)."""
    headers = {"Authorization": "Bearer any-token"}
    response = auth_enabled_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401


def test_basic_auth_rejected_in_bearer_only_deployment(bearer_auth_app):
    """Basic auth with empty password must not bypass a bearer-only deployment."""
    credentials = base64.b64encode(b"admin:").decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}
    response = bearer_auth_app.post("/api/search", json={"query": "test"}, headers=headers)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# MCP_AUTH_ENABLED=False — web protected, MCP open
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp_auth_disabled_app(temp_graph_file, temp_static_dirs) -> TestClient:
    """auth_enabled=True but mcp_auth_enabled=False: web requires auth, MCP is open."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        auth_enabled=True,
        auth_username="admin",
        auth_password="secretpassword",
        mcp_auth_enabled=False,
    )
    app = create_app(config)
    return TestClient(app)


def test_mcp_auth_disabled_web_still_requires_auth(mcp_auth_disabled_app):
    """API endpoints still require auth when mcp_auth_enabled=False."""
    response = mcp_auth_disabled_app.post("/api/search", json={"query": "test"})
    assert response.status_code == 401


def test_mcp_auth_disabled_mcp_open(mcp_auth_disabled_app):
    """MCP endpoint does not require auth when mcp_auth_enabled=False."""
    # /execute_tool is a plain @app.post route — easier to reach than the ASGI-mounted /mcp.
    # Without auth it normally returns 401; with mcp_auth_enabled=False it passes through
    # to the handler, which returns 422 (missing required fields) or similar — never 401.
    response = mcp_auth_disabled_app.post("/execute_tool", json={})
    assert response.status_code != 401


def test_mcp_auth_disabled_execute_tool_open(mcp_auth_disabled_app):
    """/execute_tool bypass is exercised independently of /mcp."""
    response = mcp_auth_disabled_app.post("/execute_tool", json={"tool_name": "get_graph_stats"})
    assert response.status_code != 401


def test_mcp_auth_explicit_true_follows_auth_enabled(temp_graph_file, temp_static_dirs):
    """mcp_auth_enabled=True (explicit) enforces auth on MCP — same as default None."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        auth_enabled=True,
        auth_username="admin",
        auth_password="secretpassword",
        mcp_auth_enabled=True,
    )
    app = create_app(config)
    client = TestClient(app)
    response = client.post("/execute_tool", json={"tool_name": "get_graph_stats"})
    assert response.status_code == 401


def test_mcp_auth_default_none_follows_auth_enabled(temp_graph_file, temp_static_dirs):
    """mcp_auth_enabled=None (default) keeps existing behaviour: MCP follows auth_enabled."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        auth_enabled=True,
        auth_username="admin",
        auth_password="secretpassword",
        mcp_auth_enabled=None,
    )
    app = create_app(config)
    client = TestClient(app)
    response = client.post("/execute_tool", json={"tool_name": "get_graph_stats"})
    assert response.status_code == 401
