# Event Subscriptions & Webhooks

This document describes the event system for graph mutation webhooks.

## Overview

The event system allows external services to receive notifications when the graph is modified. This enables:

- **Webhooks**: Send HTTP POST requests to external endpoints when nodes are created, updated, or deleted
- **Agent triggers**: Prepare for future AI agent functionality by storing subscription configurations
- **Audit logging**: Track all changes to the graph with full before/after state

## Key Concepts

### EventSubscription Nodes

Subscriptions are stored as nodes in the graph itself (type: `EventSubscription`). This means:
- They can be created, edited, and visualized like any other node
- They persist with the rest of the graph data
- They can be queried and managed via the standard API

### Agent Nodes

Agent nodes (type: `Agent`) store configuration for future AI agents. An Agent links to an EventSubscription that defines which events trigger the agent.

**Note:** The agent runtime is NOT implemented - these nodes only store configuration for future functionality.

### Event Context

Every mutation can include context for tracking and loop prevention:
- `event_origin`: Source of the mutation (e.g., "web-ui", "mcp", "agent:my-agent")
- `event_session_id`: Unique session identifier
- `event_correlation_id`: For chaining related events

## Event Types

The system generates events for:

| Event Type | Description |
|------------|-------------|
| `node.create` | A new node was added |
| `node.update` | An existing node was modified |
| `node.delete` | A node was removed |
| `edge.create` | A new edge was added |
| `edge.delete` | An edge was removed |

## Subscription Configuration

Configuration is stored in the EventSubscription node's `metadata` field:

```json
{
  "filters": {
    "target": {
      "entity_kind": "node",
      "node_types": ["Actor", "Initiative"]
    },
    "operations": ["create", "update"],
    "keywords": {
      "any": ["AI", "digitalisering"]
    },
    "federation": {
      "scope": "local_only",
      "include_graph_ids": [],
      "max_distance": null
    }
  },
  "delivery": {
    "webhook_url": "https://your-service.com/webhook",
    "ignore_origins": ["agent:my-agent"],
    "ignore_session_ids": []
  }
}
```

### Filter Options

| Field | Description |
|-------|-------------|
| `target.entity_kind` | "node" or "edge" |
| `target.node_types` | Array of node types to match (empty = all) |
| `operations` | Array of operations: "create", "update", "delete" |
| `keywords.any` | Match if any keyword appears in name/description/summary/tags |
| `federation.scope` | `local_only` (default) or `local_and_federated` |
| `federation.include_graph_ids` | Optional allow-list for federated source graph IDs |
| `federation.max_distance` | Optional max federation distance for federated events |

### Delivery Options

| Field | Description |
|-------|-------------|
| `webhook_url` | URL to POST events to (required) |
| `ignore_origins` | Don't deliver events from these origins (loop prevention) |
| `ignore_session_ids` | Don't deliver events from these sessions |

## Webhook Payload

Events are delivered as JSON POST requests:

```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_type": "node.create",
  "occurred_at": "2024-01-15T10:30:00.000000Z",
  "origin": {
    "event_origin": "web-ui",
    "event_session_id": "session-abc123",
    "event_correlation_id": null
  },
  "entity": {
    "kind": "node",
    "id": "node-123",
    "type": "Actor",
    "data": {
      "before": null,
      "after": {
        "name": "Skatteverket",
        "type": "Actor",
        "description": "Swedish Tax Agency"
      },
      "patch": null
    }
  },
  "subscription": {
    "id": "sub-456",
    "name": "New Actor Notifications"
  }
}
```

### HTTP Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | `application/json` |
| `User-Agent` | `CommunityGraph-Events/1.0` |
| `X-Event-ID` | The event's unique ID |
| `X-Event-Type` | Event type (e.g., "node.create") |

### Update Events

For `node.update` events, both `before` and `after` states are included:

```json
{
  "entity": {
    "data": {
      "before": { "name": "Old Name", "description": "Old desc" },
      "after": { "name": "New Name", "description": "Old desc" },
      "patch": { "name": "New Name" }
    }
  }
}
```

## Loop Prevention

To prevent infinite loops when agents modify the graph:

1. **Set `ignore_origins`**: Configure subscriptions to ignore events from specific sources
2. **Use unique session IDs**: Each client should use a consistent session ID
3. **Agent self-exclusion**: Agents should set their origin to `agent:<id>` and exclude themselves

Example: An agent subscription that ignores its own changes:
```json
{
  "delivery": {
    "webhook_url": "https://my-agent.com/hook",
    "ignore_origins": ["agent:my-agent-id"]
  }
}
```

## Retry Policy

Failed webhook deliveries are retried with exponential backoff:

| Attempt | Wait Before Retry |
|---------|-------------------|
| 1 | 0.5 seconds |
| 2 | 2.0 seconds |
| 3 | 5.0 seconds |

After 3 failed attempts, the event is dropped and logged.

## API Usage

### REST API

All mutation endpoints accept optional event context:

```bash
# Add nodes with event context
curl -X POST http://localhost:8000/api/nodes \
  -H "Content-Type: application/json" \
  -d '{
    "nodes": [{"name": "Test", "type": "Actor"}],
    "edges": [],
    "event_origin": "my-service",
    "event_session_id": "session-123"
  }'
```

### Creating Subscriptions via API

```bash
curl -X POST http://localhost:8000/api/nodes \
  -H "Content-Type: application/json" \
  -d '{
    "nodes": [{
      "name": "My Webhook",
      "type": "EventSubscription",
      "description": "Notifies on new initiatives",
      "metadata": {
        "filters": {
          "target": {"entity_kind": "node", "node_types": ["Initiative"]},
          "operations": ["create"]
        },
        "delivery": {
          "webhook_url": "https://my-service.com/hook"
        }
      }
    }],
    "edges": []
  }'
```

### MCP Tools

MCP tools automatically set `event_origin` to "mcp" and accept optional session/correlation IDs.

## Web UI

Right-click on the graph canvas to access:
- **"Skapa webhook-prenumeration"**: Create an EventSubscription node
- **"Skapa agent"**: Create an Agent with its EventSubscription

The web UI automatically generates a unique session ID per browser session.

## Enabling Events

Events are disabled by default. To enable:

```python
from backend.core import GraphStorage

storage = GraphStorage("graph.json")
storage.setup_events(
    enabled=True,
    max_attempts=3,
    backoff_times=[0.5, 2.0, 5.0]
)
```

In the server configuration (`backend/api_host/server.py`), add:
```python
storage.setup_events(enabled=True)
```

## Limitations (PoC)

- **In-memory queue**: Events are not persisted; lost on restart
- **No guaranteed delivery**: Failed events are dropped after retries
- **Single process**: Works within one process only
- **Simple filtering**: No complex query expressions

## Future Enhancements

The following are planned but not implemented:
- Agent runtime for executing code in response to events
- Persistent event queue with durability guarantees
- Advanced filtering with query expressions
- Event replay and debugging tools

### Federated Event Matching

By default, subscriptions are **local-only** for backward compatibility.

To include federated changes in subscriptions, set:

```json
{
  "filters": {
    "federation": {
      "scope": "local_and_federated",
      "include_graph_ids": ["esam-main"],
      "max_distance": 1
    }
  }
}
```

When omitted, `scope` defaults to `local_only`.

Federated cache updates emitted by the sync engine use `origin.event_origin = "federation-sync"` for both node and edge events.
