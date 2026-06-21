"""
Integration tests for the event system with a real webhook server.

These tests verify the complete flow:
1. Create an EventSubscription node
2. Create/update/delete nodes that match the subscription
3. Verify the webhook receives the correct events
"""

import json
import time
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Any, Optional
import pytest

from backend.core import GraphStorage, Node, Edge
from backend.core.events.models import EventContext


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler that collects received webhook payloads."""

    # Class-level storage for received events (shared across instances)
    received_events: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def do_POST(self):
        """Handle POST requests (webhook calls)."""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode('utf-8'))
            with self.lock:
                self.received_events.append(payload)
            print(f"WEBHOOK: Received event - {payload.get('event_type')}")
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        except Exception as e:
            print(f"WEBHOOK: Error processing request: {e}")
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default HTTP logging."""
        pass


class WebhookServer:
    """
    A temporary webhook server for testing.

    Usage:
        with WebhookServer(port=9999) as server:
            webhook_url = server.url
            # ... do stuff that triggers webhooks ...
            events = server.get_events()
    """

    def __init__(self, port: int = 0):
        """
        Initialize the webhook server.

        Args:
            port: Port to listen on. Use 0 for a random available port.
        """
        self.port = port
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self._actual_port: int = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        """Start the webhook server in a background thread."""
        # Clear any previous events
        WebhookHandler.received_events = []

        self.server = HTTPServer(('127.0.0.1', self.port), WebhookHandler)
        self._actual_port = self.server.server_port

        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True
        )
        self.thread.start()
        print(f"WEBHOOK: Server started on port {self._actual_port}")

    def stop(self):
        """Stop the webhook server."""
        if self.server:
            self.server.shutdown()
            self.server = None
        if self.thread:
            self.thread.join(timeout=2.0)
            self.thread = None
        print("WEBHOOK: Server stopped")

    @property
    def url(self) -> str:
        """Get the webhook URL."""
        return f"http://127.0.0.1:{self._actual_port}/webhook"

    def get_events(self, timeout: float = 2.0) -> List[Dict[str, Any]]:
        """
        Get received events, waiting for them to arrive.

        Args:
            timeout: Maximum time to wait for events

        Returns:
            List of received event payloads
        """
        # Wait a bit for events to be delivered
        time.sleep(timeout)
        with WebhookHandler.lock:
            return list(WebhookHandler.received_events)

    def clear_events(self):
        """Clear received events."""
        with WebhookHandler.lock:
            WebhookHandler.received_events = []


class TestEventIntegration:
    """Integration tests for the event system."""

    @pytest.fixture
    def storage(self, tmp_path):
        """Create a temporary GraphStorage with events enabled."""
        graph_path = tmp_path / "test_graph.json"
        storage = GraphStorage(str(graph_path))
        storage.setup_events(enabled=True)
        yield storage
        storage.shutdown_events()

    def test_webhook_receives_node_create_event(self, storage):
        """Test that creating a node triggers a webhook call."""
        with WebhookServer() as server:
            # Create an EventSubscription that listens for all node creates
            subscription = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Test Subscription",
                description="Test subscription for integration tests",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node"},
                        "operations": ["create"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            storage.add_nodes([subscription], [])
            server.clear_events()  # Clear the subscription creation event

            # Create a node that should trigger the webhook
            test_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="Test Initiative",
                description="This is a test initiative",
            )
            storage.add_nodes([test_node], [])

            # Wait for and verify the webhook received the event
            events = server.get_events(timeout=2.0)

            assert len(events) >= 1, f"Expected at least 1 event, got {len(events)}"

            # Find the Initiative create event
            initiative_events = [
                e for e in events
                if e.get('entity', {}).get('type') == 'Initiative'
            ]
            assert len(initiative_events) == 1, f"Expected 1 Initiative event, got {len(initiative_events)}"

            event = initiative_events[0]
            assert event['event_type'] == 'node.create'
            assert event['entity']['id'] == test_node.id
            assert event['entity']['data']['after']['name'] == 'Test Initiative'

    def test_webhook_receives_node_update_event(self, storage):
        """Test that updating a node triggers a webhook call."""
        with WebhookServer() as server:
            # Create subscription for update events
            subscription = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Update Subscription",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node", "node_types": ["Initiative"]},
                        "operations": ["update"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            storage.add_nodes([subscription], [])

            # Create a test node
            test_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="Original Name",
            )
            storage.add_nodes([test_node], [])
            server.clear_events()

            # Update the node
            storage.update_node(test_node.id, {"name": "Updated Name"})

            # Verify the webhook received the update event
            events = server.get_events(timeout=2.0)

            assert len(events) >= 1, f"Expected at least 1 event, got {len(events)}"

            event = events[-1]  # Get the last event (the update)
            assert event['event_type'] == 'node.update'
            assert event['entity']['data']['before']['name'] == 'Original Name'
            assert event['entity']['data']['after']['name'] == 'Updated Name'
            assert event['entity']['data']['patch']['name'] == 'Updated Name'

    def test_webhook_receives_node_delete_event(self, storage):
        """Test that deleting a node triggers a webhook call."""
        with WebhookServer() as server:
            # Create subscription for delete events
            subscription = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Delete Subscription",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node", "node_types": ["Resource"]},
                        "operations": ["delete"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            storage.add_nodes([subscription], [])

            # Create a test node
            test_node = Node(
                id=str(uuid.uuid4()),
                type="Resource",
                name="Test Resource",
            )
            storage.add_nodes([test_node], [])
            server.clear_events()

            # Delete the node
            storage.delete_nodes([test_node.id], confirmed=True)

            # Verify the webhook received the delete event
            events = server.get_events(timeout=2.0)

            assert len(events) == 1, f"Expected 1 event, got {len(events)}"

            event = events[0]
            assert event['event_type'] == 'node.delete'
            assert event['entity']['id'] == test_node.id
            assert event['entity']['data']['before']['name'] == 'Test Resource'
            assert event['entity']['data']['after'] is None

    def test_filter_by_node_type(self, storage):
        """Test that subscriptions correctly filter by node type."""
        with WebhookServer() as server:
            # Create subscription that only listens for Initiative nodes
            subscription = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Initiative Only",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node", "node_types": ["Initiative"]},
                        "operations": ["create"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            storage.add_nodes([subscription], [])
            server.clear_events()

            # Create an Actor node (should NOT trigger)
            actor = Node(
                id=str(uuid.uuid4()),
                type="Actor",
                name="Test Actor",
            )
            storage.add_nodes([actor], [])

            # Create an Initiative node (should trigger)
            initiative = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="Test Initiative",
            )
            storage.add_nodes([initiative], [])

            # Verify only the Initiative event was received
            events = server.get_events(timeout=2.0)

            assert len(events) == 1, f"Expected 1 event, got {len(events)}"
            assert events[0]['entity']['type'] == 'Initiative'

    def test_filter_by_keywords(self, storage):
        """Test that subscriptions correctly filter by keywords."""
        with WebhookServer() as server:
            # Create subscription that only listens for nodes containing "AI"
            subscription = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="AI Keywords",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node"},
                        "operations": ["create"],
                        "keywords": {"any": ["AI", "machine learning"]},
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            storage.add_nodes([subscription], [])
            server.clear_events()

            # Create a node without AI keywords (should NOT trigger)
            non_ai_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="Cloud Infrastructure Project",
                description="A project about cloud computing",
            )
            storage.add_nodes([non_ai_node], [])

            # Create a node with AI keywords (should trigger)
            ai_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="AI Research Initiative",
                description="Research on artificial intelligence and machine learning",
            )
            storage.add_nodes([ai_node], [])

            # Verify only the AI node event was received (filter out EventSubscription events)
            events = server.get_events(timeout=2.0)
            initiative_events = [
                e for e in events
                if e.get('entity', {}).get('type') == 'Initiative'
            ]

            assert len(initiative_events) == 1, f"Expected 1 Initiative event, got {len(initiative_events)}"
            assert 'AI' in initiative_events[0]['entity']['data']['after']['name']

    def test_loop_prevention_by_origin(self, storage):
        """Test that loop prevention blocks events from ignored origins."""
        with WebhookServer() as server:
            # Create subscription that ignores events from "mcp" origin
            subscription = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Ignore MCP",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node"},
                        "operations": ["create"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                        "ignore_origins": ["mcp"],
                    }
                }
            )
            storage.add_nodes([subscription], [])
            server.clear_events()

            # Create a node with MCP origin (should be blocked)
            mcp_context = EventContext(event_origin="mcp")
            mcp_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="MCP Created Node",
            )
            storage.add_nodes([mcp_node], [], event_context=mcp_context)

            # Create a node with web-ui origin (should trigger)
            web_context = EventContext(event_origin="web-ui")
            web_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="Web Created Node",
            )
            storage.add_nodes([web_node], [], event_context=web_context)

            # Verify only the web-ui event was received (filter out EventSubscription events)
            events = server.get_events(timeout=2.0)
            initiative_events = [
                e for e in events
                if e.get('entity', {}).get('type') == 'Initiative'
            ]

            assert len(initiative_events) == 1, f"Expected 1 Initiative event, got {len(initiative_events)}"
            assert initiative_events[0]['entity']['data']['after']['name'] == 'Web Created Node'

    def test_multiple_subscriptions(self, storage):
        """Test that multiple subscriptions can receive the same event."""
        with WebhookServer() as server:
            # Create two subscriptions
            sub1 = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Subscription 1",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node"},
                        "operations": ["create"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            sub2 = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Subscription 2",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node"},
                        "operations": ["create"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            storage.add_nodes([sub1, sub2], [])
            server.clear_events()

            # Create a node
            test_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="Test Node",
            )
            storage.add_nodes([test_node], [])

            # Verify both subscriptions received the event
            events = server.get_events(timeout=2.0)

            # Should get 2 events (one per subscription)
            initiative_events = [
                e for e in events
                if e.get('entity', {}).get('type') == 'Initiative'
            ]
            assert len(initiative_events) == 2, f"Expected 2 Initiative events, got {len(initiative_events)}"

            # Verify they have different subscription names
            sub_names = {e.get('subscription', {}).get('name') for e in initiative_events}
            assert 'Subscription 1' in sub_names
            assert 'Subscription 2' in sub_names

    def test_event_includes_context(self, storage):
        """Test that events include the original context."""
        with WebhookServer() as server:
            subscription = Node(
                id=str(uuid.uuid4()),
                type="EventSubscription",
                name="Context Test",
                metadata={
                    "filters": {
                        "target": {"entity_kind": "node"},
                        "operations": ["create"],
                    },
                    "delivery": {
                        "webhook_url": server.url,
                    }
                }
            )
            storage.add_nodes([subscription], [])
            server.clear_events()

            # Create a node with full context
            context = EventContext(
                event_origin="web-ui",
                event_session_id="session-123",
                event_correlation_id="correlation-456",
            )
            test_node = Node(
                id=str(uuid.uuid4()),
                type="Initiative",
                name="Contextualized Node",
            )
            storage.add_nodes([test_node], [], event_context=context)

            events = server.get_events(timeout=2.0)

            # Find the Initiative event
            initiative_events = [
                e for e in events
                if e.get('entity', {}).get('type') == 'Initiative'
            ]
            assert len(initiative_events) == 1

            event = initiative_events[0]
            assert event['origin']['event_origin'] == 'web-ui'
            assert event['origin']['event_session_id'] == 'session-123'
            assert event['origin']['event_correlation_id'] == 'correlation-456'
