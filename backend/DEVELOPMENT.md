# Development Guide

This document covers how to build, run, and test the Community Knowledge Graph system.

## Architecture Overview

The system is organized into several packages:

```
backend/
├── core/                # Core graph data structures and storage
├── service/             # GraphService layer, REST API, MCP tools
├── ui/                  # User chat (incl. chat_logic.py) and document analysis
├── llm/                 # LLM provider abstraction and language-policy prompts
├── runtime/             # Request/authorization/config runtime context
├── agents/              # Agent execution and event subscriptions
├── federation/          # Federated graph cache and search
├── skills/              # Skills loader system
└── api_host/            # FastAPI application server
frontend/
├── web/                 # Full web application
└── widget/              # Embeddable widget for ChatGPT etc.
packages/
└── ui-graph-canvas/     # React component for graph visualization
```

### Key Architectural Principles

**GraphService vs ui separation:**

- **GraphService** handles all graph operations (search, CRUD, statistics). It does NOT make any LLM calls.
- **ui** (ui backend) handles user-facing chat and document analysis. It uses LLM providers (OpenAI/Claude) and routes ALL graph mutations through GraphService.

**Why this separation matters:**

1. **Consistency**: All graph mutations go through GraphService, ensuring validation and proper handling.
2. **Testability**: GraphService can be tested without LLM mocking; ui can be tested with mocked LLM.
3. **Flexibility**: Different frontends (REST, MCP, chat) all use the same GraphService.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   REST API  │     │  MCP Tools  │     │     ui      │
│  /api/v1/*  │     │   /mcp/*    │     │    /ui/*    │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       │                   │            ┌──────┴──────┐
       │                   │            │ ChatService │
       │                   │            │ (LLM calls) │
       │                   │            └──────┬──────┘
       │                   │                   │
       └───────────────────┴───────────────────┘
                           │
                    ┌──────┴──────┐
                    │ GraphService │
                    │ (no LLM)     │
                    └──────┬──────┘
                           │
                    ┌──────┴──────┐
                    │ GraphStorage │
                    │   (core)     │
                    └─────────────┘
```

### Event System & Agents

The system includes an event-driven architecture for webhooks and AI agents:

- **Event System**: Tracks graph mutations (create, update, delete) and dispatches events to subscribers.
  See `docs/EVENT_SUBSCRIPTIONS.md` for details.
- **Agent System**: AI agents that react to graph events or run on a schedule using MCP tools.
  Agents are configured via `Agent` nodes in the graph.
  - **Event-triggered**: an agent links to an `EventSubscription` node that defines which graph mutations fire it.
  - **Schedule-triggered**: an agent's `metadata.schedule` field sets a recurring day + time.
  See `docs/AGENT_SCHEDULING.md` for scheduling details and GCP Cloud Scheduler integration.

### Concurrency & Persistence

The `GraphStorage` layer implements thread-safety and multi-process safety mechanisms:
- **In-memory**: Uses `threading.RLock` to protect shared state.
- **File System**: Uses `fcntl` (Unix) or `msvcrt` (Windows) for file locking.
- **Atomic Writes**: Uses temp-file-and-rename strategy to prevent corruption.

See `docs/DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md` for a deep dive.

## Prerequisites

- Python 3.11+
- Node.js 20+
- npm 9+

## Installation

### Python Dependencies

```bash
# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install Python packages (base — runs the full app, including semantic search)
pip install -r backend/requirements.txt

# Optional: ML extras for generating semantic embeddings from text
# (torch + sentence-transformers, CPU-only). Without them, embedding
# generation is skipped gracefully and name-based similarity still works.
pip install -r backend/requirements-ml.txt
```

### JavaScript Dependencies

```bash
# From project root
npm install
```

This installs dependencies for all workspaces (ui-graph-canvas, web, widget).

## Running the Server

### Development Mode

```bash
uvicorn backend.api_host.server:get_app --factory --reload --port 8000
```

The server will be available at:
- REST API: http://localhost:8000/api/
- UI Backend (chat): http://localhost:8000/ui/
- MCP endpoint: http://localhost:8000/mcp  (also `/mcp/sse` for legacy SSE clients)
- Health check: http://localhost:8000/health

### Production Mode

```bash
uvicorn backend.api_host.server:get_app --factory --host 0.0.0.0 --port 8000
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPH_FILE` | `data/active/graph.json` | Path to graph data file |
| `API_PREFIX` | `/api` | REST API URL prefix |
| `MCP_NAME` | `community-graph` | MCP server name |
| `OPENAI_API_KEY` | - | OpenAI API key (for chat) |
| `ANTHROPIC_API_KEY` | - | Anthropic API key (for chat) |
| `LLM_PROVIDER` | auto-detect | Force LLM provider: `openai` or `claude` |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model to use |
| `AGENTS_ENABLED` | `false` | Enable the AI agent system |
| `AGENTS_LLM_PROVIDER` | - | LLM provider for agents (`openai` or `claude`) |
| `AGENTS_SCHEDULER_ENABLED` | `false` | Enable in-process time-based scheduler (off for scale-to-zero) |

## Building Frontend

### UI Graph Canvas Component

```bash
cd packages/ui-graph-canvas
npm run build
```

### Web Application

```bash
cd frontend/web
npm run build
```

Built files output to `dist/`.

### Widget

```bash
cd frontend/widget
npm run build
```

## Testing

### Python Tests

#### Unit Tests

```bash
# Run all Python tests
python -m pytest backend

# Run with coverage
pytest --cov=backend.core --cov=backend.service --cov=backend.api_host backend

# Run specific test file
pytest backend/service/tests/test_integration_rest_vs_mcp.py

# Run with verbose output
pytest -v backend
```

#### Integration Tests (REST vs MCP)

These tests verify that REST API and MCP tools produce identical results:

```bash
pytest backend/service/tests/test_integration_rest_vs_mcp.py -v
```

#### UI Backend Tests

These tests verify chat and document upload functionality:

```bash
# Run ui unit tests
pytest backend/ui/tests/ -v

# Run ui integration tests (with full app stack)
pytest backend/api_host/tests/test_ui_backend_integration.py -v
```

The ui tests use mocked LLM providers to verify:
- Tool calls are routed through GraphService
- Graph mutations persist correctly
- Document upload and extraction work

### JavaScript Tests

#### UI Graph Canvas Tests

```bash
cd packages/ui-graph-canvas
npm test
```

#### Widget Tests

```bash
cd frontend/widget
npm test
```

Tests include:
- `mcpClient.test.js` - MCP client module tests
- `Widget.test.jsx` - Widget component tests with mocked MCP

#### Web App Tests

```bash
cd frontend/web
npm test
```

Tests include:
- `ChatPanel.test.jsx` - ChatPanel component tests with mocked API

#### Playwright E2E Tests (Web App)

```bash
cd frontend/web

# Install Playwright browsers (first time)
npx playwright install chromium

# Run e2e tests
npm run test:e2e
```

E2E tests include:
- `chat.spec.js` - Complete chat workflow testing

### E2E Tests with Live Backend

These tests run against a live server using real HTTP requests:

```bash
# Option 1: Script starts server, runs tests, stops server
./scripts/test-e2e.sh

# Option 2: Run against an already-running server
./scripts/test-e2e.sh --no-server

# Option 3: Run against custom server URL
E2E_SERVER_URL=http://localhost:8080 ./scripts/test-e2e.sh --no-server
```

### Running All Tests

```bash
# Python tests
python -m pytest backend

# JavaScript tests (all packages)
npm test

# E2E tests
./scripts/test-e2e.sh
```

### Continuous Integration

`.github/workflows/ci.yml` runs three test jobs on every push and pull request,
kept separate so a failure points at the layer that broke:

- **Backend tests** — `pytest backend/ -q` on the base (ML-free) requirements.
  Semantic search and chat use their mock/fallback paths, so no embedding model
  is downloaded in CI.
- **Frontend tests** — `npm run test:unit` across the web, widget, and canvas
  workspaces. Playwright e2e is intentionally excluded from the required path.
  Dependencies install with `npm ci` against the tracked root `package-lock.json`
  so the workspace tree is reproducible run-to-run (cached via `setup-node`).
- **Gateway tests** — the MCP OAuth gateway suite, run in isolation with its own
  pinned dependencies.

The Docker build/publish job runs only on `preview`/`prod` pushes (and version
tags) and depends on all three test jobs. `main` is the integration branch:
pushes to it run the tests but publish no image.

## API Reference

### REST API Endpoints

The default API prefix is `/api` (configurable via `API_PREFIX`).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/search` | Search nodes |
| GET | `/api/nodes/{id}` | Get node details |
| POST | `/api/nodes/{id}/related` | Get related nodes |
| POST | `/api/nodes` | Add nodes and edges |
| PATCH | `/api/nodes/{id}` | Update a node |
| DELETE | `/api/nodes` | Delete nodes |
| POST | `/api/edges` | Add edges |
| PATCH | `/api/edges/{id}` | Update an edge |
| DELETE | `/api/edges/{id}` | Delete an edge |
| GET | `/api/history` | Recent graph mutation history (newest first; `limit`, `offset`) |
| GET | `/api/nodes/{id}/history` | Mutation history for a single node |
| GET | `/api/edges/{id}/history` | Mutation history for a single edge |
| GET | `/api/stats` | Get graph statistics |
| POST | `/api/similar` | Find similar nodes |
| POST | `/api/similar/batch` | Batch similarity search |
| POST | `/api/federation/adopt` | Adopt a federated node into the local graph |
| GET | `/api/schema` | Get schema config |
| GET | `/api/presentation` | Get presentation config |
| GET | `/api/capabilities` | Get service capabilities |
| GET | `/api/export` | Export graph data |
| GET | `/api/{custom-path}` | Config-driven dedicated interface for one node/edge type (see Custom REST Interfaces below). Registered only for configured types. |
| POST | `/api/views/save` | Save a named graph view |
| GET | `/api/views/{name}` | Get a saved view |
| GET | `/api/views` | List saved views |
| GET | `/agents/schedules` | List all agent schedules (for external scheduler reconciliation) |
| GET | `/agents/runs` | List durable AgentRun history (filter by `agent_id`, `kind`, `status`, `limit`), newest-first |
| GET | `/agents/runs/{run_id}` | Get a single AgentRun by id |
| GET | `/agents/proposals` | List agent proposals (filter by `agent_id`, `status`, `limit`), newest-first |
| GET | `/agents/proposals/{proposal_id}` | Get a single proposal by id |
| POST | `/agents/proposals/{proposal_id}/approve` | Approve a proposal (applies the action for act_after_approval agents) |
| POST | `/agents/proposals/{proposal_id}/reject` | Reject a proposal |
| POST | `/agents/{id}/trigger` | Fire a scheduled agent immediately (used by GCP Cloud Scheduler) |

### Custom REST Interfaces (config-driven)

A specific node type or edge type can be exposed as its own dedicated read-only
`GET` endpoint that bypasses the generic node/edge interface and returns only
entities of that type, optionally narrowed by tag/subtype filters. This is
driven entirely from the open-core schema config file — the `rest_interfaces`
top-level array in `schema_config.json`. It is empty by default (no dedicated
endpoints; only the generic interface is exposed), so this is a purely additive,
backward-compatible config surface — it does **not** change node/relationship
types and is not a schema breaking-change.

Each entry:

| Field | Default | Meaning |
|-------|---------|---------|
| `path` | — (required) | URL segment appended to the API prefix, e.g. `actors` → `GET /api/actors`. Lowercase alphanumeric with `-`, `_`, `/` separators. |
| `entity` | `node` | `node` or `edge`. |
| `node_type` | `""` | Node type to expose (required when `entity` is `node`). |
| `edge_type` | `""` | Edge/relationship type to expose (required when `entity` is `edge`). |
| `enabled` | `true` | Set `false` to keep the config but not register the route. |
| `limit` | `500` | Max entities returned (1–5000). |
| `filters.tags_all` | `[]` | AND filter — the entity must carry **every** listed tag. |
| `filters.tags_any` | `[]` | OR filter — the entity must carry **at least one** listed tag. |
| `filters.subtypes_any` | `[]` | OR filter on node subtypes (ignored for edges). |

`tags_all` and `tags_any` combine with AND (an entity must pass both). Edges have
no `tags` field, so edge tag filters match against `edge.metadata["tags"]` (a
list, when present).

`node_type` / `edge_type` are matched by exact canonical type name (case
sensitive, no alias resolution). A malformed entry (bad `path`/`entity`, or a
`node`/`edge` entry missing its `node_type`/`edge_type`) is skipped with a logged
warning; it never disables the rest of the config or the other interfaces.

Example — expose `Actor` nodes at `/api/actors`, returning only actors tagged
`approved` **or** `processing`:

```json
{
  "schema": { "...": "..." },
  "rest_interfaces": [
    {
      "path": "actors",
      "entity": "node",
      "node_type": "Actor",
      "filters": { "tags_any": ["approved", "processing"] }
    }
  ]
}
```

The response mirrors the generic search shape (`nodes`, `edges`, `total`) — for
node interfaces, edges connecting two returned nodes are included; for edge
interfaces, the endpoint returns `edges` plus their endpoint `nodes`.

**Access parity:** dedicated interfaces apply the same read authorization and
graph-scope narrowing as the generic interface (`GRAPH_ACTION_READ`), so a
dedicated endpoint never returns more than a generic search under the same
request scope. Edges are returned only when both endpoint nodes are visible.

The SaaS/hosted layer can drive this same mechanism from a user-defined GUI
config; the open-core core reads only the config file.

### Shared Session Endpoints

Server-side multi-user sessions (see `docs/MULTI_USER_SESSIONS_DESIGN.md`).
Sessions are stored outside the graph as node references + layout + annotations;
node content is rehydrated from the graph on load via `?resolve=true`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/sessions` | Create a new shared session (server-assigned `DDDD-DDDD-DDDD-DDDD` id) |
| GET | `/api/sessions` | List session metadata |
| GET | `/api/sessions/{id}` | Get a session (meta + state + presence roster); `?resolve=true` also returns rehydrated nodes/edges |
| PATCH | `/api/sessions/{id}` | Rename a session (`{name, client_id?}`). get-or-create: materialises the session server-side if it only existed client-side. Routed through the op protocol as a `session_renamed` state op, so the rename is sequenced and visible to `since_seq` catch-up, not just a full snapshot — design §8.2 R7/R8 |
| DELETE | `/api/sessions/{id}` | Delete a session (`?client_id=` names the deleter in the broadcast) |
| POST | `/api/sessions/{id}/ops` | Apply an ordered op batch (`{client_id, base_seq, ops}` → `{applied, seq}`); server-ordered LWW, monotonic `seq`. Bounded per batch by op count (≤ 500) **and** body size (≤ 256 KB → `413`), plus a per-client token bucket (200 burst, 100 ops/s refill → `429`) — design §3.9 |
| GET | `/api/sessions/{id}/stream` | SSE fan-out: presence, applied ops, claims, and broadcast MCP commands (`{"type": "command", ...}` — every connected client applies these, not just one browser). Query `client_id`, `name`, `since_seq` (op catch-up or full-snapshot fallback). A slow consumer whose queue overflows is sent a fresh full snapshot rather than diverging. EventSource-opened, so it bypasses Basic Auth (protected by the unguessable session id — design §3.9) |

Session state is server-owned: the browser no longer uploads canvas state, and
MCP query tools read visible nodes / selection from the shared-session store
(the step-4 `PUT /api/sessions/{id}/state` full-state save and the legacy
`PATCH /sessions/{id}/state` upload were removed in step 8 — design §3.8).

### Authentication and the unauthenticated read surface

Authentication is opt-in. It is active only when a password (`AUTH_PASSWORD`) or
bearer token (`AUTH_BEARER_TOKEN`) is configured together with an activation flag
(`AUTH_ENABLED` or `MCP_BASIC_AUTH`). See `backend/api_host/middleware.py`.

When auth is **active**, the middleware guards all endpoints except the public
health/readiness/info routes and the id-protected session SSE streams.

When auth is **not** active (the default standalone posture), the instance is an
open read/write surface by design:

- The REST graph endpoints, `/execute_tool` (limited to the read-only
  `SAFE_TOOLS` allow-list in `backend/api_host/tool_routes.py`), **and the full
  graph export** (`GET /api/export` and `GET /export_graph`) are all reachable
  without credentials.
- A full graph export is therefore an **unauthenticated read on an
  auth-disabled instance**, consistent with the other `SAFE_TOOLS` reads. This is
  intentional for open/standalone deployments. If a deployment must keep graph
  contents private, enable authentication — do not rely on export being
  privileged while other reads are open.

Legacy MCP visualization-push channel (single-consumer; delivers AI-pushed
visualization commands to the browser). The browser opens this only until the
op-protocol stream above has connected for the session, since that stream's
broadcast `command` events reach every collaborator instead of just one:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/sessions/{id}/stream` | SSE stream delivering MCP visualization commands to the browser. A connected stream signals that a browser is present to receive pushes |
| POST | `/sessions/{id}/trigger-token` | Mint (or rotate) the session's pulse-trigger token, returning `{session_id, trigger_token, pulse_path}`. Called by the session's own browser; runs under the graph authorization seam (permissive in open core, hosted-gatable). Re-minting revokes the prior token |
| POST | `/sessions/{id}/pulse` | External trigger: play a visual pulse on a node in the live session. Body `{node_id, style?, color?, duration_ms?}`; authenticated with the trigger token via `Authorization: Bearer` or `?token=`. Emits a `node_pulse` command over the SSE session-push channels (best-effort dispatch — `success` means dispatched, not that a browser was watching). `401` without a valid token, `429` when the per-source lookup bucket is exhausted, `422` for a malformed body |

External systems (e.g. a customer-registration webhook) call the pulse endpoint
to draw a user's attention to a node; the trigger token is a capability-scoped,
in-memory, per-session secret that dies with the session.

### MCP Tools

| Tool Name | Description |
|-----------|-------------|
| `search_graph` | Search nodes by query |
| `get_node_details` | Get full details for a node |
| `get_related_nodes` | Get nodes connected to a node |
| `find_similar_nodes` | Find nodes with similar names |
| `add_nodes` | Add new nodes and edges |
| `update_node` | Update node properties |
| `delete_nodes` | Delete nodes by ID |
| `get_graph_stats` | Get graph statistics |
| `save_view` | Save a named view (creates SavedView node) |
| `get_visualization_layout` | Read every node's model-space position in an open session (for an agent to compute a new arrangement) |
| `apply_visualization_layout` | Move nodes in an open session by absolute positions or deltas; applied atomically, animated on the canvas, and mirrored live to all connected browsers |
| `create_visualization_session` | Create a new empty session (optional non-unique name; server assigns a default when omitted) |
| `list_visualization_sessions` | List existing sessions, most recently updated first |
| `get_visualization_session` | Inspect one session's resource metadata (incl. node count) |
| `rename_visualization_session` | Set or clear a session's display name |
| `delete_visualization_session` | Permanently delete a session — requires `confirm=true` |

`get_visualization_layout` / `apply_visualization_layout` operate on a shared
visualization session (the `SessionManager` op protocol), so an AI agent
rearranging the canvas is just another collaborator. Coordinates are model space
(zoom/pan independent, pixels at zoom 1, `x`/`y` = node top-left). `apply_*`
carries the move as one `layout_applied` op with a monotonic `revision`;
pass the `revision` from a prior read as `expected_revision` for optimistic
concurrency. Node width/height are not server-owned, so the read tool advertises
an `assumed_node_size` for collision-free spacing instead. A write carries an
animation hint (`animate`/`duration_ms`/`easing`); the canvas tweens the batch
from the nodes' current positions to the targets, and a viewer who asked for
reduced motion snaps to the final positions instead (a client-side decision — an
agent just sends the hint it intends). The full geometry and movement semantics —
coordinate model, absolute vs. delta moves, atomic batching and caps, the
animation seam and the `layout_applied` broadcast shape — are the versioned
contract in
[`docs/MCP_VISUALIZATION_LAYOUT_CONTRACT.md`](../docs/MCP_VISUALIZATION_LAYOUT_CONTRACT.md).

#### Arranging a session (agent recipes)

Coordinates are model space with `x`/`y` at the node **top-left**, so spacing is
computed from `assumed_node_size` (`{width, height}` from the read tool) plus a
gap — offset by the full node size, not half, to leave a visible gutter. Read the
layout first to get `assumed_node_size` and the current `revision`, then pass that
`revision` as `expected_revision` on the write. A single write is capped at 500
moves / 256 KiB (`too_large` beyond that) and additionally draws from a per-client
rate budget sized to the number of moves, so a very large arrange can hit
`rate_limited` first — either way, split it across successive writes and thread the
returned `revision` into the next `expected_revision`.

- **Horizontal (left-to-right) DAG.** Rank each node by its longest path from a
  root; `x = rank * (width + gap)` so every edge points rightward, and stack nodes
  sharing a rank down the column: `y = slot_in_rank * (height + gap)`.

  ```python
  layout = get_visualization_layout(session_id=sid)
  w = layout["assumed_node_size"]["width"]; h = layout["assumed_node_size"]["height"]
  gap = 60
  positions = {
      node_id: {"x": rank[node_id] * (w + gap), "y": slot[node_id] * (h + gap)}
      for node_id in ranks
  }
  apply_visualization_layout(session_id=sid, positions=positions,
                             expected_revision=layout["revision"])
  ```

- **Grid.** Place N nodes in a `cols`-wide grid:
  `x = (i % cols) * (width + gap)`, `y = (i // cols) * (height + gap)`.

- **Swimlanes.** Give each lane (e.g. a node type or status) a fixed `y` band and
  lay its members out along `x`: `y = lane_index * (height + lane_gap)`,
  `x = position_in_lane * (width + gap)`. Lanes are pure geometry here — the
  contract moves individual node positions and does not group them (§8).

- **Create a named, shareable session from scratch** — never assume a hostname:

  ```python
  s = create_visualization_session(name="Q3 dependency map")
  sid = s["session"]["session_id"]
  # add nodes via search_graph/get_related_nodes with visualization_session_id=sid,
  # then arrange with apply_visualization_layout as above, then:
  link = s["session"]["session_url"]   # server-owned canonical link, or null
  ```

  Hand `session_url` to the user verbatim; it is `null` only when the deployment
  has no public base URL configured.

The `*_visualization_session` CRUD tools manage session *resources* (as opposed
to inspecting/laying out an already-open one). They implement the versioned
contract in [`docs/MCP_SESSION_LIFECYCLE_CONTRACT.md`](../docs/MCP_SESSION_LIFECYCLE_CONTRACT.md):
every call is gated by the service authorization hook (permissive/anonymous by
default in the open core; the hosted layer swaps the hook in to enforce
tenancy), names are non-unique with a server default, rename is op-routed (it
reaches reconnecting clients via catch-up), and deletion is a confirmed hard
delete that notifies connected browsers. Each tool returns a session-resource
projection (`session_id`, `name`, `lifecycle_state`, timestamps, `revision`,
`capabilities`, `session_url`). `session_url` is the server-built canonical
`?session=<id>` link (from `COMMUNITYOVERVIEW_PUBLIC_BASE_URL`; `null` when
unconfigured) so an assistant can hand the user a direct link without guessing a
host. `owner`/`workspace` remain reserved and are populated by the hosted layer.

### UI Backend Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/ui/chat` | Process chat with conversation history |
| POST | `/ui/chat/simple` | Simple chat with single message |
| POST | `/ui/propose-nodes` | Extract entities from text (returns proposals) |
| POST | `/ui/upload` | Upload and analyze document |
| POST | `/ui/upload/extract` | Extract text only (no LLM analysis) |
| GET | `/ui/info` | Get service info (provider, tools) |
| GET | `/ui/supported-formats` | Get supported document formats |

#### Chat Example

```bash
curl -X POST http://localhost:8000/ui/chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Search for AI projects"}]
  }'
```

#### Propose Nodes Example

```bash
curl -X POST http://localhost:8000/ui/propose-nodes \
  -H "Content-Type: application/json" \
  -d '{
    "text": "The Ministry of Innovation launched Digital Strategy 2030.",
    "communities": ["Government"]
  }'
```

Response includes proposed nodes and similar existing nodes for confirmation.

#### Document Upload Example

```bash
curl -X POST http://localhost:8000/ui/upload \
  -F "file=@document.pdf" \
  -F "message=What is this document about?" \
  -F "analyze=true"
```

### Direct Tool Execution

For direct tool execution without MCP protocol:

```bash
curl -X POST http://localhost:8000/execute_tool \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "search_graph", "arguments": {"query": "test", "limit": 10}}'
```

## Using the Chat Panel

The web application includes a ChatPanel for conversational interaction with the graph.

### Features

- **Search and Query**: Ask questions about the graph ("Find AI projects", "Show me actors in eSam")
- **Add Nodes**: Request node creation ("Add a new initiative about digital identity")
- **Node Proposals**: The LLM proposes nodes with similar node detection, user must confirm before adding
- **Delete Confirmation**: Destructive actions require explicit user confirmation
- **Document Upload**: Upload PDF, Word, or text files for analysis and entity extraction

### Opening the Chat Panel

Click the "Chat" button in the application header to open the panel. The panel displays:
- Welcome message with example queries
- Conversation history
- Loading indicators during processing
- Error messages when something goes wrong

### ChatGPT Widget Integration

The same chat functionality is available via the embeddable widget (`frontend/widget/`). This widget can be embedded in ChatGPT or other interfaces that support custom widgets.

The widget uses the same `/ui/chat` endpoint, ensuring consistent behavior between the web app and external integrations.

### Active Knowledge Collection kiosk

When `/ui/chat` receives a `collection_short_name`, the assistant runs in
collection (kiosk) mode. The matching `ActiveKnowledgeCollection` node's
`metadata` drives the session:

| metadata field | Purpose |
|---|---|
| `short_name` | URL identifier used to resolve the collection |
| `introduction_text` | Public text shown before the chat starts |
| `prompt` | Server-side AI instructions (never exposed to the client) |
| `node_type_permissions` | Per-node-type `{create, update, delete}` flags, enforced server-side on graph mutations |
| `tool_allowlist` | Optional list of tool names the assistant may use |
| `collect_responses` | When `false`, `save_collection_response` is not installed |

`tool_allowlist` mirrors the AIAgent tool-permission model
(`backend/agents/governance/gate.py`): unset or empty means unrestricted (all
tools), while a non-empty list restricts the assistant to exactly those tools.
Enforcement is server-side and two-layered — disallowed tools are neither
advertised to the LLM nor executed if requested anyway
(`backend/ui/chat_logic.py`). The tool names match
`ChatProcessor._generate_tool_definitions` (e.g. `search_graph`, `add_nodes`,
`present_form`, `save_collection_response`).

## Development Workflow

### Adding a New MCP Tool

1. Add the function to `backend/service/service.py`
2. Register the tool in `backend/service/mcp_tools.py`
3. Add corresponding REST endpoint in `backend/service/rest_api.py` (if needed)
4. Add integration tests in `backend/service/tests/test_integration_rest_vs_mcp.py`

### Adding Frontend Features

1. Make changes in `packages/ui-graph-canvas/src/`
2. Run tests: `npm test`
3. Build: `npm run build`
4. Test in frontend/widget or frontend/web

## Troubleshooting

### Tests Hang on First Run

The embedding model downloads from HuggingFace on first use. If this fails (e.g., network restrictions), tests use a mock embedding model automatically.

### Module Not Found Errors

Ensure you've installed packages in development mode:

```bash
pip install -r backend/requirements.txt
npm install
```

### Port Already in Use

Kill existing server or use a different port:

```bash
uvicorn backend.api_host.server:get_app --factory --port 8001
```
