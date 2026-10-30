"""
Tests for Basic Auth middleware modes (auth_enabled vs mcp_basic_auth).
"""

import base64
import io
import json
import os
import tempfile
import zipfile

import pytest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.api_host.config import AppConfig
from backend.api_host.server import create_app
from backend.api_host.middleware import add_auth_middleware
from backend.api_host.session_auth import SESSION_COOKIE_NAME, mint_session_cookie


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
        sessions_dir=f"{graph_path}.sessions",
    )
    defaults.update(overrides)
    return AppConfig(**defaults), graph_path


def _auth_header(username: str, password: str) -> dict:
    """Build a Basic Auth header dict."""
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/sessions/id/ops"),
        ("PUT", "/api/import/archive"),
        ("PATCH", "/api/nodes/id/attachments"),
        ("DELETE", "/api/nodes/id/attachments"),
    ],
)
@pytest.mark.parametrize(
    "metadata,allowed",
    [
        ({}, False),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Sec-Fetch-Site": "same-site"}, False),
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "none"}, True),
        ({"X-Requested-With": "api-client"}, True),
        ({"Sec-Fetch-Site": "cross-site", "X-Requested-With": "fetch"}, True),
        ({"Sec-Fetch-Site": "cross-site", "X-Requested-With": " "}, False),
    ],
)
def test_basic_auth_raw_writes_require_fetch_metadata_or_custom_header(
    method, path, metadata, allowed
):
    """Unsafe Basic requests cannot reach raw-body handlers through a cross-site form."""
    app = FastAPI()
    writes = []

    @app.api_route(path, methods=[method])
    async def write(request: Request):
        writes.append(await request.body())
        return {"success": True}

    add_auth_middleware(
        app, AppConfig(auth_enabled=True, auth_username="admin", auth_password="secret")
    )
    response = TestClient(app).request(
        method,
        path,
        content=b"untrusted body",
        headers={
            **_auth_header("admin", "secret"),
            "Content-Type": "text/plain",
            **metadata,
        },
    )
    assert response.status_code == (200 if allowed else 403)
    assert writes == ([b"untrusted body"] if allowed else [])


@pytest.mark.parametrize(
    "authorization,expected",
    [
        ("Bearer api-secret", 200),
        ("bEaReR api-secret", 200),
        ("Bearer wrong-token", 403),
        ("Bearer", 403),
        (None, 403),
    ],
)
def test_bearer_write_with_session_cookie_and_cross_site_metadata(
    authorization, expected
):
    app = FastAPI()
    writes = []

    @app.post("/api/write")
    async def write(request: Request):
        writes.append(await request.body())
        return {"success": True}

    config = AppConfig(auth_enabled=True, auth_bearer_token="api-secret")
    add_auth_middleware(app, config)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, mint_session_cookie(config))
    headers = {"Origin": "https://foreign.example", "Sec-Fetch-Site": "cross-site"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = client.post("/api/write", content=b"payload", headers=headers)
    assert response.status_code == expected
    assert writes == ([b"payload"] if expected == 200 else [])


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/sessions/id/ops"),
        ("PUT", "/api/import/archive"),
        ("PATCH", "/api/nodes/id/attachments"),
        ("DELETE", "/api/nodes/id/attachments"),
    ],
)
@pytest.mark.parametrize(
    "metadata,allowed",
    [
        ({}, True),
        ({"Origin": "http://testserver"}, True),
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "none"}, True),
        ({"Origin": "https://foreign.example"}, False),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Sec-Fetch-Site": "same-site"}, False),
        ({"Sec-Fetch-Site": "cross-site", "X-Requested-With": "fetch"}, True),
        ({"Sec-Fetch-Site": "cross-site", "X-Requested-With": " "}, False),
    ],
)
def test_session_cookie_raw_writes_require_same_origin_or_custom_header(
    method, path, metadata, allowed
):
    """Unsafe cookie requests cannot reach raw-body handlers from another origin."""
    app = FastAPI()
    writes = []

    @app.api_route(path, methods=[method])
    async def write(request: Request):
        writes.append(await request.body())
        return {"success": True}

    config = AppConfig(auth_enabled=True, auth_username="admin", auth_password="secret")
    add_auth_middleware(app, config)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, mint_session_cookie(config))
    response = client.request(
        method,
        path,
        content=b"untrusted body",
        headers={"Content-Type": "text/plain", **metadata},
    )
    assert response.status_code == (200 if allowed else 403)
    assert writes == ([b"untrusted body"] if allowed else [])


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("credential", ["basic", "cookie"])
def test_authenticated_safe_methods_skip_write_guard(method, credential):
    """Cross-site metadata alone does not refuse an authenticated safe request."""
    app = FastAPI()

    @app.api_route("/api/read", methods=[method])
    async def read():
        return {"success": True}

    config = AppConfig(auth_enabled=True, auth_username="admin", auth_password="secret")
    add_auth_middleware(app, config)
    client = TestClient(app)
    headers = {"Origin": "https://foreign.example", "Sec-Fetch-Site": "cross-site"}
    if credential == "basic":
        headers.update(_auth_header("admin", "secret"))
    else:
        client.cookies.set(SESSION_COOKIE_NAME, mint_session_cookie(config))
    assert client.request(method, "/api/read", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic " + base64.b64encode(b"admin:wrong").decode(), "Bearer wrong"],
)
def test_custom_header_does_not_replace_credentials(authorization):
    """X-Requested-With only satisfies the write guard; it never authenticates."""
    app = FastAPI()
    writes = []

    @app.post("/api/write")
    async def write(request: Request):
        writes.append(await request.body())
        return {"success": True}

    add_auth_middleware(
        app,
        AppConfig(
            auth_enabled=True,
            auth_username="admin",
            auth_password="secret",
            auth_bearer_token="api-secret",
        ),
    )
    headers = {"X-Requested-With": "fetch", "Sec-Fetch-Site": "same-origin"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = TestClient(app).post("/api/write", content=b"payload", headers=headers)
    assert response.status_code == 401
    assert writes == []


class TestMcpBasicAuth:
    """Tests for MCP_BASIC_AUTH mode."""

    def _make_client(self, **config_overrides) -> tuple:
        config, path = _make_config(**config_overrides)
        app = create_app(config)
        return TestClient(app), path

    def test_mcp_basic_auth_blocks_mcp_without_creds(self):
        """MCP endpoint returns 401 when mcp_basic_auth is on and no creds provided."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/mcp")
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_blocks_execute_tool_without_creds(self):
        """/execute_tool returns 401 when mcp_basic_auth is on and no creds."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.post("/execute_tool", json={"tool_name": "x"})
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_api_without_creds(self):
        """/api endpoints pass through without auth in MCP-only mode."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/api/search", params={"query": "test"})
            # Should NOT be 401 — the request reaches the actual endpoint
            assert resp.status_code != 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_web_without_creds(self):
        """/web endpoints pass through without auth in MCP-only mode."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/web/")
            assert resp.status_code != 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_health_without_creds(self):
        """/health always passes through."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/health")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_ready_without_creds(self):
        """/ready always passes through."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/ready")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_allows_startup_diagnostics_without_creds(self):
        """/diagnostics/startup always passes through."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/diagnostics/startup")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_accepts_valid_creds_on_mcp(self):
        """MCP endpoint succeeds with correct credentials."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/mcp", headers=_auth_header("admin", "secret"))
            # Should not be 401 — auth passed, endpoint responds normally
            assert resp.status_code != 401
        finally:
            os.unlink(path)

    def test_mcp_basic_auth_rejects_wrong_password(self):
        """MCP endpoint returns 401 with wrong password."""
        client, path = self._make_client(mcp_basic_auth=True, auth_password="secret")
        try:
            resp = client.get("/mcp", headers=_auth_header("admin", "wrong"))
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
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get("/api/search", params={"query": "test"})
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_auth_enabled_blocks_mcp_without_creds(self):
        """/mcp requires auth when auth_enabled=True."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get("/mcp")
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_auth_enabled_allows_health(self):
        """/health is always exempt."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get("/health")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_auth_enabled_allows_ready(self):
        """/ready is always exempt."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get("/ready")
            assert resp.status_code == 200
        finally:
            os.unlink(path)

    def test_auth_enabled_allows_startup_diagnostics(self):
        """/diagnostics/startup is always exempt."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
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
        """Legacy /sessions/ paths are exempt even when auth_enabled=True.

        EventSource cannot send Authorization headers, so the session ID
        itself acts as the access token.  A 401 here would silently break
        the MCP visualization push stream.

        Uses an invalid id on /sessions/{id}/stream: the handler returns 400
        (invalid format) before starting the infinite SSE generator, which
        proves the request reached the handler rather than being 401'd.
        """
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get("/sessions/not-valid/stream")
            assert resp.status_code == 400
        finally:
            os.unlink(path)

    def test_shared_session_stream_exempt_from_auth_enabled(self):
        """The new /api/sessions/{id}/stream is EventSource-opened too, so it is
        exempt (design §3.9, alternative A). An invalid id returns 400 — proving
        the request reached the handler rather than being 401'd by the middleware
        (and avoiding the infinite SSE generator a valid id would start)."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get(
                "/api/sessions/not-valid/stream", params={"client_id": "c1"}
            )
            assert resp.status_code == 400
        finally:
            os.unlink(path)

    def test_shared_session_ops_still_guarded(self):
        """Only the stream is bypassed — the fetch-reachable ops endpoint (which
        can send an Authorization header) still requires auth."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.post(
                "/api/sessions/1234-5678/ops",
                json={"client_id": "c1", "ops": []},
            )
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_only_the_shared_session_stream_route_is_bypassed(self):
        """The stream bypass covers exactly {prefix}/sessions/{id}/stream. Other
        paths that merely start with {prefix}/sessions/ and end in /stream —
        the session CRUD route addressed as id "stream", or a deeper sub-path —
        must still be 401'd rather than handed to routing unauthenticated."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            for method, url in [
                ("GET", "/api/sessions/stream"),
                ("PATCH", "/api/sessions/stream"),
                ("DELETE", "/api/sessions/stream"),
                ("GET", "/api/sessions/1234-5678/extra/stream"),
            ]:
                resp = client.request(method, url, json={"name": "x"})
                assert resp.status_code == 401, (method, url, resp.status_code)
        finally:
            os.unlink(path)


class TestUnauthorizedContentNegotiation:
    """The 401 challenge is negotiated by Accept.

    Regression for the SSPCloud Edge/Chrome bug: a browser navigation must never
    land on a blank page rendering the raw {"detail": "Authentication required"}
    JSON, and no 401 may carry a WWW-Authenticate header — that header makes
    Chromium pop the native Basic dialog (on the page OR on a subresource such as
    the favicon), and that dialog can't drive the cookie login. Browsers
    (Accept: text/html) get an HTML sign-in page; everything else gets the JSON
    body. Neither carries WWW-Authenticate.
    """

    def _make_client(self, **config_overrides) -> tuple:
        config, path = _make_config(**config_overrides)
        app = create_app(config)
        return TestClient(app), path

    def test_browser_navigation_gets_html_without_www_authenticate(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get("/", headers={"Accept": "text/html"})
            assert resp.status_code == 401
            assert resp.headers["content-type"].startswith("text/html")
            # No WWW-Authenticate: browsers must render the form, not the native
            # Basic dialog (Chromium pops it before showing the body).
            assert "WWW-Authenticate" not in resp.headers
            # Never cache the sign-in page.
            assert resp.headers.get("cache-control") == "no-store"
            assert "Sign in" in resp.text
            # The raw backend error body must not be what the browser renders.
            assert '{"detail": "Authentication required"}' not in resp.text
        finally:
            os.unlink(path)

    def test_api_client_gets_json_body_without_www_authenticate(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get(
                "/api/search",
                params={"query": "x"},
                headers={"Accept": "application/json"},
            )
            assert resp.status_code == 401
            assert resp.headers["content-type"].startswith("application/json")
            assert resp.json() == {"detail": "Authentication required"}
            # A subresource 401 (favicon, XHR) carrying this header would pop the
            # native dialog in Chrome — it must not be sent.
            assert "WWW-Authenticate" not in resp.headers
        finally:
            os.unlink(path)

    def test_favicon_bypasses_auth(self):
        """Favicons must be reachable unauthenticated: a guarded (401) favicon
        subresource is what triggered the Chrome native Basic dialog."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            for fav in [
                "/favicon.ico",
                "/favicon.svg",
                "/favicon.png",
                "/graph-icon.svg",
            ]:
                resp = client.get(fav, headers={"Accept": "image/avif,image/webp,*/*"})
                assert resp.status_code != 401, fav
        finally:
            os.unlink(path)

    def test_default_accept_gets_json_body(self):
        """A client sending Accept: */* (curl, many SDKs) keeps the JSON body."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get(
                "/api/search", params={"query": "x"}, headers={"Accept": "*/*"}
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Authentication required"}
        finally:
            os.unlink(path)

    def test_invalid_credentials_negotiated_too(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            bad = _auth_header("admin", "wrong")
            html_resp = client.get("/", headers={**bad, "Accept": "text/html"})
            assert html_resp.status_code == 401
            assert html_resp.headers["content-type"].startswith("text/html")
            assert "Sign in" in html_resp.text

            json_resp = client.get(
                "/api/search",
                params={"query": "x"},
                headers={**bad, "Accept": "application/json"},
            )
            assert json_resp.status_code == 401
            assert json_resp.json() == {"detail": "Invalid credentials"}
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
            client, path = self._make_client(auth_enabled=True, auth_password="secret")
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 401
                assert "logged out" in resp.text.lower()
                assert "WWW-Authenticate" in resp.headers
            finally:
                os.unlink(path)

    def test_logged_out_page_exempt_from_auth_enabled(self):
        """/logged-out must render without credentials when auth_enabled."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
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
                assert resp.headers["location"] == "/_gcp_iap/clear_login_cookie"
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
                assert resp.headers["location"] == "https://example.com/sign-out"
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
                assert resp.headers["location"] == "https://custom.example.com/bye"
            finally:
                os.unlink(path)

    def test_logout_env_overrides_basic_auth_default(self):
        """LOGOUT_REDIRECT_URL takes precedence over the 401 Basic Auth behavior."""
        with patch.dict(
            os.environ,
            {"LOGOUT_REDIRECT_URL": "https://custom.example.com/bye"},
        ):
            client, path = self._make_client(auth_enabled=True, auth_password="secret")
            try:
                resp = client.get("/auth/logout", follow_redirects=False)
                assert resp.status_code == 302
                assert resp.headers["location"] == "https://custom.example.com/bye"
            finally:
                os.unlink(path)


class TestSessionCookieLogin:
    """Form login (POST /auth/login) mints a signed session cookie the middleware
    accepts, giving a browser-independent auth flow that does not depend on the
    native HTTP Basic dialog (which Edge never surfaces on the sspcloud deploy).
    """

    def _make_client(self, **config_overrides) -> tuple:
        config, path = _make_config(**config_overrides)
        app = create_app(config)
        return TestClient(app), path

    def test_signin_page_renders_login_form(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.get("/web/", headers={"Accept": "text/html"})
            assert resp.status_code == 401
            assert 'action="/auth/login"' in resp.text
            assert 'name="username"' in resp.text
            assert 'name="password"' in resp.text
            # The requested path is preserved as the post-login return target.
            assert 'name="next"' in resp.text
            assert 'value="/web/"' in resp.text
        finally:
            os.unlink(path)

    def test_form_login_sets_cookie_and_authenticates(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.post(
                "/auth/login",
                data={"username": "admin", "password": "secret", "next": "/web/"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/web/"
            assert SESSION_COOKIE_NAME in resp.cookies
            # The cookie is now in the client jar; a guarded request is authorised.
            follow = client.get("/api/search", params={"query": "x"})
            assert follow.status_code != 401
        finally:
            os.unlink(path)

    @pytest.mark.parametrize(
        "content_type",
        ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""],
    )
    def test_cookie_import_rejects_simple_content_types(self, content_type):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            login = client.post(
                "/auth/login",
                data={"username": "admin", "password": "secret"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            assert SESSION_COOKIE_NAME in client.cookies
            document = {
                "nodes": [{"id": "csrf", "type": "Actor", "name": "CSRF"}],
                "edges": [],
            }
            response = client.post(
                "/api/import",
                content=json.dumps(document),
                headers={
                    "Content-Type": content_type,
                    "Origin": "https://sibling.example.com",
                    "Sec-Fetch-Site": "same-site",
                },
            )
            assert response.status_code == 403
            assert client.get("/api/nodes/csrf").status_code == 404
            assert client.post("/api/import", json=document).status_code == 200
        finally:
            os.unlink(path)

    @pytest.mark.parametrize(
        "metadata",
        [
            {"Origin": "https://sibling.example.com", "Sec-Fetch-Site": "same-site"},
            {"Origin": "https://sibling.example.com"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Origin": "null"},
        ],
    )
    def test_cookie_archive_import_rejects_foreign_origin(self, metadata, monkeypatch):
        from backend.agents.execution.import_worker import drain_import_jobs

        # Finish the seed import's real embedding work before taking snapshots.
        # Its asynchronous commit legitimately advances node revisions; racing
        # that commit would misattribute a change to the rejected CSRF request.
        monkeypatch.setattr(
            "backend.service.import_service._start_embedding_drain", drain_import_jobs
        )
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            login = client.post(
                "/auth/login",
                data={"username": "admin", "password": "secret"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            original = {
                "nodes": [{"id": "original", "type": "Actor", "name": "Original"}],
                "edges": [],
            }
            assert client.post("/api/import", json=original).status_code == 200
            before = client.get("/api/export").json()
            document = {
                "nodes": [{"id": "csrf", "type": "Actor", "name": "CSRF"}],
                "edges": [],
            }
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("graph.json", json.dumps(document))
            upload = {"file": ("graph.zip", archive.getvalue(), "application/zip")}
            response = client.post(
                "/api/import/archive", files=upload, headers=metadata
            )
            assert response.status_code == 403
            after = client.get("/api/export").json()
            # Export timestamps change even when the graph is unchanged.
            before.pop("exportDate")
            after.pop("exportDate")
            assert after == before
            # The identical archive is valid and can replace the graph locally.
            response = client.post(
                "/api/import/archive",
                files=upload,
                headers={
                    "Origin": "http://testserver",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            assert response.status_code == 200, response.text
            assert client.get("/api/nodes/csrf").status_code == 200
            assert client.get("/api/nodes/original").status_code == 404
        finally:
            os.unlink(path)

    def test_form_login_wrong_password_401_no_cookie(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.post(
                "/auth/login",
                data={"username": "admin", "password": "nope"},
                follow_redirects=False,
            )
            assert resp.status_code == 401
            assert "Invalid username or password" in resp.text
            assert SESSION_COOKIE_NAME not in resp.cookies
        finally:
            os.unlink(path)

    def test_login_rejects_open_redirect_next(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            resp = client.post(
                "/auth/login",
                data={
                    "username": "admin",
                    "password": "secret",
                    "next": "//evil.example.com/",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/web/"
        finally:
            os.unlink(path)

    def test_tampered_session_cookie_rejected(self):
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            client.cookies.set(SESSION_COOKIE_NAME, "v1.9999999999.deadbeef")
            resp = client.get(
                "/api/search",
                params={"query": "x"},
                headers={"Accept": "application/json"},
            )
            assert resp.status_code == 401
        finally:
            os.unlink(path)

    def test_logout_with_session_cookie_clears_and_no_popup(self):
        """A cookie-authenticated logout redirects to /logged-out (no 401 Basic
        flush, so no native dialog re-opens) and clears the session."""
        client, path = self._make_client(auth_enabled=True, auth_password="secret")
        try:
            client.post(
                "/auth/login",
                data={"username": "admin", "password": "secret"},
                follow_redirects=False,
            )
            resp = client.get("/auth/logout", follow_redirects=False)
            assert resp.status_code == 302
            assert resp.headers["location"] == "/logged-out"
            # Session is really gone: a guarded request is unauthorised again.
            after = client.get(
                "/api/search",
                params={"query": "x"},
                headers={"Accept": "application/json"},
            )
            assert after.status_code == 401
        finally:
            os.unlink(path)
