# Community Knowledge Sharing PoC

AI-powered knowledge sharing for communities with graph visualization and conversational chat.

## Overview

This system helps organizations avoid overlapping investments by making visible:
- Ongoing initiatives and projects
- Resources and capabilities
- Connections between actors, legislation, and themes

**Tech stack:**
- **Frontend:** React + React Flow + Zustand
- **Backend:** MCP Server (Python) with NetworkX + JSON
- **AI:** Claude API for RAG and extraction
- **Graph storage:** NetworkX in-memory + JSON persistence

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

## User Scenario

1. **Search:** User searches for initiatives related to NIS2
2. **Visualize:** Graph shows connections between initiatives, actors, and legislation
3. **Discover gaps:** User sees that their project is missing
4. **Upload:** User uploads project report
5. **Review:** System proposes nodes and connections
6. **Approve:** User reviews and approves additions

## Security

- Max 10 nodes per delete operation
- Community-based isolation
- Warning against handling personal data
- Audit log for deletions

## Development Status

See [Implementation Roadmap](docs/roadmap.md) for detailed progress.

**Current Phase:** Phase 1 - Basic Infrastructure

## License

MIT License - see LICENSE for details
