"""Route-level tests for the m0-s4 health-probe endpoints (hc-02/03/07/13)."""

import json
import os
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api_host.config import AppConfig
from backend.api_host.server import create_app


def _make_auth_enabled_app() -> TestClient:
    """Mirror test_auth_middleware.py's _make_config — a fresh graph file per app."""
    fd, graph_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"nodes": [], "edges": []}, f)

    config = AppConfig(
        graph_file=graph_path,
        web_static_path="/nonexistent/web",
        widget_static_path="/nonexistent/widget",
        auth_enabled=True,
        auth_username="admin",
        auth_password="secret",
        mcp_basic_auth=False,
    )
    return TestClient(create_app(config))


class TestReadyzAlias:
    def test_readyz_matches_ready(self, test_app: TestClient):
        ready = test_app.get("/ready")
        readyz = test_app.get("/readyz")
        assert readyz.status_code == ready.status_code
        assert readyz.json()["status"] == ready.json()["status"]
        assert readyz.json()["kind"] == "readiness"


class TestHealthzDeep:
    def test_ok_path(self, test_app: TestClient):
        response = test_app.get("/healthz/deep")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_degraded_path_returns_503(self, test_app: TestClient):
        with patch(
            "backend.api_host.system_routes.check_deep",
            return_value={"status": "degraded", "detail": "graph_storage broken"},
        ):
            response = test_app.get("/healthz/deep")
        assert response.status_code == 503


class TestHealthzStorage:
    def test_ok_path(self, test_app: TestClient):
        response = test_app.get("/healthz/storage")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_degraded_path_returns_503(self, test_app: TestClient):
        with patch(
            "backend.api_host.system_routes.check_storage",
            return_value={"status": "degraded", "detail": "disk full"},
        ):
            response = test_app.get("/healthz/storage")
        assert response.status_code == 503


class TestHealthzSecrets:
    def test_skipped_when_unconfigured(self, test_app: TestClient):
        os.environ.pop("SECRET_STORE_HEALTH_CHECK_SECRET_ID", None)
        response = test_app.get("/healthz/secrets")
        assert response.status_code == 200
        assert response.json()["status"] == "skipped"

    def test_ok_when_configured_and_reachable(self, test_app: TestClient):
        with patch.dict(
            os.environ,
            {"SECRET_STORE_HEALTH_CHECK_SECRET_ID": "cw-saas-session-signing-key"},
        ):
            with patch(
                "backend.api_host.system_routes.check_secret_store",
                return_value={"status": "ok", "detail": None},
            ):
                response = test_app.get("/healthz/secrets")
        assert response.status_code == 200

    def test_degraded_returns_503(self, test_app: TestClient):
        with patch.dict(
            os.environ,
            {"SECRET_STORE_HEALTH_CHECK_SECRET_ID": "cw-saas-session-signing-key"},
        ):
            with patch(
                "backend.api_host.system_routes.check_secret_store",
                return_value={"status": "degraded", "detail": "unreachable"},
            ):
                response = test_app.get("/healthz/secrets")
        assert response.status_code == 503


class TestAuthBypass:
    def test_new_endpoints_exempt_when_auth_enabled(self):
        client = _make_auth_enabled_app()

        for path in [
            "/readyz",
            "/healthz/deep",
            "/healthz/storage",
            "/healthz/secrets",
        ]:
            response = client.get(path)
            assert response.status_code != 401, f"{path} should bypass auth"
