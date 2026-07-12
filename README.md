# Community Knowledge Sharing

AI-powered knowledge sharing for communities with graph visualization, conversational chat, and intelligent document analysis.

![Screenshot of Community Knowledge Sharing application](docs/images/ui-overview.png)

## Overview

This system helps organizations avoid overlapping investments by making visible:
- Ongoing initiatives and projects
- Resources and capabilities
- Connections between actors, legislation, and themes

**Key Features:**

*Graph & visualization*
- **Interactive canvas:** React Flow graph with drag-and-drop, zoom, pan, and node groups
- **Schema-driven node types:** Node types, colours, icons, and fields are all profile-configurable — no code changes needed
- **Subtypes:** Optional sub-classification tags on any node type, with autocomplete from existing values
- **Save views:** Snapshot and restore named canvas views, stored as nodes in the graph
- **Schema-driven context menu:** Add custom right-click actions per node type via `schema_config.json` — open URLs with node field substitution or fire named callbacks
- **Interactive guides:** Step-by-step onboarding tours triggered via URL parameter or AI assistant

*AI chat & intelligence*
- **AI-powered chat:** Natural language interface (Claude or OpenAI) for exploring and managing the graph
- **Multi-provider support:** Switch between Claude (Anthropic), OpenAI, and any OpenAI-compatible endpoint (Ollama, Groq, Azure, etc.)
- **Document upload:** Upload PDF, Word, or text documents for automatic entity extraction
- **Node proposals:** LLM suggests entities with duplicate detection; user confirms before any node is added
- **Node marking:** AI assistant can annotate nodes with colours and labels (session-only, never persisted)
- **Markdown rendering:** Chat responses render Markdown (bold, lists, tables, code blocks)
- **AI Skills:** Profile-configurable SKILL.md instructions injected into the agent system prompt; ships with generic impact analysis; ESS profile adds GSIM lineage and change-impact skills
- **Skill node type:** Create and manage SKILL.md-compatible skill definitions directly in the graph

*Integration & extensibility*
- **MCP server:** Full Model Context Protocol support — connect Claude, ChatGPT, or any MCP-compatible AI client directly to the graph
- **ChatGPT widget:** Embeddable widget for use in ChatGPT or other interfaces
- **Event subscriptions & webhooks:** Receive HTTP POST notifications when nodes are created, updated, or deleted
- **AI agent system:** Event-triggered and schedule-triggered agents that act on graph mutations or run on a cron schedule
- **Federation:** Connect to multiple remote graph instances with configurable depth, provenance labels, and node adoption

*Operations & deployment*
- **Runs without LLM keys:** Graph API and MCP server work without any API key; AI chat is simply hidden
- **Multi-language:** English and Swedish UI, selectable via URL, startup flag, or schema config
- **Authentication:** Basic Auth for all endpoints or MCP-only (for Cloud Run + IAP setups)
- **Profile system:** Run with different metadata models, AI prompts, and seed data per deployment profile
- **Data management:** Example datasets with easy loading from local files or URLs

See [docs/USER_GUIDE.md](./docs/USER_GUIDE.md) for a full user-facing walkthrough.

**Tech Stack:**
- **Frontend:** React + React Flow + Zustand (monorepo with npm workspaces)
- **Backend:** FastAPI + FastMCP (Python) with NetworkX + JSON
- **AI:** Claude or OpenAI for natural language understanding and entity extraction
- **Graph storage:** NetworkX in-memory + JSON persistence
- **Similarity search:** sentence-transformers + RapidFuzz

## Project Structure

```
/backend                          # Python backend directory
  /api_host                       # FastAPI server host
    server.py                     # Main server with REST, MCP, and static files
    config.py                     # Server configuration
  /core                           # Core graph data structures
    storage.py                    # NetworkX graph operations
    storage_backends.py           # Persistence backend abstraction
    models.py                     # Node/Edge data models
    vector_store.py               # Similarity search
    /events                       # Graph mutation event / webhook system
  /service                        # Graph service layer
    service.py                    # High-level graph operations
    rest_api.py                   # REST API router
    mcp_tools.py                  # MCP tool definitions
  /ui                             # Chat and document handling
    chat_service.py               # LLM chat with tool execution
    chat_logic.py                 # Chat processing logic
    document_service.py           # Document parsing
    rest_api.py                   # Chat REST endpoints
  /llm                            # LLM provider abstraction
    llm_providers.py              # LLM provider abstraction
    language_policy.py            # Language-policy prompt helpers
  /runtime                        # Request/authorization/config runtime context
    authorization.py              # Graph authorization context
    request_context.py            # Actor/scope request context
    config_context.py             # Config path resolution context
  /skills                         # Skills loader system
    loader.py                     # SkillsLoader — fetches/parses SKILL.md files
  /agents                         # Agent execution (event- and schedule-triggered)
  /federation                     # Federated graph cache and search
  /tests                          # Cross-cutting backend tests
  config_loader.py                # Schema and config loading
  document_processor.py           # Document text extraction
/config                           # Configuration profiles
  /default                        # Default profile (base, always required)
    schema_config.json            # Node types, relationships, presentation
    federation_config.json        # Federation graph connections
    .env.example                  # Environment variable template
    /skills                       # Skills loaded for all profiles (fallback)
      /impact-analysis            # Generic graph dependency impact analysis
  /stat-metadata                  # European Statistical System metadata profile
    schema_config.json            # ESS node types (NSIs, programmes, variables…)
    graph.json                    # ESS seed data
    /skills                       # ESS-specific skills (loaded in addition to default)
      /graph-analysis             # Generic graph pattern analysis
      /gsim-lineage-impact        # GSIM lineage tracing and change impact assessment
  /scb                            # SCB (Statistics Sweden) demo profile
    schema_config.json            # Statistical metadata model
  /test                           # Test profile
    schema_config.json            # Minimal config for testing
  profile-utils.sh                # Shared profile resolution utilities
/data                             # Graph data
  /examples                       # Example datasets (tracked in git)
    default.json                  # Default example dataset
  /active                         # Active data used at runtime (git-ignored)
    graph.json                    # Currently active graph file
/frontend                         # Frontend applications
  /web                            # React web application
    /src/components               # UI components (ChatPanel, etc.)
    /src/i18n                     # Internationalization (en, sv)
    /src/services                 # API client
    /src/store                    # Zustand state
    /tests                        # Unit and e2e tests
  /widget                         # ChatGPT embeddable widget
/packages                         # Shared packages
  /ui-graph-canvas                # Shared React Flow component
/services                         # Standalone auxiliary services
  /mcp_oauth_gateway              # OAuth 2.1 gateway wrapping the MCP endpoint
/scripts                          # Utility scripts
/docs                             # Documentation (see docs/README.md for a status-tagged index)
  README.md                       # Documentation index: current / design / historical
  DATA_MANAGEMENT.md              # Graph data management guide
  EVENT_SUBSCRIPTIONS.md          # Webhook/event system docs
  PROFILES.md                     # Configuration profiles guide
  DEPLOYMENT_GUIDE.md             # Deployment documentation
  FEDERATED_GRAPH_DESIGN.md       # Federated multi-graph architecture
  CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md  # Public core plan for runtime modes and extension seams
  CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md # Concrete public implementation slices for runtime and extension readiness
start-dev.sh                      # Development startup script
LLM_PROVIDERS.md                  # LLM configuration guide
```

## Metamodel

The metamodel defines two categories of node types. Node types, relationships, colors, icons, and AI prompts are all **configurable per profile** via `schema_config.json`. See [docs/PROFILES.md](./docs/PROFILES.md) for the full guide.

### Domain Node Types (configurable via profile)

The default profile includes these domain types:

- **Actor** (blue) - Organizations, agencies, individuals
- **Initiative** (green) - Projects, programs, collaborative activities
- **Capability** (orange) - Capabilities, competencies, skills
- **Resource** (yellow) - Reports, software, tools, datasets
- **Legislation** (red) - Laws, directives (NIS2, GDPR, etc.)
- **Theme** (teal) - AI strategies, data strategies, themes
- **Goal** (indigo) - Strategic objectives and targets
- **Event** (fuchsia) - Conferences, workshops, milestones
- **Data** (cyan) - Datasets, registers, APIs, data sources
- **Risk** (red) - Identified risks, threats, or vulnerabilities

Other profiles can add domain-specific types. For example, the **stat-metadata** profile adds: StatisticalProgramme, DataSet, DataStructure, InstanceVariable, Concept, UnitType, CodeList, Questionnaire, ProductionSolution, SubjectField. The **scb** profile adds: Dataset, Hållpunkt, Undersökning, Variabel, Värdemängd, Population, Klassifikation.

All domain nodes support **subtypes** for finer sub-classification within each node type (e.g., an Actor can be tagged as "Government agency", "Municipality", "Steering group"). Subtypes are optional, stored as a list, and the UI provides autocomplete with case normalization based on existing subtypes in the graph.

### System Node Types (foundational to the application)

These are integral to core application functionality:

- **SavedView / VisualizationView** (gray) - Saved graph view snapshots
- **EventSubscription** (violet) - Webhook subscriptions for graph mutation events
- **Agent** (pink) - AI agent configurations (runtime not implemented)
- **Skill** (violet) - SKILL.md-compatible agent skill definitions stored in the graph; uses a specialized creation form
- **Groups** - Visual grouping of nodes in the canvas

### Relationships
- Default: BELONGS_TO, IMPLEMENTS, PRODUCES, GOVERNED_BY, RELATES_TO, PART_OF, AIMS_FOR
- Profiles can define additional relationship types (e.g., MEASURES, DESCRIBES, USES, DERIVED_FROM)

## Quick Start

### Running behind a reverse proxy

The app works out of the box behind any path-stripping reverse proxy. The frontend automatically detects the proxy path prefix from the browser URL — no extra configuration is needed.

**If Node.js is not pre-installed (managed cloud environments, sandboxes)**

Install Node.js via nvm — no root access required:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc
nvm install 20
```

Then start the app normally:

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxx  # or OPENAI_API_KEY
./start-dev.sh
```

The app will be reachable at whatever URL your environment exposes for port 8000, e.g.:
`https://<your-host>/proxy/8000/web/`

> **Note:** Make sure port 8000 is publicly accessible in your environment's network or port settings.

### Development Mode (Recommended)

Start all services with a single command:

```bash
# Set your API key (pick one)
export OPENAI_API_KEY=sk-xxxxx        # For OpenAI
export ANTHROPIC_API_KEY=sk-ant-xxxxx # For Claude

# Start everything (default profile, English)
./start-dev.sh

# Start with a specific profile
./start-dev.sh --profile stat-metadata   # European Statistical System
./start-dev.sh --profile scb             # Statistics Sweden

# Start with Swedish UI
./start-dev.sh --lang sv

# Combine profile, language, and data
./start-dev.sh --profile stat-metadata --lang en

# Start with data from a URL
./start-dev.sh --data https://example.github.io/data/graph.json
```

The script will:
- Check for and set up active graph data (copies example data on first run)
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
| http://localhost:8000/federation/status | Federation cache/status |
| http://localhost:8000/federation/sync | Trigger federation sync (POST) |

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

## Language Configuration

The application supports English and Swedish. Language can be set in three ways:

1. **URL parameter** (highest priority): `http://localhost:8000/web/?lang=sv`
2. **Startup flag**: `./start-dev.sh --lang sv`
3. **Schema config** (`config/default/schema_config.json`): `"default_language": "en"`

The language setting affects the UI labels, chat placeholders, notifications, and welcome message. The AI chat assistant responds in whatever language the user writes in.

## Configuration Profiles

Profiles allow you to run the application with different metadata models, node types, and AI prompts. Each profile is a directory under `config/` that can override the default configuration.

```bash
# Start with a specific profile
./start-dev.sh --profile stat-metadata   # ESS statistical metadata (recommended for ESS use)
./start-dev.sh --profile scb             # Statistics Sweden (Swedish language)

# Profiles available out of the box:
#   default        - General community knowledge graph
#   stat-metadata  - European Statistical System metadata (NSIs, programmes, datasets, variables)
#   scb            - Statistics Sweden (Dataset, Undersökning, Variabel, etc.)
#   test           - Minimal config for testing
```

For cloud environments (SSPCloud), run `./scripts/start-sprint.sh` — it auto-installs all dependencies
and loads the `stat-metadata` profile. See [docs/SSPCloud-setup.md](./docs/SSPCloud-setup.md).

Each profile can contain:
- `schema_config.json` — Node types, relationships, colors, icons, and AI prompts
- `federation_config.json` — Federation topology
- `.env` — Secrets and environment overrides (git-ignored)
- `graph.json` — Seed data for initial setup

Missing files fall back to `config/default/`. See [docs/PROFILES.md](./docs/PROFILES.md) for the complete guide on creating custom profiles.

Federation topology can be configured at startup with `FEDERATION_FILE` (default: `config/default/federation_config.json`). This is admin-only configuration and is not editable via GUI/chat tools.


Example federation depth setup (installation policy):

```json
{
  "federation": {
    "enabled": true,
    "max_traversal_depth": 4,
    "depth_levels": [1, 2, 4],
    "graphs": [
      {
        "graph_id": "esam-main",
        "display_name": "eSam",
        "enabled": true,
        "max_depth_override": 2,
        "endpoints": { "graph_json_url": "https://example.org/graph.json" }
      }
    ]
  }
}
```

UI behavior:
- Only configured selectable levels are shown (bounded by effective max depth).
- If only one level is available, the depth selector is hidden.
- Search labels show `<GraphName>: <NodeName>` only when multiple graphs are available.

You can also name the local graph in `graph.json` metadata:

```json
{
  "metadata": {
    "graph_name": "My Local Collaboration Graph"
  }
}
```

## Data Management

Graph data is stored separately from the codebase:
- **Example data** lives in `data/examples/` (tracked in git)
- **Active data** lives in `data/active/graph.json` (git-ignored)

On first run, the default example data is automatically copied to the active location. Use `--data` to load different datasets. See [docs/DATA_MANAGEMENT.md](./docs/DATA_MANAGEMENT.md) for details.
For upcoming multi-instance capabilities, see [docs/FEDERATED_GRAPH_DESIGN.md](./docs/FEDERATED_GRAPH_DESIGN.md).

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

### Running without LLM keys

The application starts and operates fully without any LLM API keys. When no key is
configured the built-in AI chat assistant is hidden in the UI, and background agent
workers remain inactive. The MCP server, graph API, and all read/write operations
work normally. This allows teams to run the knowledge graph as a standalone data
platform and add AI capabilities later by setting an API key and restarting.

## Authentication

### Full Basic Auth (all endpoints)

```bash
export AUTH_ENABLED=true
export AUTH_USERNAME=admin
export AUTH_PASSWORD=secret
```

### MCP-only Basic Auth (for Google Cloud Run / IAP deployments)

When running behind Google Cloud Run with IAP, the web GUI and REST API are already protected by Google login. Use `MCP_BASIC_AUTH` to add Basic Auth only to MCP endpoints (`/mcp/*` and `/execute_tool`), which are called by external MCP clients that cannot use IAP:

```bash
export AUTH_ENABLED=false
export MCP_BASIC_AUTH=true
export AUTH_USERNAME=mcp-client
export AUTH_PASSWORD=secret
```

| Variable | Default | Description |
|---|---|---|
| `AUTH_ENABLED` | `false` | Enable Basic Auth on **all** endpoints (except `/health`, `/info`) |
| `MCP_BASIC_AUTH` | `false` | Enable Basic Auth **only** on `/mcp/*` and `/execute_tool` |
| `AUTH_USERNAME` | `admin` | Username for Basic Auth |
| `AUTH_PASSWORD` | *(none)* | Password for Basic Auth (required for either mode to activate) |

If both `AUTH_ENABLED` and `MCP_BASIC_AUTH` are `true`, `AUTH_ENABLED` takes precedence and all endpoints require auth.

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
2. Ask "which organizations are mentioned?"
3. AI extracts entities with duplicate detection
4. Review and approve suggested additions
5. New nodes appear in the graph

### Finding Similar Projects
1. Upload your project proposal
2. Ask "are there similar projects?"
3. System shows matching projects with similarity scores
4. Decide to add your project or join existing initiative

### Exploring the Graph
1. Use the chat panel to search: "search AI projects"
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

