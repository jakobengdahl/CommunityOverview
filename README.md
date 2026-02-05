# Community Knowledge Sharing

AI-powered knowledge sharing for communities with graph visualization, conversational chat, and intelligent document analysis.

## Overview

This system helps organizations avoid overlapping investments by making visible:
- Ongoing initiatives and projects
- Resources and capabilities
- Connections between actors, legislation, and themes

**Key Features:**
- **AI-Powered Chat:** Natural language interface with Claude or OpenAI for exploring and managing the knowledge graph
- **Multi-Provider Support:** Switch between Claude (Anthropic) and OpenAI backends
- **Document Upload:** Upload PDF, Word, or text documents for automatic entity extraction
- **Interactive Visualization:** React Flow graph with drag-and-drop, zoom, and pan
- **Node Proposals:** LLM suggests entities with duplicate detection, user confirms before adding
- **ChatGPT Widget:** Embeddable widget for use in ChatGPT or other interfaces
- **Save Views:** Create and share custom graph views

**Tech Stack:**
- **Frontend:** React + React Flow + Zustand (monorepo with npm workspaces)
- **Backend:** FastAPI + FastMCP (Python) with NetworkX + JSON
- **AI:** Claude or OpenAI for natural language understanding and entity extraction
- **Graph storage:** NetworkX in-memory + JSON persistence
- **Similarity search:** sentence-transformers + Levenshtein distance

## Project Structure

```
/backend                          # Python backend directory
  /api_host                       # FastAPI server host
    server.py                     # Main server with REST, MCP, and static files
    config.py                     # Server configuration
  /core                           # Core graph data structures
    storage.py                    # NetworkX graph operations
    models.py                     # Node/Edge data models
    vector_store.py               # Similarity search
  /service                        # Graph service layer
    service.py                    # High-level graph operations
    rest_api.py                   # REST API router
    mcp_tools.py                  # MCP tool definitions
  /ui                             # Chat and document handling
    chat_service.py               # LLM chat with tool execution
    document_service.py           # Document parsing
    rest_api.py                   # Chat REST endpoints
  graph.json                      # Graph data (auto-created)
  llm_providers.py                # LLM provider abstraction
  chat_logic.py                   # Chat processing logic
/frontend                         # Frontend applications
  /web                            # React web application
    /src/components               # UI components (ChatPanel, etc.)
    /src/services                 # API client
    /src/store                    # Zustand state
    /tests                        # Unit and e2e tests
  /widget                         # ChatGPT embeddable widget
/packages                         # Shared packages
  /ui-graph-canvas                # Shared React Flow component
/scripts                          # Utility scripts
start-dev.sh                      # Development startup script
LLM_PROVIDERS.md                  # LLM configuration guide
```

## Metamodel

### Node Types
- **Actor** (blue) - Government agencies, organizations
- **Community** (purple) - eSam, Myndigheter, Officiell Statistik
- **Initiative** (green) - Projects, collaborative activities
- **Capability** (orange) - Procurement, IT development
- **Resource** (yellow) - Reports, software components
- **Legislation** (red) - NIS2, GDPR, etc.
- **Theme** (teal) - AI, data strategies
- **VisualizationView** (gray) - Predefined views
- **EventSubscription** (violet) - Webhook subscriptions for graph mutation events
- **Agent** (pink) - AI agent configurations (runtime not implemented)

### Relationships
- BELONGS_TO, IMPLEMENTS, PRODUCES, GOVERNED_BY, RELATES_TO, PART_OF

## Quick Start

### Development Mode (Recommended)

Start all services with a single command:

```bash
# Set your API key (pick one)
export OPENAI_API_KEY=sk-xxxxx        # For OpenAI
export ANTHROPIC_API_KEY=sk-ant-xxxxx # For Claude

# Start everything
./start-dev.sh
```

The script will:
- Set up Python virtual environment and install dependencies
- Install npm dependencies (workspaces)
- Build web app and widget
- Start FastAPI server on http://localhost:8000

**Available endpoints after startup:**
| Endpoint | Description |
|----------|-------------|
| http://localhost:8000/web/ | Web application |
| http://localhost:8000/widget/ | ChatGPT widget |
| http://localhost:8000/api/ | REST API |
| http://localhost:8000/ui/ | Chat API |
| http://localhost:8000/mcp | MCP endpoint |
| http://localhost:8000/health | Health check |

### Manual Start

If you prefer to start services separately:

**Backend:**
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r backend/requirements.txt
uvicorn backend.api_host.server:get_app --factory --reload --port 8000
```

**Frontend (development with hot reload):**
```bash
npm install
npm run dev  # Starts Vite dev server on http://localhost:5173
```

Note: In development mode, the frontend runs on port 5173 with hot reload. For production, run `npm run build` and access via `/web/` on the backend server.

## LLM Provider Configuration

The system automatically detects which provider to use based on available API keys:

```bash
# Just set your API key - provider is auto-detected
export OPENAI_API_KEY=sk-xxxxx           # Auto-selects OpenAI
# OR
export ANTHROPIC_API_KEY=sk-ant-xxxxx    # Auto-selects Claude
```

**Manual selection:**
```bash
export LLM_PROVIDER=claude   # Force Claude
export LLM_PROVIDER=openai   # Force OpenAI
```

See [LLM_PROVIDERS.md](./LLM_PROVIDERS.md) for detailed configuration.

## Testing

```bash
# All Python tests
python -m pytest backend

# JavaScript tests
npm test

# E2E tests
npm run test:e2e

# All tests
npm run test:all
```

## User Scenarios

### Document Analysis
1. Upload a project description (PDF/Word)
2. Ask "vilka myndigheter namns har?"
3. AI extracts entities with duplicate detection
4. Review and approve suggested additions
5. New nodes appear in the graph

### Finding Similar Projects
1. Upload your project proposal
2. Ask "finns det liknande projekt?"
3. System shows matching projects with similarity scores
4. Decide to add your project or join existing initiative

### Exploring the Graph
1. Use ChatPanel to search: "sok AI-projekt"
2. Graph displays matching nodes
3. Click nodes to see details and connections
4. Save custom views for later

## ChatGPT Widget Integration

The widget can be embedded in ChatGPT or other platforms:

```html
<script src="https://your-server/widget/widget.iife.js"></script>
<link rel="stylesheet" href="https://your-server/widget/style.css">
<community-graph-widget api-url="https://your-server"></community-graph-widget>
```

The widget provides:
- Graph visualization
- Chat interface
- MCP tool execution

## Event Subscriptions & Webhooks

The system supports webhook notifications for graph mutations:

- **EventSubscription nodes** define webhook targets and filters
- **Events** are generated when nodes are created, updated, or deleted
- **Loop prevention** via `event_origin` and `event_session_id` tracking
- **Retry logic** with exponential backoff for failed deliveries

Create subscriptions via the web UI (right-click on canvas) or API. See [docs/EVENT_SUBSCRIPTIONS.md](./docs/EVENT_SUBSCRIPTIONS.md) for detailed documentation.

## Security

- Max 10 nodes per delete operation
- Confirmation required for deletions
- Community-based isolation
- No personal data handling

## Development

See [backend/DEVELOPMENT.md](./backend/DEVELOPMENT.md) for detailed development guide including:
- Architecture overview
- Adding new MCP tools
- Testing strategies
- API documentation

## License

MIT License - see LICENSE for details
