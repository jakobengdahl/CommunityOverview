#!/usr/bin/env python3
"""
Webhook Listener for Testing Event Subscriptions

A simple HTTP server that receives and logs webhook payloads from the event system.
Useful for testing EventSubscription nodes during development.

Usage:
    python scripts/webhook_listener.py [--port PORT] [--host HOST]

Examples:
    # Start on default port 9000
    python scripts/webhook_listener.py

    # Start on custom port
    python scripts/webhook_listener.py --port 8080

    # Listen on all interfaces
    python scripts/webhook_listener.py --host 0.0.0.0 --port 9000

Then create an EventSubscription with webhook_url pointing to this server:
    http://localhost:9000/webhook
"""

import argparse
import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for receiving webhook payloads."""

    # Statistics
    request_count = 0
    events_received = []

    def log_request(self, code="-", size="-"):
        """Override to suppress default logging (we do our own)."""
        pass

    def do_POST(self):
        """Handle POST requests (webhook events)."""
        WebhookHandler.request_count += 1

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        # Parse JSON
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            payload = {"raw_body": body.decode("utf-8", errors="replace")}

        # Extract event info
        event_type = payload.get("event_type", "unknown")
        event_id = payload.get("event_id", "unknown")
        entity = payload.get("entity", {})
        entity_type = entity.get("type", "unknown")
        entity_id = entity.get("id", "unknown")
        subscription = payload.get("subscription", {})
        sub_name = subscription.get("name", "unknown")

        # Store event
        WebhookHandler.events_received.append({
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
        })

        # Print formatted output
        print("\n" + "=" * 60)
        print(f"WEBHOOK RECEIVED #{WebhookHandler.request_count}")
        print("=" * 60)
        print(f"Time:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Path:         {self.path}")
        print(f"Event Type:   {event_type}")
        print(f"Event ID:     {event_id}")
        print(f"Entity Type:  {entity_type}")
        print(f"Entity ID:    {entity_id}")
        print(f"Subscription: {sub_name}")

        # Print entity details
        entity_data = entity.get("data", {})
        if entity_data:
            print("-" * 60)
            if entity_data.get("after"):
                after = entity_data["after"]
                print(f"Name:         {after.get('name', 'N/A')}")
                if after.get("description"):
                    desc = after["description"][:100] + "..." if len(after.get("description", "")) > 100 else after.get("description", "")
                    print(f"Description:  {desc}")

            if entity_data.get("patch"):
                print(f"Changed:      {list(entity_data['patch'].keys())}")

        # Print origin info
        origin = payload.get("origin", {})
        if origin.get("event_origin") or origin.get("event_session_id"):
            print("-" * 60)
            print(f"Origin:       {origin.get('event_origin', 'N/A')}")
            print(f"Session ID:   {origin.get('event_session_id', 'N/A')}")

        print("=" * 60)

        # Send response
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "ok": True,
            "received": event_id,
            "count": WebhookHandler.request_count,
        }
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def do_GET(self):
        """Handle GET requests (for health checks or stats)."""
        if self.path == "/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            stats = {
                "total_requests": WebhookHandler.request_count,
                "recent_events": WebhookHandler.events_received[-10:],
            }
            self.wfile.write(json.dumps(stats, indent=2).encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            msg = f"Webhook listener running. Received {WebhookHandler.request_count} events.\n"
            msg += "POST to /webhook to receive events.\n"
            msg += "GET /stats for statistics."
            self.wfile.write(msg.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(
        description="Webhook listener for testing event subscriptions"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=9000,
        help="Port to listen on (default: 9000)"
    )
    parser.add_argument(
        "--host", "-H",
        type=str,
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), WebhookHandler)

    print("=" * 60)
    print("WEBHOOK LISTENER")
    print("=" * 60)
    print(f"Listening on: http://{args.host}:{args.port}")
    print(f"Webhook URL:  http://localhost:{args.port}/webhook")
    print(f"Stats URL:    http://localhost:{args.port}/stats")
    print("-" * 60)
    print("Waiting for webhooks... (Ctrl+C to stop)")
    print("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        print(f"Total events received: {WebhookHandler.request_count}")
        server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
