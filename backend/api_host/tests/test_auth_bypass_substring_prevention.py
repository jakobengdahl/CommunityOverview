from fastapi.testclient import TestClient
from backend.api_host.server import create_app
from backend.api_host.config import AppConfig

def test_auth_bypass_substring_prevention():
    """Verify that substring bypass on /api/sessions/ paths is blocked."""
    config = AppConfig(auth_enabled=True, auth_password="secret")
    app = create_app(config)
    client = TestClient(app)

    # An unauthorized path containing the substring should be blocked
    resp = client.get("/admin/api/sessions/123/stream")
    assert resp.status_code == 401
