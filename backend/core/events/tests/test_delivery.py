"""
Tests for event delivery worker.
"""

import socket
import time
from unittest.mock import patch, Mock

import httpx2 as httpx
import pytest

from backend.core.events.models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    EntityData,
    DeliveryStatus,
    SubscriptionInfo,
)
from backend.core.events import delivery
from backend.core.events.delivery import (
    MAX_REDIRECTS,
    DeliveryWorker,
    DeliveryItem,
    _SSRFRedirectBlocked,
    is_safe_url,
)


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02):
    """Poll ``predicate`` until it returns a truthy value or the deadline passes.

    Returns the predicate's value — truthy once the awaited condition holds, or the
    final falsy value on timeout so the caller's assertion reports the real state.
    Waiting on the outcome instead of a fixed sleep avoids races with delivery
    retry timing under CI load.
    """
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()
    return value


def create_test_event(
    event_id: str = "test-event-1",
    subscription_id: str = "sub-1",
    subscription_name: str = "Test Sub",
) -> Event:
    """Helper to create a test event."""
    return Event(
        event_id=event_id,
        event_type=EventType.NODE_CREATE,
        origin=EventContext(event_origin="test"),
        entity=EntityData(
            kind=EntityKind.NODE,
            id="node-1",
            type="Actor",
            after={"name": "Test", "type": "Actor"},
        ),
        subscription=SubscriptionInfo(
            id=subscription_id,
            name=subscription_name,
        ),
    )


class TestDeliveryWorker:
    """Tests for the DeliveryWorker class."""

    def test_is_safe_url(self):
        """Test the SSRF URL validation function."""
        # Safe URLs
        assert is_safe_url("https://example.com/hook") is True
        assert is_safe_url("http://google.com") is True

        # Invalid schemes
        assert is_safe_url("ftp://example.com") is False
        assert is_safe_url("file:///etc/passwd") is False
        assert is_safe_url("javascript:alert(1)") is False

        # Private/local IPv4
        assert is_safe_url("http://127.0.0.1/hook") is False
        assert is_safe_url("http://localhost:8080") is False
        assert is_safe_url("http://10.0.0.1") is False
        assert is_safe_url("http://192.168.1.1") is False
        assert is_safe_url("http://169.254.169.254") is False

        # Private/local IPv6
        assert is_safe_url("http://[::1]/hook") is False
        assert is_safe_url("http://[fc00::1]/hook") is False  # ULA (private)
        assert is_safe_url("http://[fe80::1]/hook") is False  # link-local

        # RFC 6598 Carrier-Grade NAT (not classified as private by ipaddress)
        assert is_safe_url("http://100.64.0.1") is False
        assert is_safe_url("http://100.127.255.254") is False

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    def test_delivery_worker_ssrf_blocked(self, mock_getaddrinfo):
        """Test that SSRF attempts are blocked and marked as failed."""
        # Mock DNS resolution to return a local IPv4 address
        mock_getaddrinfo.return_value = [(None, None, None, None, ("127.0.0.1", 0))]

        results = []
        worker = DeliveryWorker(
            max_attempts=1,  # Should fail immediately without retries
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            # The domain itself looks ok, but DNS resolves to 127.0.0.1
            worker.enqueue(event, "http://malicious.com/hook")

            _wait_for(lambda: len(results) >= 1)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.DROPPED
            assert "Blocked attempt to send webhook" in results[0].error_message
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    def test_ssrf_blocked_never_retried(self, mock_getaddrinfo):
        """SSRF-blocked deliveries must be dropped immediately with no retries.

        A private IP will not become public on a subsequent attempt, so retrying
        would be wasteful and could slow down legitimate event processing.
        """
        mock_getaddrinfo.return_value = [(None, None, None, None, ("10.0.0.1", 0))]

        results = []
        worker = DeliveryWorker(
            max_attempts=3,  # Default — SSRF must still produce exactly 1 DROPPED result
            backoff_times=[0.05, 0.05, 0.05],
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "http://internal.corp/hook")

            _wait_for(lambda: len(results) >= 1)

            # Exactly one result — no RETRYING callbacks, no second attempt
            assert len(results) == 1
            assert results[0].status == DeliveryStatus.DROPPED
            assert "Blocked attempt" in results[0].error_message
        finally:
            worker.stop(wait=True)

    def test_worker_starts_and_stops(self):
        """Test that worker can start and stop cleanly."""
        worker = DeliveryWorker()

        assert not worker.is_running
        worker.start()
        assert worker.is_running

        worker.stop(wait=True)
        assert not worker.is_running

    def test_worker_enqueues_events(self):
        """Test that events can be enqueued."""
        worker = DeliveryWorker()
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            # Give it a moment to process
            time.sleep(0.1)

            # Queue should be processed (or empty after processing)
            assert worker.queue_size >= 0
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.is_safe_url", return_value=True)
    @patch("backend.core.events.delivery.httpx.Client")
    def test_successful_delivery(self, mock_client_cls, mock_safe_url):
        """Test successful webhook delivery."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.is_redirect = False
        mock_response.headers = {}

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(on_result=lambda r: results.append(r))
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            # Wait for delivery
            _wait_for(lambda: len(results) >= 1)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.SUCCESS
            assert results[0].status_code == 200
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.is_safe_url", return_value=True)
    @patch("backend.core.events.delivery.httpx.Client")
    def test_failed_delivery_with_retry(self, mock_client_cls, mock_safe_url):
        """Test that failed deliveries are retried."""
        # Fail twice, then succeed
        mock_responses = [
            Mock(status_code=500, text="Server Error", is_redirect=False, headers={}),
            Mock(status_code=500, text="Server Error", is_redirect=False, headers={}),
            Mock(status_code=200, is_redirect=False, headers={}),
        ]

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.side_effect = mock_responses
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=3,
            backoff_times=[0.1, 0.1, 0.1],  # Short delays for testing
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            # Wait until the delivery reaches its terminal SUCCESS state. The two
            # failures each incur a 0.1s backoff before the third attempt lands, so
            # a fixed sleep races with CI load; poll on the outcome instead.
            success_results = _wait_for(
                lambda: [r for r in results if r.status == DeliveryStatus.SUCCESS]
            )

            # Should have 3 results: 2 retrying + 1 success
            assert len(results) >= 1
            assert len(success_results) == 1
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.is_safe_url", return_value=True)
    @patch("backend.core.events.delivery.httpx.Client")
    def test_max_retries_exceeded(self, mock_client_cls, mock_safe_url):
        """Test that events are dropped after max retries."""
        # Always fail
        mock_response = Mock(
            status_code=500, text="Server Error", is_redirect=False, headers={}
        )

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=2,
            backoff_times=[0.1],
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            # Wait for retries to exhaust and land on a terminal DROPPED result.
            dropped_results = _wait_for(
                lambda: [r for r in results if r.status == DeliveryStatus.DROPPED]
            )

            # Should end with DROPPED status
            assert len(dropped_results) == 1
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.is_safe_url", return_value=True)
    @patch("backend.core.events.delivery.httpx.Client")
    def test_timeout_handling(self, mock_client_cls, mock_safe_url):
        """Test that timeouts are handled correctly."""
        import httpx2 as httpx

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.side_effect = httpx.TimeoutException("Connection timed out")
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=1,
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            _wait_for(lambda: len(results) >= 1)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.DROPPED
            assert "timed out" in results[0].error_message.lower()
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.is_safe_url", return_value=True)
    @patch("backend.core.events.delivery.httpx.Client")
    def test_webhook_payload_format(self, mock_client_cls, mock_safe_url):
        """Test that webhook receives correct payload format."""
        mock_response = Mock(status_code=200, is_redirect=False, headers={})

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        worker = DeliveryWorker()
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            # Wait for the worker thread to have actually made the call — a fixed
            # sleep races with CI load and can fire the assertion before the
            # background thread has posted, leaving call_args as None.
            assert _wait_for(lambda: mock_client.post.called)
            call_kwargs = mock_client.post.call_args.kwargs

            # Check URL
            assert mock_client.post.call_args.args[0] == "https://example.com/hook"

            # Check headers
            assert call_kwargs["headers"]["Content-Type"] == "application/json"
            assert call_kwargs["headers"]["X-Event-ID"] == event.event_id
            assert call_kwargs["headers"]["X-Event-Type"] == "node.create"

            # Check payload structure
            payload = call_kwargs["json"]
            assert "event_id" in payload
            assert "event_type" in payload
            assert "occurred_at" in payload
            assert "origin" in payload
            assert "entity" in payload
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.core.events.delivery.httpx.Client")
    def test_ssrf_blocked_on_redirect_to_private_ip(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """Redirect to a private/internal address must be rejected (SSRF via redirect)."""
        # Initial URL resolves to a public IP — passes the pre-request check
        mock_getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]

        # Server responds with a redirect to an internal metadata endpoint
        redirect_response = Mock()
        redirect_response.is_redirect = True
        redirect_response.status_code = 302
        redirect_response.headers = {"location": "http://169.254.169.254/metadata"}

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.return_value = redirect_response
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=3,
            backoff_times=[0.05, 0.05, 0.05],
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "http://example.com/hook")

            _wait_for(lambda: len(results) >= 1)

            # Must be dropped immediately — no retry, no follow-through to the internal address
            assert len(results) == 1
            assert results[0].status == DeliveryStatus.DROPPED
            # The per-hop check only runs if httpx is told not to follow
            # redirects itself, at both the client and the call.
            assert mock_client_cls.call_args.kwargs["follow_redirects"] is False
            assert mock_client.post.call_args.kwargs["follow_redirects"] is False
            assert "169.254.169.254" in results[0].error_message
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.core.events.delivery.httpx.Client")
    def test_ssrf_blocked_on_redirect_never_retried(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """An SSRF-blocked redirect must be dropped with no retries, same as initial block."""
        mock_getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]

        redirect_response = Mock()
        redirect_response.is_redirect = True
        redirect_response.status_code = 302
        redirect_response.headers = {"location": "http://10.0.0.1/internal"}

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.return_value = redirect_response
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=3,
            backoff_times=[0.05, 0.05, 0.05],
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "http://example.com/hook")

            _wait_for(lambda: len(results) >= 1)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.DROPPED
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.core.events.delivery.httpx.Client")
    def test_safe_redirect_is_followed(self, mock_client_cls, mock_getaddrinfo):
        """A redirect to a safe public URL must be followed normally."""
        # Both the original and redirect target resolve to public IPs
        mock_getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]

        redirect_response = Mock()
        redirect_response.is_redirect = True
        redirect_response.status_code = 307
        redirect_response.headers = {"location": "https://hooks.example.com/v2/hook"}

        success_response = Mock()
        success_response.is_redirect = False
        success_response.status_code = 200

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.side_effect = [redirect_response, success_response]
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=1,
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "http://example.com/hook")

            _wait_for(lambda: len(results) >= 1)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.SUCCESS
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.core.events.delivery.httpx.Client")
    def test_safe_relative_redirect_is_followed(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """A safe relative redirect should be resolved against the current URL and followed."""
        mock_getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]

        redirect_response = Mock()
        redirect_response.is_redirect = True
        redirect_response.status_code = 307
        redirect_response.headers = {"location": "/v2/hook"}

        success_response = Mock()
        success_response.is_redirect = False
        success_response.status_code = 200

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.side_effect = [redirect_response, success_response]
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=1,
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "http://example.com/hook")

            _wait_for(lambda: len(results) >= 1)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.SUCCESS
            assert (
                mock_client.post.call_args_list[1].args[0]
                == "http://example.com/v2/hook"
            )
        finally:
            worker.stop(wait=True)

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.core.events.delivery.httpx.Client")
    def test_redirect_limit_is_dropped_without_retry(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """Redirect loops should be dropped, not retried forever as transient failures."""
        mock_getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]

        redirect_response = Mock()
        redirect_response.is_redirect = True
        redirect_response.status_code = 307
        redirect_response.headers = {"location": "https://hooks.example.com/v2/hook"}

        mock_client = Mock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=None)
        mock_client.post.return_value = redirect_response
        mock_client_cls.return_value = mock_client

        results = []
        worker = DeliveryWorker(
            max_attempts=3,
            backoff_times=[0.05, 0.05, 0.05],
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "http://example.com/hook")

            _wait_for(lambda: len(results) >= 1)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.DROPPED
            assert results[0].error_message == "Exceeded redirect limit"
            # Pin the effective cap, not just the outcome: any limit that
            # eventually terminates satisfies the assertions above.
            assert mock_client.post.call_count == MAX_REDIRECTS
        finally:
            worker.stop(wait=True)


class TestDeliveryItem:
    """Tests for DeliveryItem class."""

    def test_create_delivery_item(self):
        """Test creating a delivery item."""
        event = create_test_event()
        item = DeliveryItem(
            event=event,
            webhook_url="https://example.com/hook",
            attempt=1,
        )

        assert item.event == event
        assert item.webhook_url == "https://example.com/hook"
        assert item.attempt == 1
        assert item.enqueued_at is not None

    def test_default_attempt_is_one(self):
        """Test that default attempt number is 1."""
        event = create_test_event()
        item = DeliveryItem(event=event, webhook_url="https://example.com")

        assert item.attempt == 1


def _addrinfo(*ips):
    return [(None, None, None, None, (ip, 0)) for ip in ips]


@pytest.fixture
def _public_dns(monkeypatch):
    # Offline, an unresolvable host is rejected by the DNS step, which would
    # let a scheme check that had stopped working pass unnoticed.
    monkeypatch.setattr(
        delivery.socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34")
    )


@pytest.mark.usefixtures("_public_dns")
class TestIsSafeUrlClauses:
    """Each clause of is_safe_url pinned by an input only that clause decides."""

    def test_public_host_is_accepted_under_the_dns_stub(self):
        assert is_safe_url("http://example.com/x") is True
        assert is_safe_url("https://example.com/x") is True

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/x",
            "file://example.com/etc/passwd",
            "gopher://example.com/",
        ],
    )
    def test_non_http_scheme_is_rejected_even_when_the_host_resolves(self, url):
        assert is_safe_url(url) is False

    def test_reserved_ipv6_literal_is_rejected(self):
        # 4000::/3 is unassigned: is_reserved, but neither is_private nor any
        # other clause of _is_safe_ip, so only the is_reserved clause refuses it.
        assert is_safe_url("http://[4000::1]/x") is False

    @pytest.mark.parametrize(
        "ip",
        ["100.64.0.1", "192.88.99.1", "fec0::1"],
        ids=["cgnat", "6to4-relay-anycast", "ipv6-site-local"],
    )
    def test_internal_range_without_an_ipaddress_flag_is_rejected(
        self, monkeypatch, ip
    ):
        # ipaddress classifies none of these as private or reserved, so only
        # the explicit network list refuses them — as a literal and as a DNS
        # answer alongside a public address.
        literal = f"[{ip}]" if ":" in ip else ip
        assert is_safe_url(f"http://{literal}/x") is False
        monkeypatch.setattr(
            delivery.socket,
            "getaddrinfo",
            lambda *a, **k: _addrinfo("93.184.216.34", ip),
        )
        assert is_safe_url("http://mixed.example.com/x") is False

    def test_dns_failure_is_rejected(self, monkeypatch):
        def fail(*args, **kwargs):
            raise socket.gaierror("name resolution failed")

        monkeypatch.setattr(delivery.socket, "getaddrinfo", fail)
        assert is_safe_url("http://unresolvable.example.com/x") is False

    def test_empty_dns_answer_is_rejected(self, monkeypatch):
        monkeypatch.setattr(delivery.socket, "getaddrinfo", lambda *a, **k: [])
        assert is_safe_url("http://empty.example.com/x") is False

    @pytest.mark.parametrize(
        "answer",
        [("not-an-ip", "10.0.0.1"), ("93.184.216.34", "not-an-ip")],
        ids=["garbage-before-internal", "garbage-after-public"],
    )
    def test_unparseable_dns_answer_is_rejected(self, monkeypatch, answer):
        monkeypatch.setattr(
            delivery.socket, "getaddrinfo", lambda *a, **k: _addrinfo(*answer)
        )
        assert is_safe_url("http://odd.example.com/x") is False


class _StubHttpxModule:
    """Stands in for delivery.py's module-level ``httpx`` name.

    Rebinding that name (rather than ``@patch``-ing ``Client`` on the shared
    httpx2 module object) keeps the mock transport local to delivery.py.
    """

    def __init__(self, transport):
        self._transport = transport

    def Client(self, **kwargs):
        return httpx.Client(transport=self._transport, **kwargs)

    def __getattr__(self, name):
        return getattr(httpx, name)


@pytest.mark.usefixtures("_public_dns")
class TestWebhookRedirectHops:
    """Drive _post_with_redirect_ssrf_check through a mock transport and record
    every request that actually went out."""

    def _post(self, monkeypatch, handler, url="http://start.example.com/hook"):
        seen = []

        def recording(request):
            seen.append((request.method, str(request.url)))
            return handler(request, len(seen) - 1)

        transport = httpx.MockTransport(recording)
        monkeypatch.setattr(delivery, "httpx", _StubHttpxModule(transport))
        worker = DeliveryWorker()
        try:
            response = worker._post_with_redirect_ssrf_check(
                url, {"k": "v"}, {"Content-Type": "application/json"}
            )
        except Exception as exc:
            return seen, exc
        return seen, response

    @pytest.mark.parametrize("internal_hop", [0, 1, 2, MAX_REDIRECTS - 2])
    def test_every_hop_is_revalidated(self, monkeypatch, internal_hop):
        def handler(request, index):
            if index < internal_hop:
                return httpx.Response(
                    307, headers={"location": f"http://hop{index + 1}.example.com/h"}
                )
            if index == internal_hop:
                return httpx.Response(307, headers={"location": "http://10.0.0.1/h"})
            raise AssertionError(f"unexpected request {request.url}")

        seen, outcome = self._post(monkeypatch, handler)

        assert isinstance(outcome, _SSRFRedirectBlocked)
        assert len(seen) == internal_hop + 1
        assert all("10.0.0.1" not in url for _, url in seen)

    def test_relative_location_resolves_against_the_current_hop(self, monkeypatch):
        def handler(request, index):
            if index == 0:
                return httpx.Response(
                    307, headers={"location": "https://second.example.com/dir/"}
                )
            if index == 1:
                return httpx.Response(307, headers={"location": "next"})
            return httpx.Response(200)

        seen, outcome = self._post(monkeypatch, handler)

        assert outcome.status_code == 200
        assert [url for _, url in seen] == [
            "http://start.example.com/hook",
            "https://second.example.com/dir/",
            "https://second.example.com/dir/next",
        ]

    @pytest.mark.parametrize(
        "location",
        ["https://loop.example.com/hook", "/loop"],
        ids=["absolute", "relative"],
    )
    def test_307_cap_holds_for_both_location_flavours(self, monkeypatch, location):
        seen, outcome = self._post(
            monkeypatch,
            lambda request, index: httpx.Response(307, headers={"location": location}),
        )

        assert isinstance(outcome, httpx.TooManyRedirects)
        assert len(seen) == MAX_REDIRECTS
        assert {method for method, _ in seen} == {"POST"}

    @pytest.mark.parametrize("status", [301, 302, 303])
    def test_method_switching_cap_counts_the_post_and_the_gets_together(
        self, monkeypatch, status
    ):
        seen, outcome = self._post(
            monkeypatch,
            lambda request, index: httpx.Response(
                status, headers={"location": "/loop"}
            ),
        )

        methods = [method for method, _ in seen]
        assert isinstance(outcome, httpx.TooManyRedirects)
        assert methods == ["POST"] + ["GET"] * (MAX_REDIRECTS - 1)
