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
from datetime import datetime
import requests

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


class DeliveryItem:
    """An item in the delivery queue."""

    def __init__(self, event: Event, webhook_url: str, attempt: int = 1):
        self.event = event
        self.webhook_url = webhook_url
        self.attempt = attempt
        self.enqueued_at = datetime.utcnow()


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
        logger.debug(
            f"Enqueued event {event.event_id} for delivery to {webhook_url}"
        )

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
            # Prepare payload
            payload = event.to_webhook_payload()

            # Make HTTP request
            response = requests.post(
                webhook_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "CommunityGraph-Events/1.0",
                    "X-Event-ID": event.event_id,
                    "X-Event-Type": event.event_type.value,
                },
                timeout=self._timeout,
            )

            # Check response
            if response.status_code >= 200 and response.status_code < 300:
                # Success
                result = DeliveryResult(
                    event_id=event.event_id,
                    subscription_id=event.subscription.id if event.subscription else "",
                    webhook_url=webhook_url,
                    status=DeliveryStatus.SUCCESS,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    status_code=response.status_code,
                    delivered_at=datetime.utcnow(),
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

        except requests.Timeout:
            self._handle_failure(
                item,
                error_message="Request timed out",
            )
            return

        except requests.RequestException as e:
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
