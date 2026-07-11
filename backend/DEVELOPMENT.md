# Development Guide

This document covers how to build, run, and test the Community Knowledge Graph system.

## Architecture Overview

The system is organized into several packages:

```
backend/
├── core/                # Core graph data structures and storage
├── service/             # GraphService layer, REST API, MCP tools
├── ui/                  # User chat and document analysis (LLM integration)
├── api_host/            # FastAPI application server
├── chat_logic.py        # Chat processing logic
├── llm_providers.py     # LLM provider abstraction
└── graph.json           # Graph data (auto-created)
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
- Node.js 18+
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
- **Gateway tests** — the MCP OAuth gateway suite, run in isolation with its own
  pinned dependencies.

The Docker build/publish job runs only on `preview`/`main` pushes and depends on
all three test jobs.

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
| POST | `/api/views/save` | Save a named graph view |
| GET | `/api/views/{name}` | Get a saved view |
| GET | `/api/views` | List saved views |
| GET | `/agents/schedules` | List all agent schedules (for external scheduler reconciliation) |
| POST | `/agents/{id}/trigger` | Fire a scheduled agent immediately (used by GCP Cloud Scheduler) |

### Shared Session Endpoints

Server-side multi-user sessions (see `docs/MULTI_USER_SESSIONS_DESIGN.md`).
Sessions are stored outside the graph as node references + layout + annotations;
node content is rehydrated from the graph on load via `?resolve=true`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/sessions` | Create a new shared session (server-assigned `DDDD-DDDD` id) |
| GET | `/api/sessions` | List session metadata |
| GET | `/api/sessions/{id}` | Get a session (meta + state + presence roster); `?resolve=true` also returns rehydrated nodes/edges |
| PATCH | `/api/sessions/{id}` | Rename a session |
| DELETE | `/api/sessions/{id}` | Delete a session (`?client_id=` names the deleter in the broadcast) |
| POST | `/api/sessions/{id}/ops` | Apply an ordered op batch (`{client_id, base_seq, ops}` → `{applied, seq}`); server-ordered LWW, monotonic `seq`. Bounded per batch by op count (≤ 500) **and** body size (≤ 256 KB → `413`), plus a per-client token bucket (200 burst, 100 ops/s refill → `429`) — design §3.9 |
| GET | `/api/sessions/{id}/stream` | SSE fan-out: presence, applied ops, claims, and broadcast MCP commands (`{"type": "command", ...}` — every connected client applies these, not just one browser). Query `client_id`, `name`, `since_seq` (op catch-up or full-snapshot fallback). A slow consumer whose queue overflows is sent a fresh full snapshot rather than diverging. EventSource-opened, so it bypasses Basic Auth (protected by the unguessable session id — design §3.9) |

Session state is server-owned: the browser no longer uploads canvas state, and
MCP query tools read visible nodes / selection from the shared-session store
(the step-4 `PUT /api/sessions/{id}/state` full-state save and the legacy
`PATCH /sessions/{id}/state` upload were removed in step 8 — design §3.8).

Legacy MCP visualization-push channel (single-consumer; delivers AI-pushed
visualization commands to the browser). The browser opens this only until the
op-protocol stream above has connected for the session, since that stream's
broadcast `command` events reach every collaborator instead of just one:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/sessions/{id}/stream` | SSE stream delivering MCP visualization commands to the browser. A connected stream signals that a browser is present to receive pushes |

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
