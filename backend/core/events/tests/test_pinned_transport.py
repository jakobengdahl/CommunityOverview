"""DNS rebinding: the address checked must be the address connected to.

is_safe_url resolves a name and checks every answer, but a name can answer
differently when the request later connects. These tests run real sockets
against a loopback server, with a resolver that answers a public address to
the check and loopback to the connect -- the shape of a rebinding attack. A
walker that connected by name would reach the server; the pinned transport
must refuse before anything is sent.
"""

import asyncio
import contextlib
import ipaddress
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore2
import httpx2 as httpx
import pytest

from backend.agents.config import MCPIntegration, MCPTransport
from backend.agents.mcp_loader import MCPLoader
from backend.core import image_ingest
from backend.core.events import delivery
from backend.skills.loader import SkillsConfig, SkillsLoader

PUBLIC = "93.184.216.34"
_real_getaddrinfo = socket.getaddrinfo


@pytest.fixture
def server(monkeypatch):
    """A loopback HTTP server that records every request it receives.

    Proxy variables are cleared so every client here connects directly: a
    by-name client would otherwise send its request to a proxy, and a test
    asserting "nothing reached the server" could pass without the pin.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    received = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(("GET", self.path, self.headers.get("Host")))
            status, headers = routes.get(self.path, (200, {}))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(("POST", self.path, self.headers.get("Host")))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], received, routes
    finally:
        httpd.shutdown()
        httpd.server_close()


@contextlib.contextmanager
def _resolver(monkeypatch, answers):
    """Patch name resolution process-wide (is_safe_url, the pinned backend and
    httpx's own by-name connect all go through socket.getaddrinfo).

    `answers` maps a hostname to a list of answer lists; the Nth lookup of that
    name gets the Nth list (the last one repeats), and an exception in that
    place is raised instead. Names not listed resolve for real, so IP literals
    resolve as themselves.
    """
    lookups = {}

    def fake(host, port, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode()
        if host not in answers:
            return _real_getaddrinfo(host, port, *args, **kwargs)
        n = lookups.get(host, 0)
        lookups[host] = n + 1
        ips = answers[host][min(n, len(answers[host]) - 1)]
        if isinstance(ips, Exception):
            raise ips
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    yield


def _rebinding(monkeypatch, host="rebind.test"):
    """Public to the first lookup (the is_safe_url check), loopback after."""
    return _resolver(monkeypatch, {host: [[PUBLIC], ["127.0.0.1"]]})


@pytest.fixture
def loopback_is_public(monkeypatch):
    """Let the pin accept 127.0.0.0/8, so a positive path can be driven against
    the loopback server. Everything else is judged as in production."""
    real = delivery._is_safe_ip
    monkeypatch.setattr(
        delivery,
        "_is_safe_ip",
        lambda ip: ip in ipaddress.ip_network("127.0.0.0/8") or real(ip),
    )


# ---------------------------------------------------------------------------
# The transport itself
# ---------------------------------------------------------------------------


class TestPinnedTransport:
    def test_connects_to_the_checked_address_with_the_original_host_header(
        self, monkeypatch, server, loopback_is_public
    ):
        port, received, _ = server
        with _resolver(monkeypatch, {"pinned.test": [["127.0.0.1"]]}):
            with httpx.Client(transport=delivery.pinned_transport()) as client:
                response = client.get(f"http://pinned.test:{port}/x")

        assert response.status_code == 200
        assert received == [("GET", "/x", f"pinned.test:{port}")]

    @pytest.mark.asyncio
    async def test_async_connects_to_the_checked_address_with_the_original_host(
        self, monkeypatch, server, loopback_is_public
    ):
        port, received, _ = server
        with _resolver(monkeypatch, {"pinned.test": [["127.0.0.1"]]}):
            async with httpx.AsyncClient(
                transport=delivery.async_pinned_transport()
            ) as client:
                response = await client.get(f"http://pinned.test:{port}/x")

        assert response.status_code == 200
        assert received == [("GET", "/x", f"pinned.test:{port}")]

    def test_falls_through_to_the_next_checked_address(
        self, monkeypatch, server, loopback_is_public
    ):
        """Nothing listens on 127.0.0.2, so the first answer refuses the
        connection and the second (also checked) one is used."""
        port, received, _ = server
        with _resolver(monkeypatch, {"pinned.test": [["127.0.0.2", "127.0.0.1"]]}):
            with httpx.Client(transport=delivery.pinned_transport()) as client:
                response = client.get(f"http://pinned.test:{port}/x")

        assert response.status_code == 200
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_async_falls_through_to_the_next_checked_address(
        self, monkeypatch, server, loopback_is_public
    ):
        port, received, _ = server
        with _resolver(monkeypatch, {"pinned.test": [["127.0.0.2", "127.0.0.1"]]}):
            async with httpx.AsyncClient(
                transport=delivery.async_pinned_transport()
            ) as client:
                response = await client.get(f"http://pinned.test:{port}/x")

        assert response.status_code == 200
        assert len(received) == 1

    def test_a_failed_lookup_at_connect_is_refused_not_retried_by_name(
        self, monkeypatch, server
    ):
        """If the pin's own lookup fails, nothing may fall back to connecting
        by name, which would resolve again -- here, to loopback."""
        port, received, _ = server
        answers = {
            "flaky.test": [[PUBLIC], socket.gaierror("no answer"), ["127.0.0.1"]]
        }
        with _resolver(monkeypatch, answers):
            assert delivery.is_safe_url(f"http://flaky.test:{port}/x")
            with httpx.Client(transport=delivery.pinned_transport()) as client:
                with pytest.raises(httpx.ConnectError) as excinfo:
                    client.get(f"http://flaky.test:{port}/x")
        assert isinstance(excinfo.value.__cause__, delivery.UnsafeAddressError)
        assert received == []

    @pytest.mark.asyncio
    async def test_async_connect_timeout_also_bounds_the_lookup(self, monkeypatch):
        """The wrapped async backend resolves under the connect timeout; the
        pin keeps that, so a stalled lookup cannot outlast the timeout."""
        real = socket.getaddrinfo
        release = threading.Event()

        def stalled(host, *args, **kwargs):
            if host == "slow.test":
                release.wait(10)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]
            return real(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", stalled)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                transport=delivery.async_pinned_transport(), timeout=0.2
            ) as client:
                with pytest.raises(httpx.ConnectTimeout):
                    await client.get("http://slow.test/x")
            assert time.monotonic() - started < 5
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_async_a_timed_out_address_still_falls_through(
        self, monkeypatch, server, loopback_is_public
    ):
        """An address that times out gets its own timeout, not the whole
        connect: the next checked address is still tried."""
        port, received, _ = server
        real_connect = httpcore2.AnyIOBackend.connect_tcp

        async def blackholed_first(self, host, port, timeout=None, **kwargs):
            if host == "127.0.0.9":
                await asyncio.sleep(timeout)
                raise httpcore2.ConnectTimeout("blackholed")
            return await real_connect(self, host, port, timeout=timeout, **kwargs)

        monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", blackholed_first)
        with _resolver(monkeypatch, {"pinned.test": [["127.0.0.9", "127.0.0.1"]]}):
            async with httpx.AsyncClient(
                transport=delivery.async_pinned_transport(), timeout=0.3
            ) as client:
                response = await client.get(f"http://pinned.test:{port}/x")

        assert response.status_code == 200
        assert len(received) == 1

    @pytest.mark.parametrize(
        "pinned, plain",
        [
            (delivery.pinned_transport, httpx.HTTPTransport),
            (delivery.async_pinned_transport, httpx.AsyncHTTPTransport),
        ],
    )
    def test_pool_matches_what_httpx_would_build(self, pinned, plain):
        """The pinned transport builds its own pool, so its TLS and limits are
        compared with the pool httpx's own default transport builds."""
        ours, theirs = pinned()._pool, plain()._pool
        for attr in (
            "_max_connections",
            "_max_keepalive_connections",
            "_keepalive_expiry",
            "_http1",
            "_http2",
            "_retries",
        ):
            assert getattr(ours, attr) == getattr(theirs, attr), attr
        assert ours._ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ours._ssl_context.check_hostname is True
        assert type(ours._ssl_context) is type(theirs._ssl_context)

    @pytest.mark.parametrize("pinned", [True, False])
    def test_a_rebound_name_is_refused_before_anything_is_sent(
        self, monkeypatch, server, pinned
    ):
        """The control case (pinned=False, a plain transport) shows the
        resolver really does steer a by-name connect to the server."""
        port, received, _ = server
        transport = delivery.pinned_transport() if pinned else httpx.HTTPTransport()
        with _rebinding(monkeypatch):
            assert delivery.is_safe_url(f"http://rebind.test:{port}/x")
            with httpx.Client(transport=transport) as client:
                if pinned:
                    with pytest.raises(httpx.ConnectError) as excinfo:
                        client.get(f"http://rebind.test:{port}/x")
                    assert isinstance(
                        excinfo.value.__cause__, delivery.UnsafeAddressError
                    )
                else:
                    client.get(f"http://rebind.test:{port}/x")

        assert received == ([] if pinned else [("GET", "/x", f"rebind.test:{port}")])

    def test_one_internal_answer_among_public_ones_is_refused(
        self, monkeypatch, server
    ):
        port, received, _ = server
        with _resolver(monkeypatch, {"mixed.test": [[PUBLIC, "127.0.0.1"]]}):
            with httpx.Client(transport=delivery.pinned_transport()) as client:
                with pytest.raises(httpx.ConnectError):
                    client.get(f"http://mixed.test:{port}/x")
        assert received == []

    def test_an_internal_ip_literal_is_refused(self, server):
        port, received, _ = server
        with httpx.Client(transport=delivery.pinned_transport()) as client:
            with pytest.raises(httpx.ConnectError):
                client.get(f"http://127.0.0.1:{port}/x")
        assert received == []

    def test_tls_server_name_is_the_original_host(self, monkeypatch):
        """The socket goes to the checked address literal while TLS is still
        negotiated for the name in the URL, so certificates verify as before."""
        seen = {}

        class _Stream(httpcore2.NetworkStream):
            def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                seen["server_hostname"] = server_hostname
                raise httpcore2.ConnectError("stop after recording")

            def close(self):
                pass

        class _Backend(httpcore2.NetworkBackend):
            def connect_tcp(self, host, port, **kwargs):
                seen["connect"] = (host, port)
                return _Stream()

        monkeypatch.setattr(httpcore2, "SyncBackend", _Backend)
        with _resolver(monkeypatch, {"pinned.test": [[PUBLIC]]}):
            with httpx.Client(transport=delivery.pinned_transport()) as client:
                with pytest.raises(httpx.ConnectError, match="stop after recording"):
                    client.get("https://pinned.test/x")

        assert seen == {"connect": (PUBLIC, 443), "server_hostname": "pinned.test"}


# ---------------------------------------------------------------------------
# Every hop-validating walker, against a name that rebinds after its check
# ---------------------------------------------------------------------------


class TestWalkersRefuseARebindingName:
    def test_webhook_delivery(self, monkeypatch, server):
        port, received, _ = server
        url = f"http://rebind.test:{port}/hook"
        with _rebinding(monkeypatch):
            # _deliver's own check, then the walk it hands the URL to.
            assert delivery.is_safe_url(url)
            with pytest.raises(httpx.ConnectError):
                delivery.DeliveryWorker()._post_with_redirect_ssrf_check(
                    url, {"k": "v"}, {"Content-Type": "application/json"}
                )
        assert received == []

    def test_image_ingest(self, monkeypatch, server):
        port, received, _ = server
        with _rebinding(monkeypatch):
            with pytest.raises(image_ingest.ImageFetchError):
                image_ingest.fetch_image_bytes(f"http://rebind.test:{port}/a.png")
        assert received == []

    def test_agent_fetch_tool(self, monkeypatch, server):
        port, received, _ = server
        with _rebinding(monkeypatch):
            result = MCPLoader([])._execute_fetch_tool(
                "fetch", {"url": f"http://rebind.test:{port}/page"}
            )
        assert "error" in result
        assert received == []

    def test_mcp_info_endpoint_redirect_hop(self, monkeypatch, server):
        """The configured address is the operator's and is reached by name;
        the hop it redirects to passes is_safe_url and then rebinds."""
        port, received, routes = server
        routes["/info"] = (302, {"Location": f"http://rebind.test:{port}/info"})
        integration = MCPIntegration(
            id="WEB",
            name="Some HTTP MCP",
            transport=MCPTransport.HTTP,
            url=f"http://127.0.0.1:{port}/mcp",
        )
        with _rebinding(monkeypatch):
            MCPLoader([integration])._connect_http(integration)
        assert received == [("GET", "/info", f"127.0.0.1:{port}")]

    @pytest.mark.asyncio
    async def test_skills_loader(self, monkeypatch, server):
        port, received, _ = server
        loader = SkillsLoader(
            SkillsConfig(allow_external_skills=True, trusted_domains=["rebind.test"])
        )
        with _rebinding(monkeypatch):
            with pytest.raises(httpx.ConnectError):
                await loader._fetch_text(f"http://rebind.test:{port}/SKILL.md")
        assert received == []
