# Federated Graph Design

## Overview

The service supports one active local graph and can federate with other graph instances, while keeping the user workflow simple and stable. The federation implementation follows the hybrid local-cache model described below (Option C).

The local graph is the authoritative source. External graphs are fetched on startup and on a configurable sync interval, then cached locally. Search and traversal merge local and cached results. See the **Practical verification guide** at the bottom of this document to validate the current implementation.

Three interaction modes are supported:

- GUI (web)
- MCP tools
- Direct file (`graph.json`)

## Design goals

- Administrators define external graph connections through startup configuration only — not via GUI or chat.
- End-user workflows remain unchanged: they still work in “their graph”, but search/analysis can include connected graphs.
- Ownership and distance are explicit (local graph vs external graph, 1 hop, 2 hops, etc.).
- Different capabilities per external graph endpoint:
  - file URL (`graph.json`) = read-only
  - MCP endpoint = read/write depending on credentials and policy
  - GUI URL = informational/deep-link only
- Instability when one or more external graphs are unavailable does not affect local operations.
- Controlled federation depth (max number of hops).
- Subscriptions can optionally include federated changes.

## Non-goals

- Full distributed transactions across graphs.
- Guaranteed real-time consistency between all connected graphs.
- GUI/chat users modifying federation topology at runtime.

## Design alternatives

### Option A: Live federation only (query remote on every request)

**How it works:** all searches/traversals call remote endpoints in real time.

**Pros**
- Always freshest data.
- No local cache complexity.

**Cons**
- High latency and cascading failures.
- Complex timeout behavior.
- Poor UX when remote graphs are intermittently down.

### Option B: Full replication (copy all remote graphs locally)

**How it works:** external graphs are fully cloned into local storage.

**Pros**
- Fast local query performance.
- Strong resilience during remote downtime.

**Cons**
- Heavy storage and sync cost.
- Harder data ownership boundaries.
- Higher risk of stale or conflicting data.

### Option C (recommended): Hybrid federation with selective local cache + adoption

**How it works:**
- Keep local graph authoritative for local nodes.
- Cache external nodes/edges with provenance metadata.
- Support node adoption/clone into local graph when linking requires local ownership.
- Query planner combines local + cached + optional live remote enrichment.

**Pros**
- Balanced resilience, performance, and freshness.
- External outages degrade gracefully.
- Supports read-only and read/write remote interaction models.

**Cons**
- Requires sync orchestration and provenance tracking.

## Recommended architecture

### 1. Federation configuration file (startup-only)

Add a dedicated config file, for example `config/default/federation_config.json`:

```json
{
  "federation": {
    "enabled": true,
    "max_traversal_depth": 2,
    "default_timeout_ms": 1200,
    "allow_live_remote_enrichment": true,
    "graphs": [
      {
        "graph_id": "esam-main",
        "display_name": "eSam Federation Graph",
        "enabled": true,
        "trust_level": "partner",
        "max_depth_override": 1,
        "endpoints": {
          "graph_json_url": "https://esam.example/graph.json",
          "mcp_url": "https://esam.example/mcp",
          "gui_url": "https://esam.example/web/"
        },
        "capabilities": {
          "allow_read": true,
          "allow_write": false,
          "allow_adopt": true
        },
        "sync": {
          "mode": "scheduled",
          "interval_seconds": 300,
          "on_startup": true,
          "on_demand": true
        },
        "auth": {
          "type": "bearer",
          "env_token": "ESAM_MCP_TOKEN"
        }
      }
    ]
  }
}
```

Rules:
- Loaded at startup only.
- Not editable via GUI/chat/MCP assistant tools.
- Invalid or unreachable entries become `degraded` state, not fatal startup errors.

### 2. Canonical provenance model

Add provenance fields for all federated entities:

- `origin_graph_id` (where entity was created)
- `origin_node_id` / `origin_edge_id`
- `federation_distance` (0 = local, 1 = directly connected graph, etc.)
- `federation_path` (graph chain used to discover entity)
- `sync_state` (`fresh`, `stale`, `unreachable`)
- `last_synced_at`

This allows explicit labeling in UI, filtering in API, and safe merge behavior.

### 3. Node adoption / clone mechanism

When users need to create local links to an external node:

- Create a **local adopted node** with stable reference to source:
  - `adopted_from.graph_id`
  - `adopted_from.node_id`
  - `adopted_from.version_hash` (if available)
- Keep adopted node editable only according to policy.
- Preserve a link type like `FEDERATED_REF` or `ADOPTED_FROM`.

This avoids direct hard dependencies on remote write availability while enabling rich local modeling.

### 4. Federation query planner

For search/analysis requests:

1. Query local graph first.
2. Merge cached federated results up to allowed depth.
3. Optionally enrich with live remote calls if enabled and within timeout budget.
4. Return partial results with source metadata and warning flags.

Planner controls:
- Depth budget (global + per-graph override)
- Timeout budget per remote
- Max remote fan-out to avoid explosion
- Circuit breaker state per remote graph

### 5. Sync engine

Support multiple triggers:

- Startup sync (`on_startup`)
- Scheduled sync (`interval_seconds`)
- On-demand sync (admin/API internal trigger)

Behaviors:
- Incremental sync when remote supports watermark/etag.
- Fallback full fetch when needed.
- Store cache snapshot locally for offline reads.
- Never block user operations on sync failures.

### 6. Resilience and stability patterns

Mandatory controls:

- Strict timeout per remote call.
- Circuit breaker with cooldown.
- Retry with jitter (small bounded attempts).
- Bounded queue for sync jobs.
- “Last known good” cache serving.
- Health state per graph: `healthy`, `degraded`, `offline`.

If a remote graph is down:
- Continue serving local and cached data.
- Mark affected results as stale.
- Emit operational telemetry and warning banners (admin-visible).

### 7. Subscription model extension

Extend subscription filters with federation scope:

```json
{
  "filters": {
    "federation": {
      "scope": "local_and_federated",
      "include_graph_ids": ["esam-main"],
      "max_distance": 1,
      "event_sources": ["sync", "live_remote"]
    }
  }
}
```

Principles:
- Default remains local-only for backwards compatibility.
- Federated events are opt-in.
- Event payload must include origin graph metadata.

### 8. UX requirements (important)

Even though federation is transparent, users must see provenance:

- Node badge: `Local` / `External: <graph>`.
- Distance badge: `0`, `1`, `2` hops.
- Filter toggles: local only / include external.
- Search results grouped by source graph.
- Detail panel shows sync freshness and source links (GUI/MCP/file).

## Security and governance

- Credentials for remote MCP stored in environment/secrets, never in graph data.
- Per-graph capability policy (read/write/adopt).
- Audit log for cross-graph writes and adoptions.
- Optional allowlist for reachable federation domains.

## Implementation status

### Implemented (Phases 0–3)

- Federation config schema and validation (`config/default/federation_config.json`)
- Provenance model: `origin_graph_id`, `federation_distance`, `sync_state`, `last_synced_at`
- Read-only connector for remote `graph.json` fetch
- Local cache store for external snapshots
- Search/traversal merges local + cached results with depth budget enforcement
- Provenance metadata in REST and MCP responses
- UI depth selector and source-graph badges
- Startup and scheduled sync with configurable interval
- Timeouts, circuit breaker, per-graph health states (`healthy`/`degraded`/`offline`)
- `GET /federation/status` diagnostics endpoint
- `POST /federation/sync` on-demand sync endpoint
- Node adoption: `POST /api/federation/adopt` clones a federated node into the local graph
- Federated event subscriptions (`scope: local_and_federated`)

### Remaining / future work

- **Phase 4 (MCP federation):** controlled cross-graph writes via MCP connector with scoped credentials
- **Phase 5 (advanced subscriptions):** live-remote event sources beyond sync-engine events

## Critical acceptance criteria

- Service starts even if configured remote graphs are unreachable.
- Local graph operations remain functional during remote outages.
- All federated results include source graph + distance metadata.
- Depth limits are enforced for both traversal and search expansion.
- Subscription triggers can explicitly include/exclude federated sources.
- Federation topology is not mutable from GUI/chat-assistant/MCP user flows.

## Implementation checklist

- [x] JSON schema for `federation_config.json` + validation tests.
- [x] Connector interface abstraction (`GraphConnector`: file).
- [x] Cache storage design (versioning + TTL + stale markers).
- [x] Query planner merge strategy and ranking adjustments.
- [x] Provenance fields surfaced in API contracts.
- [x] UI labels/filters for provenance and hop count.
- [x] Sync scheduler with resilience primitives.
- [x] Federated subscription schema + compatibility migration.
- [ ] MCP connector with scoped credentials (Phase 4).
- [ ] Load/failure testing with multiple simulated remote graphs.



## Practical verification guide (current implementation)

Use this checklist to quickly validate the current Option C baseline in a running environment.

### 1) Configure one remote graph

Update `config/default/federation_config.json` with one enabled graph that exposes `graph_json_url`:

```json
{
  "federation": {
    "enabled": true,
    "max_traversal_depth": 1,
    "default_timeout_ms": 1200,
    "allow_live_remote_enrichment": false,
    "graphs": [
      {
        "graph_id": "esam-main",
        "display_name": "eSam",
        "enabled": true,
        "endpoints": {
          "graph_json_url": "https://example.org/graph.json"
        },
        "sync": {
          "mode": "scheduled",
          "interval_seconds": 300,
          "on_startup": true,
          "on_demand": true
        }
      }
    ]
  }
}
```

### 2) Start the service and verify health

- Check startup health includes federation summary and runtime state.
- Expected: service starts even if remote URL is unavailable.

```bash
curl -s http://localhost:8000/health | jq
```

### 3) Inspect federation runtime status

Verify graph status moves between `healthy` and `degraded` depending on connectivity:

```bash
curl -s http://localhost:8000/federation/status | jq
```

Look for:
- `scheduler_running`
- per-graph `status`
- `last_synced_at` / `last_error`
- `cached_nodes` and `cached_edges`

### 4) Trigger on-demand sync

```bash
curl -s -X POST http://localhost:8000/federation/sync | jq
```

Expected:
- `success: true` when remote source is reachable and valid
- per-graph errors but continued service operation when unavailable

### 5) Validate merged search behavior

Run a search that should match both local and federated cache data:

```bash
curl -s -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"esam","node_types":["Actor"],"limit":20}' | jq
```

Expected response characteristics:
- local + federated nodes in `nodes`
- `federation.included = true`
- `federation.federated_nodes > 0` when cache contains matches
- federated entities contain provenance metadata (`origin_graph_id`, `is_federated`, etc.)

### 6) Simulate outage behavior

Set an unreachable `graph_json_url`, restart, and verify:
- service still starts
- `/federation/status` reports `degraded` for that graph
- local search still works

### 7) Validate adoption (clone into local graph)

Use one federated node ID from search results and adopt it into local graph:

```bash
curl -s -X POST http://localhost:8000/api/federation/adopt \
  -H "Content-Type: application/json" \
  -d '{"federated_node_id":"federated::esam-main::remote-1","local_name":"Local adopted copy"}' | jq
```

To force another clone even if already adopted:

```bash
curl -s -X POST http://localhost:8000/api/federation/adopt \
  -H "Content-Type: application/json" \
  -d '{"federated_node_id":"federated::esam-main::remote-1","local_name":"Another copy","create_new_copy":true}' | jq
```

Expected:
- `success: true`
- `adopted_node.metadata.is_adopted = true`
- `adopted_node.metadata.adopted_from.origin_graph_id` set
- repeat adoption without `create_new_copy` should return `already_adopted: true`

### 8) Run automated regression checks

```bash
PYTHONPATH=backend:. python -m pytest -q \
  backend/federation/tests/test_config.py \
  backend/federation/tests/test_manager.py \
  backend/service/tests/test_federated_search.py \
  backend/service/tests/test_federated_adoption.py
```

This test set verifies:
- config load/fallback behavior
- manager sync + degraded fallback
- merged local/federated search behavior
- adoption flow for cached federated nodes


### Depth budget enforcement in current baseline

Current implementation enforces federation depth budget during federated cache search merge:
- global `federation.max_traversal_depth`
- optional per-graph `max_depth_override` (effective depth is min of global and override)

Nodes with `metadata.federation_distance` greater than allowed depth are excluded from merged federated search results.

## Runtime depth selector in GUI (user-controlled within admin limits)

A user can now choose the effective federation depth at runtime from the GUI with a vertical selector rendered next to the MiniMap (floor-selector style, levels `1..N`).

### Behavior
- Selector updates a client-side `federationDepth` state (default `1`).
- Search (`/api/search`) sends `federation_depth` and federated cache merge respects this value.
- Chat/UI assistant requests (`/ui/chat`, `/ui/chat/simple`) send `federation_depth` and tool-driven `search_graph` calls inherit that depth.
- Effective depth is always bounded by configured federation limits (`max_traversal_depth` and optional per-graph `max_depth_override`).

### Important constraints
- This does **not** change federation topology or admin policy.
- Topology remains startup-config only (`FEDERATION_FILE` / `config/default/federation_config.json`).
- Runtime depth is only a user query/view lens within configured safety bounds.


### UI/UX adjustments for depth selector and search labels
- Depth selector is now docked adjacent to MiniMap (no overlap) and only rendered when more than one depth level is selectable.
- Selectable levels are derived from installation policy (`max_traversal_depth`, optional explicit `depth_levels`, and enabled graph overrides), not hard-coded UI values.
- Search result labels can include graph prefix (`<GraphName>: <NodeName>`) when multiple graphs are available.
- When only local graph exists, search shows only node names.
- `graph.json` metadata supports `graph_name` for local graph labeling in UI.

- Optional `federation.depth_levels` can define exact selectable levels (e.g. `[1,3,5]`); levels above effective max depth are automatically excluded at runtime.
