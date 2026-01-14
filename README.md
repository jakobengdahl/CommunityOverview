# Community Knowledge Sharing PoC

AI-powered knowledge sharing for communities with graph visualization, conversational chat, and intelligent document analysis.

## Overview

This system helps organizations avoid overlapping investments by making visible:
- Ongoing initiatives and projects
- Resources and capabilities
- Connections between actors, legislation, and themes

**Key Features:**
- 🤖 **AI-Powered Chat:** Natural language interface with Claude or OpenAI for exploring and managing the knowledge graph
- 🔄 **Multi-Provider Support:** Switch between Claude (Anthropic) and OpenAI backends
- 📄 **Document Upload:** Upload PDF, Word, or text documents for automatic entity extraction
- 🔗 **URL Integration:** Paste document URLs for automatic download and analysis
- 🔍 **Batch Processing:** Efficient similarity search for multiple entities at once
- 🎨 **Interactive Visualization:** React Flow graph with drag-and-drop, zoom, and pan
- 💾 **Save Views:** Create and share custom graph views
- 📊 **Duplicate Detection:** Automatic similarity checking using Levenshtein distance and semantic embeddings

**Tech stack:**
- **Frontend:** React + React Flow + Zustand
- **Backend:** FastMCP Server (Python) with NetworkX + JSON
- **AI:** Claude Sonnet 4.5 or OpenAI GPT-4o for natural language understanding and entity extraction
- **Graph storage:** NetworkX in-memory + JSON persistence
- **Similarity search:** sentence-transformers (all-MiniLM-L6-v2) + Levenshtein distance

## Project Structure

```
/frontend                      # React app with graph visualization
  /src/components             # UI components (Header, ChatPanel, etc.)
  /src/services               # API client for backend communication
  /src/store                  # Zustand state management
/mcp-server                   # Python MCP server with graph logic
  graph.json                  # Graph data storage (auto-created)
  llm_providers.py            # LLM provider abstraction (Claude/OpenAI)
  chat_logic.py               # Chat processing and tool execution
  graph_storage.py            # NetworkX graph operations
  server.py                   # FastAPI HTTP server
  /tests                      # Unit and integration tests
/docs                          # Documentation and specifications
LLM_PROVIDERS.md              # Detailed LLM configuration guide
TROUBLESHOOTING_OPENAI.md     # OpenAI setup troubleshooting
```

**Data Storage:**
- Graph data is stored in `/mcp-server/graph.json`
- Vector embeddings in `/mcp-server/embeddings.pkl`
- Both files are auto-created on first run

## Metamodel

### Node Types
- **Actor** (blue) - Government agencies, organizations
- **Community** (purple) - eSam, Myndigheter, Officiell Statistik
- **Initiative** (green) - Projects, collaborative activities
- **Capability** (orange) - Procurement, IT development, portfolio management
- **Resource** (yellow) - Reports, software components
- **Legislation** (red) - NIS2, GDPR, etc.
- **Theme** (teal) - AI, data strategies, change management
- **VisualizationView** (gray) - Predefined views for navigation

### Relationships
- BELONGS_TO, IMPLEMENTS, PRODUCES, GOVERNED_BY, RELATES_TO, PART_OF

## Quick Start

### Local Development

**MCP Server:**
```bash
cd mcp-server
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

### With Docker Compose
```bash
docker-compose up
```

### GitHub Codespaces
Open the project in Codespaces - everything is pre-configured.

## LLM Provider Configuration

This project supports both **Claude (Anthropic)** and **OpenAI (GPT-4)** as AI backends with **automatic provider detection**.

### Auto-Detection (Recommended)

The system automatically detects which provider to use based on available API keys:

```bash
# Just set your API key - provider is auto-detected
export OPENAI_API_KEY=sk-xxxxx           # Auto-selects OpenAI
# OR
export ANTHROPIC_API_KEY=sk-ant-xxxxx    # Auto-selects Claude

# Start the server
cd mcp-server
python server.py
```

**Priority when both keys are set:**
1. `LLM_PROVIDER` env variable (if explicitly set)
2. OpenAI (preferred as more cost-effective)
3. Claude (fallback)

### Manual Provider Selection

**Force Claude:**
```bash
export LLM_PROVIDER=claude
export ANTHROPIC_API_KEY=sk-ant-xxxxx
```

**Force OpenAI:**
```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-xxxxx
```

### Runtime Override

Users can override the provider at runtime through:
- **UI Settings (⚙️)**: Select provider and optionally provide API key
- **URL Parameters**: `?provider=openai&apikey=sk-xxxxx`

📖 **For detailed configuration and troubleshooting, see [LLM_PROVIDERS.md](./LLM_PROVIDERS.md)**

## URL Parameters

The application supports configuration via URL parameters for easy sharing and testing:

### Available Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `provider` | LLM provider (`claude` or `openai`) | `?provider=openai` |
| `apikey` | Custom API key for the session | `?apikey=sk-xxxxx` |
| `loaddata` | Load external graph data from URL | `?loaddata=https://...` |
| `view` | Load a saved visualization view | `?view=MyView` |
| `community` | Pre-select communities (multiple) | `?community=eSam&community=Myndigheter` |

### Examples

**Use OpenAI with custom key:**
```
http://localhost:5173/?provider=openai&apikey=sk-xxxxx
```

**Use Claude with custom key:**
```
http://localhost:5173/?provider=claude&apikey=sk-ant-xxxxx
```

**Load external data with OpenAI:**
```
http://localhost:5173/?provider=openai&loaddata=https%3A%2F%2Fraw.githubusercontent.com%2Fuser%2Frepo%2Fmain%2Fdata.json
```

**Open specific view with community filter:**
```
http://localhost:5173/?view=AI-Projects&community=eSam
```

### External Data Format

When using `loaddata`, the JSON should follow this format:
```json
{
  "nodes": [
    {
      "id": "1",
      "label": "Node Name",
      "data": {
        "label": "Node Name",
        "description": "Description",
        "type": "Initiative"
      },
      "position": { "x": 100, "y": 100 }
    }
  ],
  "edges": [
    {
      "id": "e1-2",
      "source": "1",
      "target": "2",
      "data": { "label": "relates to" }
    }
  ]
}
```

See `example-graph-data.json` for a complete example.

## User Scenarios

### Scenario 1: Document Analysis
1. **Upload:** User uploads a project description (PDF/Word/URL)
2. **Chat:** User asks "vilka myndigheter nämns här?"
3. **Process:** AI extracts all agencies using batch similarity search
4. **Review:** System shows which agencies are new vs. duplicates
5. **Approve:** User reviews and approves additions
6. **Visualize:** New nodes appear in the graph with automatic connections

### Scenario 2: Finding Similar Projects
1. **Upload:** User uploads their project proposal
2. **Chat:** User asks "finns det liknande projekt i grafen?"
3. **Search:** AI searches existing graph for similar initiatives
4. **Present:** System shows matching projects with similarity scores
5. **Decide:** User can add their project or join existing initiative

### Scenario 3: Exploring the Graph
1. **Search:** User asks "sök i databasen efter AI-projekt"
2. **Visualize:** Graph shows all AI-related initiatives
3. **Expand:** User right-clicks node to see related actors and legislation
4. **Navigate:** User uses zoom, pan, and drag to explore connections
5. **Save:** User saves custom view for future reference

## Security

- Max 10 nodes per delete operation
- Community-based isolation
- Warning against handling personal data
- Audit log for deletions

## Development Status

**Current Phase:** Phase 2 - Document Analysis & UX Enhancements

### Completed Features ✅
- ✅ Basic graph visualization with React Flow
- ✅ AI chat interface with Claude integration
- ✅ Node CRUD operations with duplicate detection
- ✅ Document upload (PDF, Word, Text)
- ✅ URL-based document download
- ✅ Batch similarity search (90% fewer API calls)
- ✅ Loading indicators and progress feedback
- ✅ Modal dialogs for node editing
- ✅ Right-click panning in visualization
- ✅ Context menus auto-close on background click
- ✅ Save and load custom views
- ✅ Community-based filtering

### In Progress 🚧
- 🚧 Enhanced relationship suggestions
- 🚧 Advanced search filters
- 🚧 Export functionality

### Planned 📋
- 📋 Real-time collaboration
- 📋 Graph analytics and insights
- 📋 Advanced RAG with document chunks
- 📋 Integration with external data sources

See [Implementation Roadmap](docs/roadmap.md) for detailed progress.

## License

MIT License - see LICENSE for details
