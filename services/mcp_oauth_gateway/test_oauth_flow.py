"""
Tests for the MCP OAuth Gateway – focus on redirect_uri handling.

Uses unittest.mock to isolate from Google OIDC and config env vars.
"""

import asyncio
import hashlib
import base64
import importlib
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import jwt

# Set required env vars before importing config (it reads them at import time)
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
os.environ.setdefault("GW_JWT_SIGNING_KEY", "test-jwt-key-at-least-32-chars!!")
os.environ.setdefault("TEST_USERS", "alice@example.com,bob@example.com")
os.environ.setdefault("UPSTREAM_MCP_BASE_URL", "http://localhost:9000")
os.environ.setdefault("PUBLIC_BASE_URL", "https://gateway.example.com")
os.environ.setdefault("GATEWAY_API_KEY", "static-test-api-key")

import auth
import config
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def _make_pkce_pair():
    """Generate a code_verifier and its S256 code_challenge."""
    verifier = "test-verifier-that-is-long-enough-for-pkce-requirements"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# RSA keypair used to sign fake Google ID tokens in tests. The gateway verifies
# these against a mocked JWKS client that returns the matching public key.
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

_GOOGLE_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_google_id_token(**overrides) -> str:
    """Sign a Google-style ID token (RS256) with the test key."""
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": config.GOOGLE_OAUTH_CLIENT_ID,
        "email": "alice@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, _GOOGLE_TEST_KEY, algorithm="RS256")


def _mock_google_token_exchange(id_token: str):
    """Return a patch context that fakes Google's token endpoint + JWKS.

    The httpx client yields ``id_token``; the JWKS client returns the public
    half of ``_GOOGLE_TEST_KEY`` so signature verification succeeds.
    """
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"id_token": id_token}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    signing_key = MagicMock()
    signing_key.key = _GOOGLE_TEST_KEY.public_key()
    jwks_client = MagicMock()
    jwks_client.get_signing_key_from_jwt.return_value = signing_key

    return mock_client, jwks_client


class TestGatewayProxyAuth(unittest.TestCase):
    """Tests for bearer auth on the proxy endpoints."""

    def test_static_api_key_allows_request_without_jwt(self):
        with patch("proxy.proxy_sse", new=AsyncMock(return_value=MagicMock(status_code=200))):
            resp = client.get("/sse", headers={"Authorization": "Bearer static-test-api-key"})
        assert resp.status_code == 200

    def test_non_matching_api_key_falls_back_to_jwt_validation(self):
        with patch("auth.validate_token", return_value={"sub": "alice@example.com"}) as mock_validate:
            with patch("proxy.proxy_sse", new=AsyncMock(return_value=MagicMock(status_code=200))):
                resp = client.get("/sse", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 200
        mock_validate.assert_called_once_with("wrong-key")

    def test_invalid_non_matching_api_key_is_rejected(self):
        with patch("auth.validate_token", return_value=None) as mock_validate:
            resp = client.get("/sse", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401
        mock_validate.assert_called_once_with("wrong-key")

    def test_absent_gateway_api_key_skips_static_check(self):
        with patch.object(config, "GATEWAY_API_KEY", None):
            with patch("auth.validate_token", return_value={"sub": "alice@example.com"}) as mock_validate:
                with patch("proxy.proxy_sse", new=AsyncMock(return_value=MagicMock(status_code=200))):
                    resp = client.get("/sse", headers={"Authorization": "Bearer static-test-api-key"})
        assert resp.status_code == 200
        mock_validate.assert_called_once_with("static-test-api-key")


class TestStreamableHttpTransport(unittest.TestCase):
    """POST/GET/DELETE /mcp — Streamable HTTP transport (MCP ≥ 2025-03-26)."""

    def test_post_mcp_requires_auth(self):
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert resp.status_code == 401

    def test_post_mcp_is_proxied_when_authorized(self):
        with patch("proxy.proxy_streamable_http",
                   new=AsyncMock(return_value=MagicMock(status_code=200))) as proxied:
            resp = client.post(
                "/mcp",
                headers={"Authorization": "Bearer static-test-api-key"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            )
        assert resp.status_code == 200
        proxied.assert_awaited_once()

    def test_get_and_delete_mcp_are_proxied(self):
        for method in ("get", "delete"):
            with patch("proxy.proxy_streamable_http",
                       new=AsyncMock(return_value=MagicMock(status_code=200))) as proxied:
                resp = getattr(client, method)(
                    "/mcp", headers={"Authorization": "Bearer static-test-api-key"}
                )
            assert resp.status_code == 200, method
            proxied.assert_awaited_once()

    def test_upstream_is_asked_for_the_trailing_slash_path(self):
        """The bare mount path would 307 to /mcp/ carrying the upstream's own host."""
        import proxy as proxy_module

        captured = {}

        def fake_build_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            return MagicMock()

        upstream_resp = MagicMock()
        upstream_resp.headers = {"content-type": "application/json"}
        upstream_resp.status_code = 200
        upstream_resp.aread = AsyncMock(return_value=b"{}")
        upstream_resp.aclose = AsyncMock()

        with patch.object(proxy_module._client, "build_request", side_effect=fake_build_request):
            with patch.object(proxy_module._client, "send",
                              new=AsyncMock(return_value=upstream_resp)):
                request = MagicMock()
                request.method = "POST"
                request.query_params = {}
                request.headers = {}
                request.body = AsyncMock(return_value=b"{}")
                asyncio.run(proxy_module.proxy_streamable_http(request))

        assert captured["url"] == config.UPSTREAM_MCP_BASE_URL + "/mcp/"

    def test_redirect_to_the_upstream_host_is_rewritten_to_the_gateway(self):
        import httpx2 as httpx

        import proxy as proxy_module

        upstream = config.UPSTREAM_MCP_BASE_URL.rstrip("/")
        headers = httpx.Headers({"location": f"{upstream}/mcp/?session_id=abc"})
        filtered = proxy_module._response_headers(headers)
        assert filtered["location"] == (
            config.PUBLIC_BASE_URL.rstrip("/") + "/mcp/?session_id=abc"
        )

    def test_redirect_to_an_unrelated_host_is_left_alone(self):
        import httpx2 as httpx

        import proxy as proxy_module

        headers = httpx.Headers({"location": "https://example.com/elsewhere"})
        filtered = proxy_module._response_headers(headers)
        assert filtered["location"] == "https://example.com/elsewhere"

    def test_session_id_header_survives_the_response_filter(self):
        import httpx2 as httpx

        import proxy as proxy_module

        headers = httpx.Headers({
            "content-type": "application/json",
            "content-length": "12",
            "mcp-session-id": "abc-123",
        })
        filtered = proxy_module._response_headers(headers)
        assert filtered["mcp-session-id"] == "abc-123"
        assert "content-length" not in {k.lower() for k in filtered}


class TestAuthorizeEndpoint(unittest.TestCase):
    """Tests for GET /authorize."""

    def test_accepts_external_redirect_uri(self):
        """External redirect_uri (not the gateway callback) should be accepted."""
        _, challenge = _make_pkce_pair()
        resp = client.get(
            "/authorize",
            params={
                "client_id": "chatgpt",
                "redirect_uri": "https://chatgpt.com/aip/plugin-abc/oauth/callback",
                "state": "some-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        # Should redirect to Google (302), not reject with 400
        assert resp.status_code == 302
        assert "accounts.google.com" in resp.headers["location"]

    def test_accepts_gateway_callback_redirect_uri(self):
        """The gateway's own callback URL should also still work."""
        _, challenge = _make_pkce_pair()
        resp = client.get(
            "/authorize",
            params={
                "client_id": "chatgpt",
                "redirect_uri": config.PUBLIC_BASE_URL + "/callback",
                "state": "some-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

    def test_rejects_missing_pkce(self):
        resp = client.get(
            "/authorize",
            params={
                "client_id": "chatgpt",
                "redirect_uri": "https://example.com/callback",
                "state": "s",
                "code_challenge": "",
                "code_challenge_method": "S256",
            },
        )
        assert resp.status_code == 400


class TestTokenEndpointRedirectUri(unittest.TestCase):
    """Tests for redirect_uri validation in POST /token."""

    def _issue_code(self, redirect_uri: str) -> tuple:
        """Helper: issue an auth code with a given redirect_uri and return (code, verifier)."""
        verifier, challenge = _make_pkce_pair()
        code = auth.issue_auth_code(
            email="alice@example.com",
            code_challenge=challenge,
            redirect_uri=redirect_uri,
        )
        return code, verifier

    def test_matching_redirect_uri_succeeds(self):
        """Token exchange should succeed when redirect_uri matches."""
        redirect = "https://chatgpt.com/aip/plugin-abc/oauth/callback"
        code, verifier = self._issue_code(redirect)

        resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "Bearer"

    def test_mismatched_redirect_uri_fails(self):
        """Token exchange should fail when redirect_uri does not match."""
        code, verifier = self._issue_code("https://chatgpt.com/callback")

        resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "https://evil.com/steal",
            },
        )
        assert resp.status_code == 400

    def test_missing_redirect_uri_fails(self):
        """Token exchange should fail when redirect_uri is omitted."""
        code, verifier = self._issue_code("https://chatgpt.com/callback")

        resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                # redirect_uri intentionally omitted
            },
        )
        assert resp.status_code == 400

    def test_json_body_also_works(self):
        """Token endpoint should accept JSON bodies with redirect_uri."""
        redirect = "https://other-client.example.com/cb"
        code, verifier = self._issue_code(redirect)

        resp = client.post(
            "/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect,
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()


class TestAuthModuleRedirectUri(unittest.TestCase):
    """Unit tests for auth.exchange_code_for_token redirect_uri check."""

    def test_exchange_with_correct_redirect_uri(self):
        verifier, challenge = _make_pkce_pair()
        redirect = "https://app.example.com/oauth/done"
        code = auth.issue_auth_code("alice@example.com", challenge, redirect)

        token = auth.exchange_code_for_token(code, verifier, redirect)
        assert token is not None

    def test_exchange_with_wrong_redirect_uri(self):
        verifier, challenge = _make_pkce_pair()
        redirect = "https://app.example.com/oauth/done"
        code = auth.issue_auth_code("alice@example.com", challenge, redirect)

        token = auth.exchange_code_for_token(code, verifier, "https://evil.com/steal")
        assert token is None

    def test_exchange_with_empty_redirect_uri(self):
        verifier, challenge = _make_pkce_pair()
        redirect = "https://app.example.com/oauth/done"
        code = auth.issue_auth_code("alice@example.com", challenge, redirect)

        token = auth.exchange_code_for_token(code, verifier, "")
        assert token is None


class TestGatewayJwt(unittest.TestCase):
    """Round-trip tests for the gateway's JWT minting and verification.

    These guard the python-jose -> PyJWT migration: the gateway signs its own
    access tokens (HS256) and verifies them on every proxied request, so a
    regression here silently breaks all authenticated access.
    """

    def test_issued_token_validates_and_carries_claims(self):
        verifier, challenge = _make_pkce_pair()
        code = auth.issue_auth_code("alice@example.com", challenge, "https://app/cb")
        token = auth.exchange_code_for_token(code, verifier, "https://app/cb")
        assert token is not None

        claims = auth.validate_token(token)
        assert claims is not None
        assert claims["sub"] == "alice@example.com"
        assert claims["aud"] == config.PUBLIC_BASE_URL

    def test_validate_token_rejects_wrong_signing_key(self):
        verifier, challenge = _make_pkce_pair()
        code = auth.issue_auth_code("bob@example.com", challenge, "https://app/cb")
        token = auth.exchange_code_for_token(code, verifier, "https://app/cb")

        with patch.object(config, "GW_JWT_SIGNING_KEY", "a-different-signing-key-32-chars!!"):
            assert auth.validate_token(token) is None

    def test_validate_token_rejects_expired_token(self):
        past = int(time.time()) - 10
        claims = {
            "sub": "alice@example.com",
            "aud": config.PUBLIC_BASE_URL,
            "iat": past - 60,
            "exp": past,
        }
        expired = jwt.encode(claims, config.GW_JWT_SIGNING_KEY, algorithm=config.JWT_ALGORITHM)
        assert auth.validate_token(expired) is None

    def test_validate_token_rejects_wrong_audience(self):
        now = int(time.time())
        claims = {
            "sub": "alice@example.com",
            "aud": "https://someone-else.example.com",
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(claims, config.GW_JWT_SIGNING_KEY, algorithm=config.JWT_ALGORITHM)
        assert auth.validate_token(token) is None

    def test_validate_token_rejects_garbage(self):
        assert auth.validate_token("not-a-jwt") is None

    def test_exchange_google_code_verifies_signature_and_reads_email(self):
        """A validly-signed Google ID token yields the email."""
        id_token = _make_google_id_token()
        mock_client, jwks_client = _mock_google_token_exchange(id_token)

        with patch("auth.httpx.AsyncClient", return_value=mock_client):
            with patch("auth._get_google_jwks_client", return_value=jwks_client):
                email = asyncio.run(auth.exchange_google_code("google-auth-code"))
        assert email == "alice@example.com"

    def test_exchange_google_code_rejects_unverified_email(self):
        id_token = _make_google_id_token(email_verified=False)
        mock_client, jwks_client = _mock_google_token_exchange(id_token)

        with patch("auth.httpx.AsyncClient", return_value=mock_client):
            with patch("auth._get_google_jwks_client", return_value=jwks_client):
                email = asyncio.run(auth.exchange_google_code("google-auth-code"))
        assert email is None

    def test_exchange_google_code_rejects_bad_signature(self):
        """A token signed with a different key must be rejected."""
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "aud": config.GOOGLE_OAUTH_CLIENT_ID,
                "email": "alice@example.com",
                "email_verified": True,
                "iat": now,
                "exp": now + 3600,
            },
            other_key,
            algorithm="RS256",
        )
        mock_client, jwks_client = _mock_google_token_exchange(id_token)

        with patch("auth.httpx.AsyncClient", return_value=mock_client):
            with patch("auth._get_google_jwks_client", return_value=jwks_client):
                email = asyncio.run(auth.exchange_google_code("google-auth-code"))
        assert email is None

    def test_exchange_google_code_rejects_wrong_audience(self):
        id_token = _make_google_id_token(aud="some-other-client")
        mock_client, jwks_client = _mock_google_token_exchange(id_token)

        with patch("auth.httpx.AsyncClient", return_value=mock_client):
            with patch("auth._get_google_jwks_client", return_value=jwks_client):
                email = asyncio.run(auth.exchange_google_code("google-auth-code"))
        assert email is None

    def test_exchange_google_code_enforces_nonce(self):
        id_token = _make_google_id_token(nonce="expected-nonce")
        mock_client, jwks_client = _mock_google_token_exchange(id_token)

        with patch("auth.httpx.AsyncClient", return_value=mock_client):
            with patch("auth._get_google_jwks_client", return_value=jwks_client):
                good = asyncio.run(
                    auth.exchange_google_code(
                        "google-auth-code", expected_nonce="expected-nonce"
                    )
                )
                bad = asyncio.run(
                    auth.exchange_google_code(
                        "google-auth-code", expected_nonce="different-nonce"
                    )
                )
        assert good == "alice@example.com"
        assert bad is None


class TestCorsConfiguration(unittest.TestCase):
    """Tests that CORS credentials are disabled when allow_origins is wildcard."""

    def test_wildcard_origins_disables_credentials(self):
        """When CORS_ALLOWED_ORIGINS is '*', allow_credentials must be False."""
        with patch.dict(os.environ, {"CORS_ALLOWED_ORIGINS": "*"}):
            import importlib
            import config as cfg
            importlib.reload(cfg)
            allow_credentials = "*" not in cfg.CORS_ALLOWED_ORIGINS
        self.assertFalse(allow_credentials)

    def test_specific_origins_enables_credentials(self):
        """When CORS_ALLOWED_ORIGINS lists specific origins, allow_credentials is True."""
        with patch.dict(os.environ, {"CORS_ALLOWED_ORIGINS": "https://app.example.com,https://other.example.com"}):
            import importlib
            import config as cfg
            importlib.reload(cfg)
            allow_credentials = "*" not in cfg.CORS_ALLOWED_ORIGINS
        self.assertTrue(allow_credentials)
        self.assertEqual(cfg.CORS_ALLOWED_ORIGINS, ["https://app.example.com", "https://other.example.com"])

    def test_default_cors_origins_is_wildcard(self):
        """Without CORS_ALLOWED_ORIGINS set, the default is ['*']."""
        env = {k: v for k, v in os.environ.items() if k != "CORS_ALLOWED_ORIGINS"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import config as cfg
            importlib.reload(cfg)
        self.assertEqual(cfg.CORS_ALLOWED_ORIGINS, ["*"])


class TestRedirectOriginAllowList(unittest.TestCase):
    """Tests for the ALLOWED_REDIRECT_ORIGINS enforcement at /authorize."""

    def test_disallowed_origin_rejected_when_list_configured(self):
        _, challenge = _make_pkce_pair()
        with patch.object(config, "ALLOWED_REDIRECT_ORIGINS", ["https://chatgpt.com"]):
            resp = client.get(
                "/authorize",
                params={
                    "client_id": "c",
                    "redirect_uri": "https://evil.example.com/callback",
                    "state": "s",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 400

    def test_allowed_origin_accepted_when_list_configured(self):
        _, challenge = _make_pkce_pair()
        with patch.object(config, "ALLOWED_REDIRECT_ORIGINS", ["https://chatgpt.com"]):
            resp = client.get(
                "/authorize",
                params={
                    "client_id": "c",
                    "redirect_uri": "https://chatgpt.com/aip/plugin-abc/oauth/callback",
                    "state": "s",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302

    def test_loopback_always_allowed(self):
        _, challenge = _make_pkce_pair()
        with patch.object(config, "ALLOWED_REDIRECT_ORIGINS", ["https://chatgpt.com"]):
            resp = client.get(
                "/authorize",
                params={
                    "client_id": "c",
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "state": "s",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302


class TestAllowListRevocation(unittest.TestCase):
    """A token whose subject left TEST_USERS must be rejected on the next call."""

    def test_delisted_subject_rejected(self):
        with patch("auth.validate_token", return_value={"sub": "removed@example.com"}):
            resp = client.get(
                "/sse", headers={"Authorization": "Bearer some-valid-looking-jwt"}
            )
        assert resp.status_code == 401

    def test_allow_listed_subject_accepted(self):
        with patch("auth.validate_token", return_value={"sub": "alice@example.com"}):
            with patch(
                "proxy.proxy_sse",
                new=AsyncMock(return_value=MagicMock(status_code=200)),
            ):
                resp = client.get(
                    "/sse", headers={"Authorization": "Bearer some-valid-looking-jwt"}
                )
        assert resp.status_code == 200


class TestDcrStoreBounds(unittest.TestCase):
    """The DCR store must not grow without bound."""

    def setUp(self):
        import main

        main.dcr_clients.clear()
        main._register_buckets.clear()

    def test_expired_registration_is_pruned(self):
        import main

        main.dcr_clients["old"] = {
            "client_id": "old",
            "client_id_issued_at": 0,
            "client_id_expires_at": 1,  # already expired
        }
        resp = client.post("/register", json={"redirect_uris": ["https://a/cb"]})
        assert resp.status_code == 201
        assert "old" not in main.dcr_clients

    def test_store_stays_capped(self):
        import main

        # Each registration comes from a distinct client IP so the per-IP
        # /register rate limit does not trip during this store-cap check.
        for i in range(main.MAX_DCR_CLIENTS + 25):
            resp = client.post(
                "/register",
                json={"redirect_uris": ["https://a/cb"]},
                headers={"X-Forwarded-For": f"10.0.{i // 256}.{i % 256}"},
            )
            assert resp.status_code == 201
        assert len(main.dcr_clients) <= main.MAX_DCR_CLIENTS
        main.dcr_clients.clear()


class TestRegisterRateLimit(unittest.TestCase):
    """The unauthenticated /register endpoint is rate-limited per client IP."""

    def setUp(self):
        import main

        main.dcr_clients.clear()
        main._register_buckets.clear()

    def test_burst_from_one_ip_is_eventually_throttled(self):
        import main

        headers = {"X-Forwarded-For": "203.0.113.7"}
        body = {"redirect_uris": ["https://a/cb"]}
        # The burst capacity worth of requests succeed...
        for _ in range(int(main.REGISTER_RATE_CAPACITY)):
            assert client.post("/register", json=body, headers=headers).status_code == 201
        # ...the next one, before the bucket refills, is throttled.
        assert client.post("/register", json=body, headers=headers).status_code == 429

    def test_separate_ips_have_independent_budgets(self):
        body = {"redirect_uris": ["https://a/cb"]}
        # Exhaust one IP's budget.
        for _ in range(50):
            client.post("/register", json=body, headers={"X-Forwarded-For": "198.51.100.1"})
        # A different IP is unaffected.
        resp = client.post(
            "/register", json=body, headers={"X-Forwarded-For": "198.51.100.2"}
        )
        assert resp.status_code == 201

    def test_rightmost_forwarded_for_entry_is_used(self):
        # A spoofed left-most entry must not create a fresh budget: keying on the
        # right-most (proxy-appended) entry means both requests share a bucket.
        body = {"redirect_uris": ["https://a/cb"]}
        for _ in range(25):  # exhaust the burst budget (capacity 20)
            client.post(
                "/register",
                json=body,
                headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"},
            )
        # Same real client (right-most 203.0.113.9), different spoofed left-most.
        resp = client.post(
            "/register",
            json=body,
            headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.9"},
        )
        assert resp.status_code == 429

    def test_trusted_proxy_hops_selects_entry_from_the_right(self):
        # With two trusted hops the real client is the 2nd-from-right entry, so a
        # rotating right-most (e.g. a load-balancer IP) does not create fresh
        # budgets for the same client.
        import main

        body = {"redirect_uris": ["https://a/cb"]}
        with patch.object(config, "TRUSTED_PROXY_HOPS", 2):
            for _ in range(25):  # exhaust the budget for client 100.64.0.5
                client.post(
                    "/register",
                    json=body,
                    headers={"X-Forwarded-For": "1.1.1.1, 100.64.0.5, 10.0.0.1"},
                )
            # Same real client (2nd-from-right 100.64.0.5), different LB IP right-most.
            resp = client.post(
                "/register",
                json=body,
                headers={"X-Forwarded-For": "1.1.1.1, 100.64.0.5, 10.0.0.2"},
            )
        assert resp.status_code == 429


class TestRegisterBucketBounds(unittest.TestCase):
    """The rate-limiter's own state must not grow without bound."""

    def setUp(self):
        import main

        main._register_buckets.clear()

    def test_prune_hard_caps_the_map_under_a_fresh_flood(self):
        import main
        import time as _time

        now = _time.monotonic()
        # Simulate an active many-IP flood: every bucket is fresh (not idle), so
        # idle eviction frees nothing and the hard cap must kick in.
        for i in range(main._MAX_REGISTER_BUCKETS + 500):
            main._register_buckets[f"ip-{i}"] = (0.0, now)
        main._prune_register_buckets(now)
        assert len(main._register_buckets) < main._MAX_REGISTER_BUCKETS
        main._register_buckets.clear()

    def test_prune_drops_idle_buckets_first(self):
        import main
        import time as _time

        now = _time.monotonic()
        # Fill to the cap with idle entries plus a few fresh ones.
        for i in range(main._MAX_REGISTER_BUCKETS):
            main._register_buckets[f"idle-{i}"] = (5.0, now - main._REGISTER_BUCKET_IDLE_TTL - 10)
        main._register_buckets["fresh"] = (5.0, now)
        main._prune_register_buckets(now)
        # The fresh key survives; idle keys are reclaimed.
        assert "fresh" in main._register_buckets
        assert len(main._register_buckets) < main._MAX_REGISTER_BUCKETS
        main._register_buckets.clear()


if __name__ == "__main__":
    unittest.main()
