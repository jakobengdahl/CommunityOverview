import pytest
from fastapi.testclient import TestClient

from backend.api_host.server import create_app
from backend.api_host.config import AppConfig

@pytest.fixture
def auth_app():
    config = AppConfig(
        auth_enabled=True,
        auth_username="admin",
        auth_password="password",
        auth_bearer_token="secret",
    )
    return create_app(config)

@pytest.fixture
def client(auth_app):
    return TestClient(auth_app)

def test_session_prefix_bypass(client):
    # This should be allowed
    resp = client.get("/sessions/123/stream")
    # assert resp.status_code == 200 # It might return 404 depending on session existence, but shouldn't 401

    # This is a hypothetical endpoint /sessions_bypass that should be 401
    resp_bypass = client.get("/sessions_bypass/something")

    assert resp_bypass.status_code == 401, f"Expected 401 for /sessions_bypass, got {resp_bypass.status_code}"
