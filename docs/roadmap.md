# Implementation Roadmap

Status for Community Knowledge Graph PoC.

## ✅ Phase 1: Basic Infrastructure (COMPLETE)

### 1.1 Project setup ✅
- [x] Repo structure: `/frontend`, `/mcp-server`, `/docs`
- [x] React app with Vite
- [x] Python MCP server with FastMCP
- [x] Docker Compose
- [x] GitHub Codespaces config

### 1.2 MCP Knowledge Graph adaptation ✅
- [x] Metamodel implemented (8 node types)
- [x] NetworkX + JSON storage
- [x] Initial `graph.json` with example data (14 nodes)

### 1.3 Basic MCP tools ✅
- [x] `search_graph()` - text-based search
- [x] `get_node_details()`
- [x] `get_related_nodes()`
- [x] `add_nodes()` with validation
- [x] `update_node()`
- [x] `delete_nodes()` with security controls
- [x] `find_similar_nodes()` (Levenshtein)
- [x] `get_graph_stats()`
- [x] `list_node_types()`, `list_relationship_types()`

### 1.4 Frontend: Basic layout ✅
- [x] Split-panel layout (40% chat, 60% graph)
- [x] Community dropdown in header
- [x] URL-query parsing for `?community=X`
- [x] Chat interface with message list
- [x] Zustand state management

---

## ✅ Phase 2: Graph Visualization (COMPLETE)

### 2.1 React Flow integration ✅
- [x] React Flow setup
- [x] Custom node components (color-coded)
- [x] Node rendering: name + summary
- [x] Better layout algorithm (dagre hierarchical)

### 2.2 Graph navigation ✅
- [x] Zoom/pan functionality (via React Flow)
- [x] Node selection
- [x] [+] icon on nodes
- [x] Click handler for "show related" nodes

### 2.3 Dynamic graph updates ✅
- [x] Zustand state for graph data
- [x] `updateVisualization()` function
- [x] Animated transitions (800ms fitView)
- [x] Highlight styling for nodes

---

## ✅ Phase 3: Claude Integration and Chat (COMPLETE)

### 3.1 Claude API setup ✅
- [x] Anthropic API client in frontend
- [x] Environment variables for API key
- [x] MCP tools registration in Claude calls
- [x] Error handling and user feedback

### 3.2 Chat flow: Search ✅
- [x] User input → Claude API with MCP context
- [x] Claude calls `search_graph()`
- [x] Parse response and update visualization
- [x] Display Claude's response in chat

### 3.3 Chat flow: Two-step node addition ✅
- [x] `find_similar_nodes()` implemented with Levenshtein distance
- [x] Claude proposes node + connections + duplicates
- [x] User approval workflow with approve/reject buttons
- [x] `add_nodes()` after approval (addNodeToDemoData)
- [x] Update visualization automatically

### 3.4 Welcome message ✅
- [x] Welcome prompt with example questions (in frontend)
- [x] Personal data warning (in frontend)
- [x] System prompt for MCP server with workflow instructions

---

## 📋 Phase 4: Document Upload and RAG (TODO)

### 4.1 File upload in GUI
- [ ] Upload button and file picker
- [ ] Support for PDF and Word
- [ ] Upload status indicator
- [ ] Send file to Claude API

### 4.2 Document extraction MCP tool
- [ ] `propose_nodes_from_text()` implementation
- [ ] PDF/Word parsing (PyMuPDF, python-docx)
- [ ] Structured prompt to Claude for extraction
- [ ] Auto-linking to active communities

### 4.3 Flow: Document → Proposal → Approval
- [ ] Extract nodes + find duplicates
- [ ] Present in chat
- [ ] Show proposed nodes in visualization (different style)
- [ ] User approval → batch `add_nodes()`

---

## 🚀 Phase 5: Advanced Functionality (TODO)

### 5.1 Graph statistics and overview
- [x] `get_graph_stats()` MCP tool
- [ ] Show stats in GUI
- [ ] "Show entire graph" button

### 5.2 Node editing
- [x] `update_node()` MCP tool
- [ ] Edit via chat
- [ ] (Optional) Form for node editing in GUI

### 5.3 Node deletion with security
- [x] `delete_nodes()` with max 10 nodes limit
- [x] Security controls in MCP
- [ ] Double confirmation in chat
- [ ] Show affected connections
- [ ] Audit log for deletions

### 5.4 VisualizationViews
- [ ] Support for URL: `?view=radarbildlagstiftning`
- [ ] Load predefined node set
- [ ] Create 2-3 example views

---

## 🎨 Phase 6: Improvements and Polish (TODO)

### 6.1 Similarity search with embeddings (optional)
- [ ] Install sentence-transformers
- [ ] Generate embeddings on node creation
- [ ] Update `find_similar_nodes()` with vector search
- [ ] Cache embeddings in JSON

### 6.2 UI/UX improvements
- [x] Loading states (Thinking... button)
- [x] Error messages and user feedback
- [ ] Tooltips on nodes
- [ ] Responsive layout

### 6.3 Documentation and README
- [x] Root README with overview
- [x] MCP server README
- [x] Frontend README
- [x] Architecture diagram
- [ ] Video/GIF demo
- [ ] Setup guide for new developers

---

## 🧪 Phase 7: Testing and Deployment Prep (TODO)

### 7.1 Automated tests
- [ ] Frontend: React Testing Library
- [ ] MCP: Pytest for all tools
- [ ] E2E: Playwright for critical user flows
- [ ] Screenshot tests

### 7.2 Docker and Codespaces
- [x] Dockerfile for MCP server
- [x] Dockerfile for frontend
- [x] Docker Compose
- [x] .devcontainer for Codespaces
- [ ] **TODO:** Test in Codespaces

### 7.3 Performance and optimization
- [ ] Lazy loading for large graphs
- [ ] Debounce for chat input
- [ ] Memoization of graph calculations
- [ ] Test with 500 nodes

---

## Current Status

**Completed:**
- ✅ Phase 1: Basic Infrastructure
- ✅ Phase 2: Graph Visualization
- ✅ Phase 3: Claude Integration & Chat

**Next Steps:**
1. **Option A:** Phase 4: Document upload and RAG extraction
2. **Option B:** Phase 5-6: Advanced features and polish
3. **Option C:** Phase 7: Testing and deployment

**Current Features Working:**
- Natural language search with Claude
- Interactive graph visualization with dagre layout
- "Show related nodes" expansion
- Two-step node addition with duplicate detection
- User approval workflow for new nodes
- Automatic graph updates

**Blockers:** None

**Notes:**
- Claude integration uses client-side API (dangerouslyAllowBrowser: true)
- In production, should use backend proxy for API calls
- Currently works with demo data; real MCP server integration pending
- All core user flows are functional end-to-end
