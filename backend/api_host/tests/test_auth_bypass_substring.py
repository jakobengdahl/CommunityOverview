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

def test_session_substring_bypass(client):
    # This is a hypothetical endpoint that contains /api/sessions/ but shouldn't be bypassed
    resp_bypass = client.get("/admin/api/sessions/123/stream")

    assert resp_bypass.status_code == 401, f"Expected 401 for /admin/api/sessions/... got {resp_bypass.status_code}"
