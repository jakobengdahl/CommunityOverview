"""
Tests for Basic Auth middleware modes (auth_enabled vs mcp_basic_auth).
"""

import base64
import json
import os
import tempfile

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api_host.config import AppConfig
from backend.api_host.server import create_app


def _make_config(**overrides) -> tuple:
    """Create an AppConfig with a temp graph file and return (config, graph_path)."""
    fd, graph_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"nodes": [], "edges": []}, f)

    defaults = dict(
        graph_file=graph_path,
        web_static_path="/nonexistent/web",
        widget_static_path="/nonexistent/widget",
        auth_enabled=False,
        auth_username="admin",
        auth_password=None,
        mcp_basic_auth=False,
    )
    defaults.update(overrides)
    return AppConfig(**defaults), graph_path


def _auth_header(username: str, password: str) -> dict:
    """Build a Basic Auth header dict."""
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


class TestMcpBasicAuth:
    """Tests for MCP_BASIC_AUTH mode."""

    def _make_client(self, **config_overrides) -> tuple:
        config, path = _make_config(**config_overrides)
        app = create_app(config)
        return TestClient(app), path

    def test_mcp_basic_auth_blocks_mcp_without_creds(self):
        """MCP endpoint returns 401 when mcp_basic_auth is on and no creds provided."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get("/mcp")
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_blocks_execute_tool_without_creds(self):
        """/execute_tool returns 401 when mcp_basic_auth is on and no creds."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.post("/execute_tool", json={"tool_name": "x"})
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_api_without_creds(self):
        """/api endpoints pass through without auth in MCP-only mode."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get("/api/search", params={"query": "test"})
            # Should NOT be 401 — the request reaches the actual endpoint
            assert resp.status_code != 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_web_without_creds(self):
        """/web endpoints pass through without auth in MCP-only mode."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get("/web/")
            assert resp.status_code != 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_health_without_creds(self):
        """/health always passes through."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get("/health")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_ready_without_creds(self):
        """/ready always passes through."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get("/ready")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_startup_diagnostics_without_creds(self):
        """/diagnostics/startup always passes through."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get("/diagnostics/startup")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_accepts_valid_creds_on_mcp(self):
        """MCP endpoint succeeds with correct credentials."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get(
                "/mcp", headers=_auth_header("admin", "secret")
            )
            # Should not be 401 — auth passed, endpoint responds normally
            assert resp.status_code != 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_rejects_wrong_password(self):
        """MCP endpoint returns 401 with wrong password."""
        client, path = self._make_client(
            mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get(
                "/mcp", headers=_auth_header("admin", "wrong")
            )
            assert resp.status_code == 401
        finally:
            os.unlink(path)


class TestAuthEnabledTakesPrecedence:
    """When auth_enabled=True, ALL endpoints require auth regardless of mcp_basic_auth."""

    def _make_client(self, **config_overrides) -> tuple:
        config, path = _make_config(**config_overrides)
        app = create_app(config)
        return TestClient(app), path

    def test_auth_enabled_blocks_api_without_creds(self):
        """/api requires auth when auth_enabled=True."""
        client, path = self._make_client(
            auth_enabled=True, auth_password="secret"
        )
        try:
            resp = client.get("/api/search", params={"query": "test"})
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_auth_enabled_blocks_mcp_without_creds(self):
        """/mcp requires auth when auth_enabled=True."""
        client, path = self._make_client(
            auth_enabled=True, auth_password="secret"
        )
        try:
            resp = client.get("/mcp")
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_auth_enabled_allows_health(self):
        """/health is always exempt."""
        client, path = self._make_client(
            auth_enabled=True, auth_password="secret"
        )
        try:
            resp = client.get("/health")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_auth_enabled_allows_ready(self):
        """/ready is always exempt."""
        client, path = self._make_client(
            auth_enabled=True, auth_password="secret"
        )
        try:
            resp = client.get("/ready")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_auth_enabled_allows_startup_diagnostics(self):
        """/diagnostics/startup is always exempt."""
        client, path = self._make_client(
            auth_enabled=True, auth_password="secret"
        )
        try:
            resp = client.get("/diagnostics/startup")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_auth_enabled_with_mcp_basic_auth_still_blocks_api(self):
        """auth_enabled takes precedence over mcp_basic_auth."""
        client, path = self._make_client(
            auth_enabled=True, mcp_basic_auth=True, auth_password="secret"
        )
        try:
            resp = client.get("/api/search", params={"query": "test"})
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_sessions_exempt_from_auth_enabled(self):
        """/sessions/ paths are exempt even when auth_enabled=True.

        EventSource cannot send Authorization headers, so the session ID
        itself acts as the access token.  A 401 here would silently break
        all SSE visualization sessions.

        Uses the PATCH /state endpoint (not /stream) because /stream is an
        infinite SSE generator that would block TestClient indefinitely.
        """
        client, path = self._make_client(
            auth_enabled=True, auth_password="secret"
        )
        try:
            resp = client.patch("/sessions/1234-5678/state", json={})
            assert resp.status_code == 200
        finally:
            os.unlink(path)


class TestNoAuthDisabled:
    """When both auth flags are off, nothing is blocked."""

    def _make_client(self, **config_overrides) -> tuple:
        config, path = _make_config(**config_overrides)
        app = create_app(config)
        return TestClient(app), path

    def test_no_auth_allows_mcp(self):
        client, path = self._make_client()
        try:
            resp = client.get("/mcp")
            assert resp.status_code != 401
        finally:
            os.unlink(path)

    def test_no_auth_allows_api(self):
        client, path = self._make_client()
        try:
            resp = client.get("/api/search", params={"query": "test"})
            assert resp.status_code != 401
        finally:
            os.unlink(path)


class TestLogoutRoutes:
    """Logout routes must be reachable without auth and behave
    cloud-agnostically based on auth mode and LOGOUT_REDIRECT_URL."""

    def _make_client(self, **config_overrides) -> tuple:
        config, path = _make_config(**config_overrides)
        app = create_app(config)
        return TestClient(app), path

    def test_logout_exempt_from_auth_enabled(self):
        """/auth/logout must be reachable without credentials when auth_enabled.
        Returns 401 (not 403) to clear browser's cached Basic Auth credentials."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOGOUT_REDIRECT_URL", None)
            client, path = self._make_client(
                auth_enabled=True, auth_password="secret"
            )
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 401
                assert "logged out" in resp.text.lower()
                assert "WWW-Authenticate" in resp.headers
            finally:
                os.unlink(path)

    def test_logged_out_page_exempt_from_auth_enabled(self):
        """/logged-out must render without credentials when auth_enabled."""
        client, path = self._make_client(
            auth_enabled=True, auth_password="secret"
        )
        try:
            resp = client.get("/logged-out")
            assert resp.status_code == 200
            assert "logged out" in resp.text.lower()
        finally:
            os.unlink(path)

    def test_logout_defaults_to_local_logged_out_page(self):
        """Without LOGOUT_REDIRECT_URL and no auth, /auth/logout redirects to /logged-out."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOGOUT_REDIRECT_URL", None)
            client, path = self._make_client()
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 302
                assert resp.headers["location"] == "/logged-out"
            finally:
                os.unlink(path)

    def test_logout_iap_mode_redirects_to_clear_cookie(self):
        """In mcp_basic_auth (IAP) mode, /auth/logout redirects to
        /_gcp_iap/clear_login_cookie to properly end the IAP session."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOGOUT_REDIRECT_URL", None)
            client, path = self._make_client(
                mcp_basic_auth=True, auth_password="secret"
            )
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 302
                assert (
                    resp.headers["location"]
                    == "/_gcp_iap/clear_login_cookie"
                )
            finally:
                os.unlink(path)

    def test_logout_honors_env_redirect_url(self):
        """When LOGOUT_REDIRECT_URL is set, /auth/logout redirects there."""
        with patch.dict(
            os.environ,
            {"LOGOUT_REDIRECT_URL": "https://example.com/sign-out"},
        ):
            client, path = self._make_client()
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 302
                assert (
                    resp.headers["location"]
                    == "https://example.com/sign-out"
                )
            finally:
                os.unlink(path)

    def test_logout_env_overrides_iap_default(self):
        """LOGOUT_REDIRECT_URL takes precedence over the IAP default."""
        with patch.dict(
            os.environ,
            {"LOGOUT_REDIRECT_URL": "https://custom.example.com/bye"},
        ):
            client, path = self._make_client(
                mcp_basic_auth=True, auth_password="secret"
            )
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 302
                assert (
                    resp.headers["location"]
                    == "https://custom.example.com/bye"
                )
            finally:
                os.unlink(path)

    def test_logout_env_overrides_basic_auth_default(self):
        """LOGOUT_REDIRECT_URL takes precedence over the 401 Basic Auth behavior."""
        with patch.dict(
            os.environ,
            {"LOGOUT_REDIRECT_URL": "https://custom.example.com/bye"},
        ):
            client, path = self._make_client(
                auth_enabled=True, auth_password="secret"
            )
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 302
                assert (
                    resp.headers["location"]
                    == "https://custom.example.com/bye"
                )
            finally:
                os.unlink(path)
