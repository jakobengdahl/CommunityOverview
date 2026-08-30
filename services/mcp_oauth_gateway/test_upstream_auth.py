"""Tests for the service identity the gateway presents to the upstream.

The upstream's Cloud Run service is meant to require roles/run.invoker instead
of allowing allUsers. That only holds if every proxied call carries an ID token
for the gateway's own service account — a single route that forgets it is an
outage on that route, and a flag that defaults on is an outage at deploy time
for anyone who has not yet added the binding.
"""

import asyncio
import importlib
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
os.environ.setdefault("GW_JWT_SIGNING_KEY", "test-jwt-key-at-least-32-chars!!")
os.environ.setdefault("TEST_USERS", "alice@example.com")
os.environ.setdefault("UPSTREAM_MCP_BASE_URL", "http://localhost:9000")
os.environ.setdefault("PUBLIC_BASE_URL", "https://gateway.example.com")

import config
import proxy
import upstream_auth


def _request(headers):
    req = MagicMock()
    req.headers = headers
    return req


class UpstreamIdTokenTests(unittest.TestCase):
    def setUp(self):
        upstream_auth.reset_cache()

    def tearDown(self):
        upstream_auth.reset_cache()
        config.UPSTREAM_USE_ID_TOKEN = False

    def test_disabled_by_default(self):
        """A build shipped before the invoker binding exists must not change."""
        importlib.reload(config)
        self.assertFalse(config.UPSTREAM_USE_ID_TOKEN)

    def test_no_header_when_disabled(self):
        config.UPSTREAM_USE_ID_TOKEN = False
        self.assertEqual(asyncio.run(upstream_auth.id_token_header()), {})

    def test_mints_token_with_upstream_as_audience(self):
        config.UPSTREAM_USE_ID_TOKEN = True
        with patch.object(
            upstream_auth, "_fetch_id_token", AsyncMock(return_value="tok-1")
        ) as fetch:
            header = asyncio.run(upstream_auth.id_token_header())
        self.assertEqual(header, {"authorization": "Bearer tok-1"})
        fetch.assert_awaited_once_with(config.UPSTREAM_MCP_BASE_URL)

    def test_token_is_cached_between_calls(self):
        config.UPSTREAM_USE_ID_TOKEN = True
        with patch.object(
            upstream_auth, "_fetch_id_token", AsyncMock(return_value="tok-1")
        ) as fetch:
            asyncio.run(upstream_auth.id_token_header())
            asyncio.run(upstream_auth.id_token_header())
        self.assertEqual(fetch.await_count, 1)

    def test_concurrent_callers_mint_once(self):
        """Requests queued on the refresh lock must not each mint a token."""
        config.UPSTREAM_USE_ID_TOKEN = True

        async def slow(_audience):
            await asyncio.sleep(0.01)
            return "tok-1"

        with patch.object(upstream_auth, "_fetch_id_token", AsyncMock(side_effect=slow)) as fetch:

            async def race():
                return await asyncio.gather(
                    *(upstream_auth.id_token_header() for _ in range(8))
                )

            results = asyncio.run(race())
        self.assertEqual(fetch.await_count, 1)
        self.assertTrue(all(r == {"authorization": "Bearer tok-1"} for r in results))

    def test_metadata_failure_does_not_raise(self):
        """A failed mint must degrade to a 403 from Cloud Run, not a 500 here."""
        config.UPSTREAM_USE_ID_TOKEN = True
        with patch.object(
            upstream_auth, "_fetch_id_token", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            self.assertEqual(asyncio.run(upstream_auth.id_token_header()), {})


class UpstreamHeaderTests(unittest.TestCase):
    def setUp(self):
        upstream_auth.reset_cache()

    def tearDown(self):
        upstream_auth.reset_cache()
        config.UPSTREAM_USE_ID_TOKEN = False

    def test_client_bearer_never_reaches_the_upstream(self):
        """`authorization` is hop-by-hop here; the gateway JWT must not leak."""
        config.UPSTREAM_USE_ID_TOKEN = True
        req = _request({"Authorization": "Bearer client-token", "accept": "text/event-stream"})
        with patch.object(upstream_auth, "_fetch_id_token", AsyncMock(return_value="tok-1")):
            headers = asyncio.run(proxy._upstream_headers(req))
        auth_values = [v for k, v in headers.items() if k.lower() == "authorization"]
        self.assertEqual(auth_values, ["Bearer tok-1"])
        self.assertEqual(headers["accept"], "text/event-stream")

    def test_no_authorization_at_all_when_disabled(self):
        """Today's behaviour: the upstream is called with no bearer whatsoever."""
        config.UPSTREAM_USE_ID_TOKEN = False
        req = _request({"Authorization": "Bearer client-token", "accept": "application/json"})
        headers = asyncio.run(proxy._upstream_headers(req))
        self.assertEqual([k for k in headers if k.lower() == "authorization"], [])
        self.assertEqual(headers["accept"], "application/json")

    def test_every_proxy_path_uses_the_identity_helper(self):
        """A route that called _forward_headers directly would be anonymous."""
        source = open(os.path.join(os.path.dirname(proxy.__file__), "proxy.py")).read()
        body = source[source.index("async def proxy_sse") : source.index("def _forward_headers")]
        self.assertNotIn("_forward_headers(request)", body)
        self.assertEqual(body.count("await _upstream_headers(request)"), 4)


if __name__ == "__main__":
    unittest.main()
