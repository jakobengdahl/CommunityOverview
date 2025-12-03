# Community Knowledge Sharing PoC

AI-powered kunskapsdelning för communities med graf-visualisering och konversationell chat.

## Översikt

Detta system hjälper organisationer att undvika överlappande investeringar genom att synliggöra:
- Pågående initiativ och projekt
- Resurser och förmågor
- Kopplingar mellan aktörer, lagstiftning och teman

**Teknisk stack:**
- **Frontend:** React + React Flow + Zustand
- **Backend:** MCP Server (Python) med NetworkX + JSON
- **AI:** Claude API för RAG och extraktion
- **Graf-lagring:** NetworkX in-memory + JSON persistens

## Projektstruktur

```
/frontend          # React app med graf-visualisering
/mcp-server        # Python MCP server med graf-logik
/docs              # Dokumentation och specifikationer
```

## Metamodell

### Node-typer
- **Actor** (blue) - Myndigheter, organisationer
- **Community** (purple) - eSam, Myndigheter, Officiell Statistik
- **Initiative** (green) - Projekt, gruppverksamheter
- **Capability** (orange) - Upphandling, IT-utveckling, portföljhantering
- **Resource** (yellow) - Rapporter, mjukvarukomponenter
- **Legislation** (red) - NIS2, GDPR, etc.
- **Theme** (teal) - AI, datastrategier, förändringsledning
- **VisualizationView** (gray) - Färdiga vyer för navigation

### Relationships
- BELONGS_TO, IMPLEMENTS, PRODUCES, GOVERNED_BY, RELATES_TO, PART_OF

## Snabbstart

### Lokal utveckling

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

### Med Docker Compose
```bash
docker-compose up
```

### GitHub Codespaces
Öppna projektet i Codespaces - allt är förkonfigurerat.

## Användarscenario

1. **Söka:** Användare söker efter initiativ kopplade till NIS2
2. **Visualisera:** Graf visar kopplingar mellan initiativ, aktörer och lagstiftning
3. **Upptäcka gap:** Användare ser att deras projekt saknas
4. **Ladda upp:** Användare laddar upp projektrapport
5. **Granska:** System föreslår noder och kopplingar
6. **Godkänna:** Användare granskar och godkänner tillägg

## Säkerhet

- Max 10 noder per delete-operation
- Community-baserad isolation
- Varning mot hantering av personuppgifter
- Audit log för deletions

## Development Status

Se [Implementation Roadmap](docs/roadmap.md) för detaljerad progress.

**Current Phase:** Fas 1 - Grundläggande infrastruktur

## License

MIT License - se LICENSE för detaljer
