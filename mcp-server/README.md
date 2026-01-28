# Community Knowledge Graph MCP Server

MCP (Model Context Protocol) server för kunskapsdelning i communities.

## Funktionalitet

### MCP Tools

- **search_graph** - Sök efter noder baserat på text
- **get_node_details** - Hämta detaljer för specifik nod
- **get_related_nodes** - Hämta relaterade noder (med depth)
- **find_similar_nodes** - Hitta liknande noder för dublettkontroll
- **add_nodes** - Lägg till nya noder och edges
- **update_node** - Uppdatera befintlig nod
- **delete_nodes** - Ta bort noder (max 10, kräver confirmation)
- **get_graph_stats** - Hämta statistik
- **list_node_types** - Lista alla node-typer
- **list_relationship_types** - Lista alla relationship-typer

### Graf-lagring

- **NetworkX** in-memory graf för snabba queries
- **JSON** för persistens (graph.json)
- Automatisk save vid ändringar

## Installation

```bash
# Skapa virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Installera dependencies
pip install -r requirements.txt
```

## Användning

### Starta server
```bash
cd mcp-server
uvicorn app_host.server:get_app --factory --reload --port 8000
```

Servern tillhandahåller:
- **REST API**: http://localhost:8000/api/
- **MCP endpoint**: http://localhost:8000/mcp
- **Chat API**: http://localhost:8000/ui/

### Starta webbappen
```bash
cd mcp-server/apps/web
npm run dev
```

### Kör tester
```bash
# Python-tester
cd mcp-server
pytest

# JavaScript-tester
npm test

# E2E-tester
cd apps/web && npm run test:e2e
```

## Chattfunktionalitet

Webbappen innehåller en ChatPanel för konversationsbaserad interaktion med grafen:

- **Sök och fråga**: "Hitta AI-projekt", "Visa aktörer i eSam"
- **Lägg till noder**: "Lägg till ett initiativ om digital identitet"
- **Nodförslag**: LLM föreslår noder med dublettkontroll, användaren måste bekräfta
- **Dokumentuppladdning**: Ladda upp PDF, Word eller textfiler för analys

### ChatGPT-widget

Samma chattfunktionalitet finns som en inbäddningsbar widget (`apps/widget/`). Widgeten kan bäddas in i ChatGPT eller andra gränssnitt som stöder custom widgets.

## Metamodell

### Node-typer
- **Actor** - Myndigheter, organisationer
- **Community** - eSam, Myndigheter, etc.
- **Initiative** - Projekt
- **Capability** - Förmågor
- **Resource** - Rapporter, mjukvara
- **Legislation** - NIS2, GDPR
- **Theme** - AI, datastrategier
- **VisualizationView** - Färdiga vyer

### Relationships
- BELONGS_TO, IMPLEMENTS, PRODUCES, GOVERNED_BY, RELATES_TO, PART_OF

## Exempel-data

Se `graph.json` för exempel med svenska myndigheter, NIS2-initiativ, etc.

## Säkerhet

- Max 10 noder per delete
- Dubbelkonfirmation krävs för deletion
- Community-baserad filtrering
- Ingen hantering av personuppgifter
