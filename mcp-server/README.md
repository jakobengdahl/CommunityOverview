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

### Starta MCP server
```bash
python server.py
```

### Kör tester
```bash
python test_graph.py
```

### Kör pytest
```bash
pytest
```

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
