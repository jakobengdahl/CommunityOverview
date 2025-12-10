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
/frontend          # React app with graph visualization
/mcp-server        # Python MCP server with graph logic
/docs              # Documentation and specifications
```

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

This project supports both **Claude (Anthropic)** and **OpenAI (GPT-4)** as AI backends.

### Quick Setup

**For Claude (default):**
```bash
export LLM_PROVIDER=claude
export ANTHROPIC_API_KEY=sk-ant-xxxxx
```

**For OpenAI:**
```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-xxxxx
```

**Frontend Override:**
Users can override the backend provider through the UI Settings (⚙️) by selecting their preferred provider and optionally providing their own API key.

📖 **For detailed configuration, see [LLM_PROVIDERS.md](./LLM_PROVIDERS.md)**

## URL Parameters

The application supports loading external graph data and custom API keys via URL parameters:

### Load External Graph Data

Load graph data from an external JSON file:
```
http://localhost:5173/?loaddata=https%3A%2F%2Fraw.githubusercontent.com%2Fuser%2Frepo%2Fmain%2Fdata.json
```

**Parameters:**
- `loaddata` - URL-encoded URL to a JSON file containing graph data

**JSON Format:**
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

### Custom API Key

Provide a custom Anthropic API key:
```
http://localhost:5173/?apikey=sk-ant-api03-...
```

**Parameters:**
- `apikey` - URL-encoded Anthropic API key

### Combining Parameters

You can combine multiple parameters:
```
http://localhost:5173/?loaddata=https%3A%2F%2F...&apikey=sk-ant-api03-...
```

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
