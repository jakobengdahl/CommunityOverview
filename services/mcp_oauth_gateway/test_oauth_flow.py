"""
Tests for the MCP OAuth Gateway – focus on redirect_uri handling.

Uses unittest.mock to isolate from Google OIDC and config env vars.
"""

import hashlib
import base64
import importlib
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

# Set required env vars before importing config (it reads them at import time)
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
os.environ.setdefault("GW_JWT_SIGNING_KEY", "test-jwt-key-at-least-32-chars!!")
os.environ.setdefault("TEST_USERS", "alice@example.com,bob@example.com")
os.environ.setdefault("UPSTREAM_MCP_BASE_URL", "http://localhost:9000")
os.environ.setdefault("PUBLIC_BASE_URL", "https://gateway.example.com")

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


if __name__ == "__main__":
    unittest.main()
