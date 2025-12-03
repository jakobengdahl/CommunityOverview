# Implementation Roadmap

Status för Community Knowledge Graph PoC.

## ✅ Fas 1: Grundläggande infrastruktur (KLAR)

### 1.1 Projekt setup ✅
- [x] Repo-struktur: `/frontend`, `/mcp-server`, `/docs`
- [x] React app med Vite
- [x] Python MCP server med FastMCP
- [x] Docker Compose
- [x] GitHub Codespaces config

### 1.2 MCP Knowledge Graph anpassning ✅
- [x] Metamodell implementerad (8 node types)
- [x] NetworkX + JSON lagring
- [x] Initial `graph.json` med exempel-data (14 noder)

### 1.3 Grundläggande MCP tools ✅
- [x] `search_graph()` - text-baserad sökning
- [x] `get_node_details()`
- [x] `get_related_nodes()`
- [x] `add_nodes()` med validation
- [x] `update_node()`
- [x] `delete_nodes()` med säkerhetskontroller
- [x] `find_similar_nodes()` (Levenshtein)
- [x] `get_graph_stats()`
- [x] `list_node_types()`, `list_relationship_types()`

### 1.4 Frontend: Grundläggande layout ✅
- [x] Split-panel layout (40% chat, 60% graf)
- [x] Community dropdown i header
- [x] URL-query parsing för `?community=X`
- [x] Chat-interface med message list
- [x] Zustand state management

---

## 🔨 Fas 2: Graf-visualisering (IN PROGRESS)

### 2.1 React Flow integration ✅
- [x] React Flow setup
- [x] Custom node-komponenter (färgkodade)
- [x] Node-rendering: namn + summary
- [ ] **TODO:** Bättre layout-algoritm (hierarchical/force-directed)

### 2.2 Graf-navigation 🔨
- [x] Zoom/pan funktionalitet (via React Flow)
- [x] Node selection
- [x] [+]-ikon på noder
- [ ] **TODO:** Click handler för "visa relaterade" (behöver MCP integration)

### 2.3 Dynamisk graf-uppdatering 🔨
- [x] Zustand state för graf-data
- [x] `updateVisualization()` funktion
- [ ] **TODO:** Animated transitions
- [x] Highlight-styling för noder

---

## 📋 Fas 3: Claude integration och chat (TODO)

### 3.1 Claude API setup
- [ ] Anthropic API-klient i frontend
- [ ] Environment variables för API key
- [ ] MCP-tools registrering i Claude-anrop
- [ ] Error handling och retry-logik

### 3.2 Chat-flöde: Sökning
- [ ] User input → Claude API med MCP context
- [ ] Claude anropar `search_graph()`
- [ ] Parse response och uppdatera visualisering
- [ ] Display Claude's svar i chat

### 3.3 Chat-flöde: Tvåstegs nodtillägg
- [x] `find_similar_nodes()` implementerad
- [ ] Claude föreslår nod + kopplingar + dubletter
- [ ] User godkännande workflow
- [ ] `add_nodes()` efter godkännande
- [ ] Uppdatera visualisering

### 3.4 Välkomstmeddelande
- [x] Välkomst-prompt med exempelfrågor (i frontend)
- [x] Personuppgiftsvarning (i frontend)
- [ ] **TODO:** System prompt för MCP server

---

## 📄 Fas 4: Dokumentuppladdning och RAG (TODO)

### 4.1 Filuppladdning i GUI
- [ ] Upload-knapp och file-picker
- [ ] Stöd för PDF och Word
- [ ] Uppladdningsstatus
- [ ] Skicka fil till Claude API

### 4.2 Dokumentextraktion MCP tool
- [ ] `propose_nodes_from_text()` implementation
- [ ] PDF/Word parsing (PyMuPDF, python-docx)
- [ ] Structured prompt till Claude för extraktion
- [ ] Auto-länkning till active communities

### 4.3 Flöde: Dokument → Förslag → Godkännande
- [ ] Extrahera noder + hitta dubletter
- [ ] Presentera i chat
- [ ] Visa proposed noder i visualisering (annan stil)
- [ ] User-godkännande → batch `add_nodes()`

---

## 🚀 Fas 5: Avancerad funktionalitet (TODO)

### 5.1 Graf-statistik och översikt
- [x] `get_graph_stats()` MCP tool
- [ ] Visa stats i GUI
- [ ] "Visa hela grafen"-knapp

### 5.2 Node-editering
- [x] `update_node()` MCP tool
- [ ] Edit via chat
- [ ] (Optional) Formulär för node-editering i GUI

### 5.3 Node-borttagning med säkerhet
- [x] `delete_nodes()` med max 10 nodes-gräns
- [x] Säkerhetskontroller i MCP
- [ ] Dubbelkonfirmation i chat
- [ ] Visa påverkade kopplingar
- [ ] Audit log för deletions

### 5.4 VisualizationViews
- [ ] Stöd för URL: `?view=radarbildlagstiftning`
- [ ] Ladda fördefinierad node-uppsättning
- [ ] Skapa 2-3 exempel-vyer

---

## 🎨 Fas 6: Förbättringar och polish (TODO)

### 6.1 Similarity search med embeddings (optional)
- [ ] Installera sentence-transformers
- [ ] Generera embeddings vid node-creation
- [ ] Uppdatera `find_similar_nodes()` med vector search
- [ ] Cacha embeddings i JSON

### 6.2 UI/UX-förbättringar
- [ ] Loading states och spinners
- [ ] Error messages och user feedback
- [ ] Tooltips på noder
- [ ] Responsiv layout

### 6.3 Documentation och README
- [x] Root README med översikt
- [x] MCP server README
- [x] Frontend README
- [x] Architecture diagram
- [ ] Video/GIF demo
- [ ] Setup guide för nya utvecklare

---

## 🧪 Fas 7: Testing och deployment-prep (TODO)

### 7.1 Automatiserade tester
- [ ] Frontend: React Testing Library
- [ ] MCP: Pytest för alla tools
- [ ] E2E: Playwright för critical user flows
- [ ] Screenshot-tester

### 7.2 Docker och Codespaces
- [x] Dockerfile för MCP server
- [x] Dockerfile för frontend
- [x] Docker Compose
- [x] .devcontainer för Codespaces
- [ ] **TODO:** Testa i Codespaces

### 7.3 Performance och optimering
- [ ] Lazy loading av stora grafer
- [ ] Debounce för chat input
- [ ] Memoization av graf-beräkningar
- [ ] Test med 500 noder

---

## Current Status

**Completed:** Fas 1 (Grundläggande infrastruktur)

**In Progress:** Fas 2 (Graf-visualisering)

**Next Steps:**
1. Testa MCP server lokalt
2. Integrera Claude API i frontend
3. Implementera första use case: Sökning + visualisering

**Blockers:** Ingen

**Estimated Completion:** Fas 1-3 inom 1-2 veckor
