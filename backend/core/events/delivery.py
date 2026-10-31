"""
Delivery worker for sending events to webhooks.

Responsibilities:
- Maintain an in-memory queue of events to deliver
- Process events in a background thread
- Implement retry logic with exponential backoff
- Log delivery results
"""

import logging
import threading
import queue
import time
from typing import Optional, Callable
from datetime import datetime, timezone
import asyncio
import typing
import httpcore2
import httpx2 as httpx
import urllib.parse
import socket
import ipaddress

from .models import (
    Event,
    DeliveryResult,
    DeliveryStatus,
)

logger = logging.getLogger(__name__)


# Default retry configuration
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_TIMES = [0.5, 2.0, 5.0]  # Seconds between retries
DEFAULT_TIMEOUT = 10  # HTTP request timeout in seconds
# Only a short error excerpt is used; each hop reads at most this many bytes
# off its connection, response headers included.
MAX_WEBHOOK_BODY_BYTES = 256 * 1024


# Non-global ranges that Python's ipaddress module does not classify as private
# or reserved, so the flag checks in _is_safe_ip let them through:
# - 100.64.0.0/10: RFC 6598 Carrier-Grade NAT, used for internal infrastructure
#   in many cloud and ISP environments.
# - 192.88.99.0/24: the deprecated 6to4 relay anycast prefix (RFC 7526).
# - fec0::/10: the deprecated IPv6 site-local prefix (RFC 3879), still routed
#   internally by some networks.
_UNFLAGGED_INTERNAL_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("fec0::/10"),
)


def _is_safe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP is publicly routable (not private/internal)."""
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv4-mapped address (::ffff:a.b.c.d) reaches the embedded IPv4
        # host through a dual-stack socket, and the IPv6 flags on the wrapper
        # differ between Python versions, so judge the embedded host alone.
        if ip.ipv4_mapped is not None:
            return _is_safe_ip(ip.ipv4_mapped)
        # A 6to4 address (2002::/16) is relayed to its embedded IPv4 host.
        # ipaddress currently flags all of 2002::/16 is_private, so this is
        # defense in depth: the guard does not rest on that classification.
        if ip.sixtofour is not None and not _is_safe_ip(ip.sixtofour):
            return False
    if any(ip in network for network in _UNFLAGGED_INTERNAL_NETWORKS):
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
    )


def _public_addresses(hostname: str) -> list[str]:
    """Resolve *hostname* and return its addresses if every one is public.

    getaddrinfo returns both A and AAAA records, and every one is checked, so a
    hostname with a public IPv4 and an internal IPv6 (or vice versa) is still
    refused (fail-closed). Returns an empty list when the name is refused or
    has no answers; resolution errors propagate. An IP literal is judged as
    itself, without DNS.
    """
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        return [str(literal_ip)] if _is_safe_ip(literal_ip) else []

    addresses: list[str] = []
    for addr_info in socket.getaddrinfo(hostname, None):
        # Strip the zone ID that may appear in IPv6 link-local addresses
        ip = ipaddress.ip_address(addr_info[4][0].split("%")[0])
        if not _is_safe_ip(ip):
            return []
        if str(ip) not in addresses:
            addresses.append(str(ip))
    return addresses


def is_safe_url(url: str) -> bool:
    """
    Validates a URL to prevent SSRF attacks.

    Ensures the scheme is http or https and that every IP address the hostname
    resolves to (IPv4 and IPv6) is publicly routable.  All resolved addresses
    must pass; a single internal address causes rejection (fail-closed).

    This answers for the moment it is called only: a name can resolve
    differently when the request later connects (DNS rebinding). The clients
    built with pinned_transport() / async_pinned_transport() close that gap by
    re-checking at connect time and connecting to the address they checked.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        return bool(_public_addresses(hostname))
    except (ValueError, socket.gaierror, OSError, Exception) as e:
        logger.warning(f"URL validation failed for {url}: {e}")
        return False


class UnsafeAddressError(httpcore2.ConnectError):
    """Raised at connect time when the host does not resolve to only public
    addresses. A ConnectError subclass, so httpx surfaces it as
    httpx.ConnectError and every caller's existing network-error path applies.
    """


def _pinned_targets(host: str) -> list[str]:
    try:
        addresses = _public_addresses(host)
    except (ValueError, OSError) as exc:
        raise UnsafeAddressError(f"Could not resolve {host}: {exc}") from exc
    if not addresses:
        raise UnsafeAddressError(
            f"Refused to connect to {host}: it resolves to a restricted address"
        )
    return addresses


class _ReadBudgetSpent(Exception):
    """Raised by a budgeted stream asked to read once its budget is spent."""


class _ReadBudget:
    """Body bytes a request may still read off the wire.

    reset() before each request opens the header phase, which is not capped:
    a server may send any number of 1xx responses before the final one, and
    spending the budget there would turn a delivery into a retry. arm() once
    the final headers are in starts the cap, charging it for the read that
    completed them, the only read that can also hold body bytes.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.remaining = limit
        self.armed = False
        self.last_read = 0

    def reset(self) -> None:
        self.remaining = self.limit
        self.armed = False
        self.last_read = 0

    def arm(self) -> None:
        self.remaining = max(0, self.limit - self.last_read)
        self.armed = True


class _BudgetedStream(httpcore2.NetworkStream):
    # httpcore reads up to 64 KiB per call whatever the caller still wants, so
    # the cap must shrink the socket read itself, not slice what it returned.
    def __init__(self, inner: httpcore2.NetworkStream, budget: _ReadBudget) -> None:
        self._inner = inner
        self._budget = budget

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        budget = self._budget
        if not budget.armed:
            data = self._inner.read(max_bytes, timeout)
            budget.last_read = len(data)
            return data
        if budget.remaining <= 0:
            raise _ReadBudgetSpent("Read budget spent")
        data = self._inner.read(min(max_bytes, budget.remaining), timeout)
        budget.remaining -= len(data)
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, timeout)

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        return _BudgetedStream(
            self._inner.start_tls(ssl_context, server_hostname, timeout),
            self._budget,
        )

    def get_extra_info(self, info: str) -> typing.Any:
        return self._inner.get_extra_info(info)


class _PinnedBackend(httpcore2.NetworkBackend):
    """Resolves once per connection, validates every answer, and connects to a
    validated address literal, so the address checked is the address used.

    The URL is never rewritten: httpcore still takes the Host header and the
    TLS server name from the request's own host. With a read budget, every
    stream it opens stops reading off the wire once that budget is spent.
    """

    def __init__(self, read_budget: _ReadBudget | None = None) -> None:
        self._inner = httpcore2.SyncBackend()
        self._read_budget = read_budget

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        last_error: Exception | None = None
        # Every address is already validated, so falling through to the next
        # one on a connect failure keeps the fallback a by-name connect had.
        for address in _pinned_targets(host):
            try:
                stream = self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as exc:
                last_error = exc
                continue
            if self._read_budget is None:
                return stream
            return _BudgetedStream(stream, self._read_budget)
        assert last_error is not None
        raise last_error

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class _AsyncPinnedBackend(httpcore2.AsyncNetworkBackend):
    """The async counterpart of _PinnedBackend; resolution runs in a thread.

    The lookup is held to the connect timeout. Checked addresses are then
    tried one at a time, each with the full timeout, as _PinnedBackend and
    socket.create_connection do -- not raced, as the by-name connect this
    replaces does -- so a name with N unreachable addresses can take up to
    N times the timeout before the next one answers.
    """

    def __init__(self) -> None:
        self._inner = httpcore2.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(_pinned_targets, host), timeout
            )
        except TimeoutError as exc:
            raise httpcore2.ConnectTimeout(f"Timed out resolving {host}") from exc
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PinnedTransport(httpx.HTTPTransport):
    # HTTPTransport keeps no network-backend option, so the pool it routes
    # requests through (self._pool) is built here with the same TLS and limit
    # defaults it would otherwise use. That attribute is httpx2's own; the
    # rebinding tests in tests/test_pinned_transport.py go through the real
    # transport and fail if a future httpx2 stops using it.
    def __init__(self, read_budget: _ReadBudget | None = None) -> None:
        self._pool = httpcore2.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=_PinnedBackend(read_budget),
        )


class _AsyncPinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=_AsyncPinnedBackend(),
        )


def pinned_transport(read_budget: _ReadBudget | None = None) -> httpx.HTTPTransport:
    """A transport for the hop-validating walkers below: it connects only to an
    address it has just resolved and checked, closing the window in which a
    name passes is_safe_url and then resolves elsewhere at connect time.

    An explicit transport also means the client takes no proxy from the
    environment; a proxy would resolve the name itself and undo the pin.
    """
    return _PinnedTransport(read_budget)


def async_pinned_transport() -> httpx.AsyncHTTPTransport:
    """The AsyncClient counterpart of pinned_transport()."""
    return _AsyncPinnedTransport()


# The redirect cap for the five paths that walk redirects by hand and
# re-validate every hop with is_safe_url: the webhook delivery below, the image
# ingest in core/image_ingest.py, BOTH walkers in agents/mcp_loader.py (the
# agent fetch tool, and the MCP server info-endpoint discovery in
# _connect_http) and the skills loader in skills/loader.py. Each hop is
# re-validated before it is requested, so the cap bounds that cost; they import
# this constant rather than keeping their own copy, so the five cannot drift
# apart. The skills loader additionally re-applies its own trusted_domains
# allowlist per hop, which the other four have no equivalent of. Other outbound
# requests in the backend do not use this cap: they either leave redirect
# handling to their HTTP client, or follow no redirects at all.
#
# This list is what an audit of "every hop-validating path" reads, so a new
# walker is not added without being named here.
MAX_REDIRECTS = 10


class _SSRFRedirectBlocked(Exception):
    """Raised when a redirect target fails the SSRF check."""


class _MalformedRedirect(Exception):
    """Raised when a 3xx carries no Location header to follow.

    Separate from _SSRFRedirectBlocked because the reason differs -- nothing
    was refused on address grounds -- but both share the same outcome: the
    delivery is dropped rather than retried, because neither improves on a
    second attempt.
    """


class DeliveryItem:
    """An item in the delivery queue."""

    def __init__(self, event: Event, webhook_url: str, attempt: int = 1):
        self.event = event
        self.webhook_url = webhook_url
        self.attempt = attempt
        self.enqueued_at = datetime.now(timezone.utc)


class DeliveryWorker:
    """
    Background worker that delivers events to webhooks.

    The worker maintains an in-memory queue and processes deliveries
    in a background thread with retry support.
    """

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_times: Optional[list] = None,
        timeout: int = DEFAULT_TIMEOUT,
        on_result: Optional[Callable[[DeliveryResult], None]] = None,
    ):
        """
        Initialize the delivery worker.

        Args:
            max_attempts: Maximum delivery attempts per event
            backoff_times: List of wait times between retries (seconds)
            timeout: HTTP request timeout in seconds
            on_result: Optional callback for delivery results
        """
        self._queue: queue.Queue[Optional[DeliveryItem]] = queue.Queue()
        self._max_attempts = max_attempts
        self._backoff_times = backoff_times or DEFAULT_BACKOFF_TIMES
        self._timeout = timeout
        self._on_result = on_result
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the background worker thread."""
        with self._lock:
            if self._running:
                return

            self._running = True
            self._worker_thread = threading.Thread(
                target=self._process_queue,
                name="event-delivery-worker",
                daemon=True,
            )
            self._worker_thread.start()
            logger.info("Delivery worker started")

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        """
        Stop the background worker thread.

        Args:
            wait: Whether to wait for the worker to finish
            timeout: Maximum time to wait (seconds)
        """
        with self._lock:
            if not self._running:
                return

            self._running = False
            # Send sentinel to unblock the queue
            self._queue.put(None)

        if wait and self._worker_thread:
            self._worker_thread.join(timeout=timeout)
            logger.info("Delivery worker stopped")

    def enqueue(self, event: Event, webhook_url: str) -> None:
        """
        Add an event to the delivery queue.

        Args:
            event: The event to deliver
            webhook_url: Target webhook URL
        """
        item = DeliveryItem(event=event, webhook_url=webhook_url)
        self._queue.put(item)
        logger.debug(f"Enqueued event {event.event_id} for delivery to {webhook_url}")

    def _process_queue(self) -> None:
        """Main loop for processing the delivery queue."""
        while self._running:
            try:
                # Block waiting for next item
                item = self._queue.get(timeout=1.0)

                if item is None:
                    # Sentinel value, exit loop
                    break

                self._deliver(item)

            except queue.Empty:
                # Timeout, continue loop
                continue
            except Exception as e:
                logger.error(f"Error in delivery worker: {e}")

    def _deliver(self, item: DeliveryItem) -> None:
        """
        Attempt to deliver an event.

        Args:
            item: The delivery item to process
        """
        event = item.event
        webhook_url = item.webhook_url
        attempt = item.attempt

        try:
            # Validate URL to prevent SSRF — drop immediately, never retry.
            # A private/reserved IP will not become public on a subsequent attempt.
            if not is_safe_url(webhook_url):
                error_message = (
                    "Blocked attempt to send webhook to restricted IP address"
                )
                logger.error(
                    f"SSRF blocked: event {event.event_id} to {webhook_url}. {error_message}"
                )
                if self._on_result:
                    result = DeliveryResult(
                        delivery_id=f"{event.subscription.id if event.subscription else ''}:{event.event_id}",
                        correlation_id=event.origin.event_correlation_id or "",
                        event_id=event.event_id,
                        subscription_id=event.subscription.id
                        if event.subscription
                        else "",
                        webhook_url=webhook_url,
                        status=DeliveryStatus.DROPPED,
                        attempt=attempt,
                        max_attempts=self._max_attempts,
                        error_message=error_message,
                    )
                    try:
                        self._on_result(result)
                    except Exception as e:
                        logger.error(f"Error in result callback: {e}")
                return

            # Prepare payload
            payload = event.to_webhook_payload()
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "CommunityGraph-Events/1.0",
                # Replaces httpx's default gzip/deflate offer: encoded bodies
                # are left unread, so ask receivers not to send them.
                "Accept-Encoding": "identity",
                "X-Event-ID": event.event_id,
                "X-Event-Type": event.event_type.value,
            }

            response = self._post_with_redirect_ssrf_check(
                webhook_url, payload, headers
            )

            # Check response
            if response.status_code >= 200 and response.status_code < 300:
                # Success
                result = DeliveryResult(
                    delivery_id=f"{event.subscription.id if event.subscription else ''}:{event.event_id}",
                    correlation_id=event.origin.event_correlation_id or "",
                    event_id=event.event_id,
                    subscription_id=event.subscription.id if event.subscription else "",
                    webhook_url=webhook_url,
                    status=DeliveryStatus.SUCCESS,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    status_code=response.status_code,
                    delivered_at=datetime.now(timezone.utc),
                )
                logger.info(
                    f"Successfully delivered event {event.event_id} to {webhook_url} "
                    f"(attempt {attempt}/{self._max_attempts})"
                )
            else:
                # HTTP error
                self._handle_failure(
                    item,
                    status_code=response.status_code,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}",
                )
                return

        except (_SSRFRedirectBlocked, _MalformedRedirect) as e:
            error_message = str(e)
            logger.error(
                f"Redirect refused: event {event.event_id} to {webhook_url}. "
                f"{error_message}"
            )
            if self._on_result:
                result = DeliveryResult(
                    delivery_id=f"{event.subscription.id if event.subscription else ''}:{event.event_id}",
                    correlation_id=event.origin.event_correlation_id or "",
                    event_id=event.event_id,
                    subscription_id=event.subscription.id if event.subscription else "",
                    webhook_url=webhook_url,
                    status=DeliveryStatus.DROPPED,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    error_message=error_message,
                )
                try:
                    self._on_result(result)
                except Exception as cb_exc:
                    logger.error(f"Error in result callback: {cb_exc}")
            return

        except httpx.TooManyRedirects:
            error_message = "Exceeded redirect limit"
            logger.error(
                f"Redirect loop blocked: event {event.event_id} to {webhook_url}. {error_message}"
            )
            if self._on_result:
                result = DeliveryResult(
                    delivery_id=f"{event.subscription.id if event.subscription else ''}:{event.event_id}",
                    correlation_id=event.origin.event_correlation_id or "",
                    event_id=event.event_id,
                    subscription_id=event.subscription.id if event.subscription else "",
                    webhook_url=webhook_url,
                    status=DeliveryStatus.DROPPED,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    error_message=error_message,
                )
                try:
                    self._on_result(result)
                except Exception as cb_exc:
                    logger.error(f"Error in result callback: {cb_exc}")
            return

        except httpx.TimeoutException:
            self._handle_failure(
                item,
                error_message="Request timed out",
            )
            return

        except httpx.RequestError as e:
            self._handle_failure(
                item,
                error_message=str(e),
            )
            return

        except Exception as e:
            self._handle_failure(
                item,
                error_message=f"Unexpected error: {e}",
            )
            return

        # Report success
        if self._on_result:
            try:
                self._on_result(result)
            except Exception as e:
                logger.error(f"Error in result callback: {e}")

    def _post_with_redirect_ssrf_check(
        self, url: str, payload: dict, headers: dict
    ) -> httpx.Response:
        """POST to url, following redirects while re-applying the SSRF check at each hop."""
        redirect_headers = {
            key: value
            for key, value in headers.items()
            if key.lower()
            not in ("content-type", "content-length", "transfer-encoding")
        }
        current_method = "POST"
        current_url = url
        read_budget = _ReadBudget(MAX_WEBHOOK_BODY_BYTES)

        with httpx.Client(
            timeout=self._timeout,
            follow_redirects=False,
            transport=pinned_transport(read_budget),
        ) as client:
            for _ in range(MAX_REDIRECTS):
                read_budget.reset()
                with client.stream(
                    current_method,
                    current_url,
                    headers=headers if current_method == "POST" else redirect_headers,
                    **({"json": payload} if current_method == "POST" else {}),
                    follow_redirects=False,
                ) as response:
                    read_budget.arm()
                    body = self._read_bounded_body(response)
                    if not response.is_redirect:
                        response._content = body
                        return response
                    location = str(response.headers.get("location", ""))
                    if not location:
                        raise _MalformedRedirect(
                            f"Redirect without a Location header from {current_url}"
                        )
                    next_url = urllib.parse.urljoin(current_url, location)
                    if not is_safe_url(next_url):
                        raise _SSRFRedirectBlocked(
                            f"Blocked redirect to restricted address: {next_url}"
                        )
                    if response.status_code in (301, 302, 303):
                        current_method = "GET"
                    current_url = next_url
        raise httpx.TooManyRedirects("Exceeded redirect limit", request=None)

    @staticmethod
    def _read_bounded_body(response: httpx.Response) -> bytes:
        """Read at most MAX_WEBHOOK_BODY_BYTES of a hop's body, then stop.

        Redirect bodies are read too, so a body that fails to arrive still
        fails the attempt and it is retried.

        The body is diagnostic only, so an oversized or encoded body never
        changes the delivery outcome; the status code alone decides it.
        """
        # HTTPX decodes before iter_bytes yields, so even a tiny compressed
        # chunk can allocate far beyond the cap: leave encoded bodies unread.
        encoding = response.headers.get("content-encoding", "")
        if encoding.strip().lower() not in ("", "identity"):
            return b""
        chunks = []
        remaining = MAX_WEBHOOK_BODY_BYTES
        # Check the budget before asking for the next chunk, so nothing is
        # pulled from the stream once the cap is reached.
        try:
            for chunk in response.iter_bytes():
                chunks.append(chunk[:remaining])
                remaining -= len(chunks[-1])
                if remaining <= 0:
                    break
        except _ReadBudgetSpent:
            pass
        return b"".join(chunks)

    def _handle_failure(
        self,
        item: DeliveryItem,
        status_code: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Handle a delivery failure.

        Implements retry logic with exponential backoff.
        """
        event = item.event
        webhook_url = item.webhook_url
        attempt = item.attempt

        if attempt < self._max_attempts:
            # Schedule retry
            backoff_index = min(attempt - 1, len(self._backoff_times) - 1)
            backoff_time = self._backoff_times[backoff_index]

            logger.warning(
                f"Delivery failed for event {event.event_id} to {webhook_url} "
                f"(attempt {attempt}/{self._max_attempts}): {error_message}. "
                f"Retrying in {backoff_time}s..."
            )

            # Report retrying status
            if self._on_result:
                result = DeliveryResult(
                    delivery_id=f"{event.subscription.id if event.subscription else ''}:{event.event_id}",
                    correlation_id=event.origin.event_correlation_id or "",
                    event_id=event.event_id,
                    subscription_id=event.subscription.id if event.subscription else "",
                    webhook_url=webhook_url,
                    status=DeliveryStatus.RETRYING,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    status_code=status_code,
                    error_message=error_message,
                )
                try:
                    self._on_result(result)
                except Exception as e:
                    logger.error(f"Error in result callback: {e}")

            # Wait and retry
            time.sleep(backoff_time)

            # Re-enqueue with incremented attempt
            retry_item = DeliveryItem(
                event=event,
                webhook_url=webhook_url,
                attempt=attempt + 1,
            )
            self._queue.put(retry_item)

        else:
            # Max retries exceeded, drop the event
            logger.error(
                f"Max retries exceeded for event {event.event_id} to {webhook_url}. "
                f"Event dropped. Last error: {error_message}"
            )

            # Report dropped status
            if self._on_result:
                result = DeliveryResult(
                    delivery_id=f"{event.subscription.id if event.subscription else ''}:{event.event_id}",
                    correlation_id=event.origin.event_correlation_id or "",
                    event_id=event.event_id,
                    subscription_id=event.subscription.id if event.subscription else "",
                    webhook_url=webhook_url,
                    status=DeliveryStatus.DROPPED,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    status_code=status_code,
                    error_message=error_message,
                )
                try:
                    self._on_result(result)
                except Exception as e:
                    logger.error(f"Error in result callback: {e}")

    @property
    def queue_size(self) -> int:
        """Get the current queue size."""
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        """Check if the worker is running."""
        return self._running
