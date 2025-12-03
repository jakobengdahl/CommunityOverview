# System Architecture

## Översikt

Community Knowledge Graph är en AI-powered PoC för kunskapsdelning med graf-visualisering.

```
┌─────────────────────────────────────────────────────────┐
│                      Frontend                           │
│  ┌──────────────┐              ┌──────────────┐        │
│  │ Chat Panel   │              │ Visualization│        │
│  │              │              │   Panel      │        │
│  │ - Messages   │              │              │        │
│  │ - Input      │              │ React Flow   │        │
│  │              │              │ - Nodes      │        │
│  └──────────────┘              │ - Edges      │        │
│                                 └──────────────┘        │
│                                                          │
│  State Management: Zustand                              │
└─────────────────────┬───────────────────────────────────┘
                      │
                      │ Claude API + MCP Tools
                      │
┌─────────────────────▼───────────────────────────────────┐
│                   MCP Server                            │
│  ┌──────────────────────────────────────────────────┐  │
│  │  MCP Tools:                                      │  │
│  │  - search_graph                                  │  │
│  │  - get_node_details                              │  │
│  │  - get_related_nodes                             │  │
│  │  - find_similar_nodes                            │  │
│  │  - add_nodes                                     │  │
│  │  - update_node                                   │  │
│  │  - delete_nodes                                  │  │
│  │  - get_graph_stats                               │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Graph Storage (NetworkX + JSON)                │  │
│  │  - In-memory graph for fast queries             │  │
│  │  - JSON persistence (graph.json)                │  │
│  │  - Similarity search (Levenshtein)              │  │
│  └──────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

## Komponenter

### Frontend (React + Vite)
- **React Flow:** Graf-visualisering
- **Zustand:** State management
- **Claude API:** Chat-integration med MCP

### MCP Server (Python + FastMCP)
- **FastMCP:** MCP server implementation
- **NetworkX:** In-memory graf
- **Pydantic:** Data validation
- **Levenshtein:** Text similarity

### Graf-lagring
- **NetworkX:** MultiDiGraph för noder och edges
- **JSON:** Persistent storage i `graph.json`
- **Future:** Vector embeddings för similarity search

## Data Flow

### Sökning
1. User skriver fråga i chat
2. Frontend → Claude API med MCP context
3. Claude anropar `search_graph()` MCP tool
4. MCP server söker i NetworkX graf
5. Returnerar matching nodes
6. Claude svarar i chat + uppdaterar visualisering

### Lägga till nod
1. User: "Lägg till initiativ X"
2. Claude → `find_similar_nodes()` (dublettkontroll)
3. Claude presenterar förslag + liknande
4. User bekräftar
5. Claude → `add_nodes()`
6. MCP sparar till JSON + NetworkX
7. Visualisering uppdateras

### Dokumentuppladdning
1. User laddar upp PDF/Word
2. Frontend → Claude API med dokument
3. Claude → `propose_nodes_from_text()`
4. MCP extraherar noder via Claude API
5. MCP kör `find_similar_nodes()` för varje
6. Returnerar proposed + existing similar
7. User granskar och godkänner
8. Claude → `add_nodes()` för godkända

## Säkerhet

- Community-baserad filtrering
- Max 10 noder per delete
- Dubbelkonfirmation för deletion
- Varning mot personuppgifter
- Audit log för deletions (TODO)

## Scaling Considerations

**Current (PoC):**
- NetworkX in-memory (~500 noder)
- JSON file persistence
- Text-based similarity

**Future:**
- Neo4j eller ArangoDB för >1000 noder
- Vector database (Pinecone, Weaviate)
- Sentence-transformers för embeddings
- Caching layer (Redis)
- Multi-tenant support

## Deployment

**Development:**
- Docker Compose
- GitHub Codespaces

**Production (Future):**
- Kubernetes
- Managed database
- CDN för frontend
- API Gateway
