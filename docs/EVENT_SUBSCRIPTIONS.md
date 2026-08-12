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

Agent nodes (type: `Agent`) define AI agents that react to graph events. An Agent links to an EventSubscription that defines which mutations trigger it. Agents can also be triggered on a time-based schedule — see `docs/AGENT_SCHEDULING.md`.

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
  "schema_version": "1.0",
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

The top-level `schema_version` identifies the shape of the event envelope.
Subscribers should treat it as a contract version: additive changes keep the
same major version, while a backwards-incompatible change bumps it and is
accompanied by a migration note.

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
- **"Create webhook subscription"**: Creates an EventSubscription node
- **"Create agent"**: Creates an Agent with its EventSubscription

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

## AgentRun History

Each time an agent processes a trigger (a schedule firing or a matching graph
event), the run is recorded as a durable **AgentRun** behind the execution-store
seam ([`DURABLE_EXECUTION_CONTRACT.md`](DURABLE_EXECUTION_CONTRACT.md)). A run
captures the trigger kind, agent, status (`running` → `succeeded` / `failed`),
attempts, correlation/session/origin, timestamps, and a small terminal result or
error. History survives a restart when a durable store is configured and can be
swapped for a hosted store without changing the API or UI.

This is a **history sink**: event delivery still uses the in-memory queue
(below); recording history never blocks or breaks processing. The store backing
history is chosen by `AGENTS_RUN_HISTORY_DB` — a SQLite file path for durable
history, or an in-memory (volatile) store when unset.

Read the history through the API:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/agents/runs` | List runs newest-first; filter by `agent_id`, `kind` (`scheduled`/`event`), `status`, `limit` |
| GET | `/agents/runs/{run_id}` | Fetch a single run |

In the web UI, open an agent for editing and choose **View run history**.

## Agent governance: autonomy levels and proposals

Each agent has an **autonomy level** (`metadata.autonomy_level`) that bounds what
its runs may do, enforced at the tool-execution boundary together with the
agent's optional `tool_allowlist`:

| Level | Mutating tools (add/update/delete nodes/edges, write_file) |
|-------|-----------------------------------------------------------|
| `observe` | Blocked — read-only |
| `assist` | Blocked — read-only |
| `propose` | Recorded as a durable **Proposal**; approving does not apply it (a human applies) |
| `act_after_approval` | Recorded as a durable Proposal; approving **applies** the captured action |

Read-only tools always run (subject to the allowlist). A mutating call under
`propose` / `act_after_approval` is **not executed** — it becomes a durable
`Proposal` (tool, arguments, agent, autonomy level, run correlation) with a
persistent approve/reject decision and attribution. Advanced multi-approver /
enterprise policy is a commercial-layer concern on top of this baseline.

**Default:** when `autonomy_level` is unset it defaults to `act_after_approval`
— an agent's mutating actions require a human approval before they take effect.

Manage proposals through the API (or the **View proposals** button in the agent
editor):

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/agents/proposals` | List proposals; filter by `agent_id`, `status`, `limit` |
| GET | `/agents/proposals/{id}` | Fetch a single proposal |
| POST | `/agents/proposals/{id}/approve` | Approve (applies the action for act_after_approval) |
| POST | `/agents/proposals/{id}/reject` | Reject |

Proposals are persisted behind a replaceable store: a SQLite file when
`AGENTS_GOVERNANCE_DB` is set, else a volatile in-memory store. Decisions are
sticky — a decided proposal is never re-decided or re-applied.

## Session-scoped auto-add agents

A **session-scoped auto-add agent** is a lightweight, deterministic reactor built
directly on the `node.create` event. It watches for newly created nodes that
match a pattern and **adds each match to one visualization session's live view**
— additively (reusing the `add_to_visualization` push path), so it never clears
what the session already shows.

It is deliberately *not* an `Agent` node:

- **Session-scoped.** Its rule lives in memory keyed by the visualization
  `session_id` (like the pulse-trigger token), can only ever push to that one
  session, and is pruned when the session goes away. It cannot leak nodes into
  another session.
- **Deterministic.** No LLM and no graph mutation — matching a created node and
  pushing it to a session is a pure reaction. Because it never writes to the
  graph it cannot generate further events, so — unlike graph-mutating agents — it
  needs no loop-prevention wiring.
- **Same match model.** The pattern reuses the subscription filter shape:
  `node_types` (any-of) and/or `keywords` (case-insensitive, matched against
  name/description/summary/tags). At least one constraint is required — a rule
  matching every created node is rejected, so it can't flood the canvas.

Configure one through either surface (both back the same in-memory registry):

| Surface | Operation |
|---------|-----------|
| MCP tool | `create_session_auto_add_agent(visualization_session_id, node_types?, keywords?)` |
| MCP tool | `list_session_auto_add_agents(visualization_session_id)` |
| MCP tool | `remove_session_auto_add_agent(visualization_session_id, agent_id)` |
| REST | `POST/GET /sessions/{id}/auto-add-agents`, `DELETE /sessions/{id}/auto-add-agents/{agent_id}` |

Example: an agent with `node_types=["Actor"]` on the session you are viewing adds
every newly created Actor node into that view as it appears, without disturbing
the nodes already on the canvas.

## Limitations (PoC)

- **In-memory delivery queue**: Events are delivered through an in-memory queue,
  lost on restart. (AgentRun *history* is recorded durably — see above — but the
  delivery queue itself is not yet wired to the durable store.)
- **No guaranteed delivery**: Failed events are dropped after retries
- **Single process**: Works within one process only
- **Simple filtering**: No complex query expressions

## Future Enhancements

The following are planned but not implemented:
- Persistent event queue with durability guarantees — the seam these will
  implement (durable queue/job state, retries, dead-letter, restart recovery) is
  specified in [`DURABLE_EXECUTION_CONTRACT.md`](DURABLE_EXECUTION_CONTRACT.md)
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
