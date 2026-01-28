# Development Guide

This document covers how to build, run, and test the Community Knowledge Graph system.

## Architecture Overview

The system is organized into several packages:

```
mcp-server/
├── graph_core/          # Core graph data structures and storage
├── graph_services/      # GraphService layer, REST API, MCP tools
├── ui_backend/          # User chat and document analysis (LLM integration)
├── app_host/            # FastAPI application server
├── packages/
│   └── ui-graph-canvas/ # React component for graph visualization
└── apps/
    ├── web/             # Full web application
    └── widget/          # Embeddable widget for ChatGPT etc.
```

### Key Architectural Principles

**GraphService vs ui_backend separation:**

- **GraphService** handles all graph operations (search, CRUD, statistics). It does NOT make any LLM calls.
- **ui_backend** handles user-facing chat and document analysis. It uses LLM providers (OpenAI/Claude) and routes ALL graph mutations through GraphService.

**Why this separation matters:**

1. **Consistency**: All graph mutations go through GraphService, ensuring validation and proper handling.
2. **Testability**: GraphService can be tested without LLM mocking; ui_backend can be tested with mocked LLM.
3. **Flexibility**: Different frontends (REST, MCP, chat) all use the same GraphService.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   REST API  │     │  MCP Tools  │     │  ui_backend │
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
                    │ (graph_core) │
                    └─────────────┘
```

## Prerequisites

- Python 3.11+
- Node.js 18+
- npm 9+

## Installation

### Python Dependencies

```bash
cd mcp-server

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install Python packages
pip install -e .
# or for development:
pip install -e ".[dev]"
```

### JavaScript Dependencies

```bash
# From mcp-server directory
npm install
```

This installs dependencies for all workspaces (ui-graph-canvas, web, widget).

## Running the Server

### Development Mode

```bash
cd mcp-server
uvicorn app_host.server:get_app --factory --reload --port 8000
```

The server will be available at:
- REST API: http://localhost:8000/api/v1/
- UI Backend (chat): http://localhost:8000/ui/
- MCP endpoint: http://localhost:8000/mcp
- Health check: http://localhost:8000/health

### Production Mode

```bash
uvicorn app_host.server:get_app --factory --host 0.0.0.0 --port 8000
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPH_FILE` | `graph.json` | Path to graph data file |
| `API_PREFIX` | `/api/v1` | REST API URL prefix |
| `MCP_NAME` | `community-graph` | MCP server name |
| `OPENAI_API_KEY` | - | OpenAI API key (for chat) |
| `ANTHROPIC_API_KEY` | - | Anthropic API key (for chat) |
| `LLM_PROVIDER` | auto-detect | Force LLM provider: `openai` or `claude` |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model to use |

## Building Frontend

### UI Graph Canvas Component

```bash
cd mcp-server/packages/ui-graph-canvas
npm run build
```

### Web Application

```bash
cd mcp-server/apps/web
npm run build
```

Built files output to `dist/`.

### Widget

```bash
cd mcp-server/apps/widget
npm run build
```

## Testing

### Python Tests

#### Unit Tests

```bash
cd mcp-server

# Run all Python tests
pytest

# Run with coverage
pytest --cov=graph_core --cov=graph_services --cov=app_host

# Run specific test file
pytest graph_services/tests/test_integration_rest_vs_mcp.py

# Run with verbose output
pytest -v
```

#### Integration Tests (REST vs MCP)

These tests verify that REST API and MCP tools produce identical results:

```bash
pytest graph_services/tests/test_integration_rest_vs_mcp.py -v
```

#### UI Backend Tests

These tests verify chat and document upload functionality:

```bash
# Run ui_backend unit tests
pytest ui_backend/tests/ -v

# Run ui_backend integration tests (with full app stack)
pytest app_host/tests/test_ui_backend_integration.py -v
```

The ui_backend tests use mocked LLM providers to verify:
- Tool calls are routed through GraphService
- Graph mutations persist correctly
- Document upload and extraction work

### JavaScript Tests

#### UI Graph Canvas Tests

```bash
cd mcp-server/packages/ui-graph-canvas
npm test
```

#### Widget Tests

```bash
cd mcp-server/apps/widget
npm test
```

Tests include:
- `mcpClient.test.js` - MCP client module tests
- `Widget.test.jsx` - Widget component tests with mocked MCP

### E2E Tests with Live Backend

These tests run against a live server using real HTTP requests:

```bash
cd mcp-server

# Option 1: Script starts server, runs tests, stops server
./scripts/test-e2e.sh

# Option 2: Run against an already-running server
./scripts/test-e2e.sh --no-server

# Option 3: Run against custom server URL
E2E_SERVER_URL=http://localhost:8080 ./scripts/test-e2e.sh --no-server
```

### Running All Tests

```bash
cd mcp-server

# Python tests
pytest

# JavaScript tests (all packages)
npm test

# E2E tests
./scripts/test-e2e.sh
```

## API Reference

### REST API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/search` | Search nodes |
| GET | `/api/v1/node/{id}` | Get node details |
| GET | `/api/v1/node/{id}/related` | Get related nodes |
| POST | `/api/v1/nodes` | Add nodes and edges |
| PATCH | `/api/v1/node/{id}` | Update a node |
| DELETE | `/api/v1/nodes` | Delete nodes |
| GET | `/api/v1/stats` | Get graph statistics |
| GET | `/api/v1/similar` | Find similar nodes |

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

## Development Workflow

### Adding a New MCP Tool

1. Add the function to `graph_services/graph_service.py`
2. Register the tool in `graph_services/mcp_tools.py`
3. Add corresponding REST endpoint in `graph_services/rest_api.py` (if needed)
4. Add integration tests in `graph_services/tests/test_integration_rest_vs_mcp.py`

### Adding Frontend Features

1. Make changes in `packages/ui-graph-canvas/src/`
2. Run tests: `npm test`
3. Build: `npm run build`
4. Test in apps/widget or apps/web

## Troubleshooting

### Tests Hang on First Run

The embedding model downloads from HuggingFace on first use. If this fails (e.g., network restrictions), tests use a mock embedding model automatically.

### Module Not Found Errors

Ensure you've installed packages in development mode:

```bash
pip install -e .
npm install
```

### Port Already in Use

Kill existing server or use a different port:

```bash
uvicorn app_host.server:get_app --factory --port 8001
```
