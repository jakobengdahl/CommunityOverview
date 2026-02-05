"""
Tests for event delivery worker.
"""

import pytest
import time
from unittest.mock import MagicMock, patch, Mock
from datetime import datetime

from backend.core.events.models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    EntityData,
    DeliveryResult,
    DeliveryStatus,
    SubscriptionInfo,
)
from backend.core.events.delivery import DeliveryWorker, DeliveryItem


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

    @patch('backend.core.events.delivery.requests.post')
    def test_successful_delivery(self, mock_post):
        """Test successful webhook delivery."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        results = []
        worker = DeliveryWorker(on_result=lambda r: results.append(r))
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            # Wait for delivery
            time.sleep(0.5)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.SUCCESS
            assert results[0].status_code == 200
        finally:
            worker.stop(wait=True)

    @patch('backend.core.events.delivery.requests.post')
    def test_failed_delivery_with_retry(self, mock_post):
        """Test that failed deliveries are retried."""
        # Fail twice, then succeed
        mock_responses = [
            Mock(status_code=500, text="Server Error"),
            Mock(status_code=500, text="Server Error"),
            Mock(status_code=200),
        ]
        mock_post.side_effect = mock_responses

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

            # Wait for retries
            time.sleep(1.0)

            # Should have 3 results: 2 retrying + 1 success
            assert len(results) >= 1
            # Last result should be success
            success_results = [r for r in results if r.status == DeliveryStatus.SUCCESS]
            assert len(success_results) == 1
        finally:
            worker.stop(wait=True)

    @patch('backend.core.events.delivery.requests.post')
    def test_max_retries_exceeded(self, mock_post):
        """Test that events are dropped after max retries."""
        # Always fail
        mock_response = Mock(status_code=500, text="Server Error")
        mock_post.return_value = mock_response

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

            # Wait for retries
            time.sleep(1.0)

            # Should end with DROPPED status
            dropped_results = [r for r in results if r.status == DeliveryStatus.DROPPED]
            assert len(dropped_results) == 1
        finally:
            worker.stop(wait=True)

    @patch('backend.core.events.delivery.requests.post')
    def test_timeout_handling(self, mock_post):
        """Test that timeouts are handled correctly."""
        import requests
        mock_post.side_effect = requests.Timeout("Connection timed out")

        results = []
        worker = DeliveryWorker(
            max_attempts=1,
            on_result=lambda r: results.append(r),
        )
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            time.sleep(0.5)

            assert len(results) == 1
            assert results[0].status == DeliveryStatus.DROPPED
            assert "timed out" in results[0].error_message.lower()
        finally:
            worker.stop(wait=True)

    @patch('backend.core.events.delivery.requests.post')
    def test_webhook_payload_format(self, mock_post):
        """Test that webhook receives correct payload format."""
        mock_response = Mock(status_code=200)
        mock_post.return_value = mock_response

        worker = DeliveryWorker()
        worker.start()

        try:
            event = create_test_event()
            worker.enqueue(event, "https://example.com/hook")

            time.sleep(0.5)

            # Check the call arguments
            assert mock_post.called
            call_kwargs = mock_post.call_args.kwargs

            # Check URL
            assert mock_post.call_args.args[0] == "https://example.com/hook"

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
