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
