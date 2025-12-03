# Snabbstart

Kom igång med Community Knowledge Graph på 5 minuter.

## Förutsättningar

- Python 3.11+
- Node.js 20+
- Docker (optional, för enklare setup)

## Alternativ 1: Docker Compose (Rekommenderat)

```bash
# Klona repo
git clone <repo-url>
cd CommunityOverview

# Starta allt med Docker Compose
docker-compose up

# Öppna i browser
# Frontend: http://localhost:3000
# MCP Server: http://localhost:8000
```

## Alternativ 2: Lokal utveckling

### Starta MCP Server

```bash
cd mcp-server

# Skapa virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Installera dependencies
pip install -r requirements.txt

# Testa graf-lagring
python test_graph.py

# Starta MCP server
python server.py
```

### Starta Frontend

```bash
cd frontend

# Installera dependencies
npm install

# Starta dev server
npm run dev

# Öppna http://localhost:3000
```

## Första stegen

1. **Välj community:** Klicka på dropdown i headern och välj "eSam"

2. **Ställ en fråga i chatten:**
   - "Vilka initiativ rör NIS2?"
   - "Visa alla aktörer i eSam"

3. **Utforska grafen:**
   - Använd scroll för zoom
   - Dra för pan
   - Klicka på nod för detaljer
   - Hover på nod → klicka [+] för relaterade noder (TODO)

## Exempel-data

Projektet kommer med förinstallerad data:

- **3 Communities:** eSam, Myndigheter, Officiell Statistik
- **3 Aktörer:** DIGG, MSB, SCB
- **2 Lagstiftningar:** NIS2, GDPR
- **3 Initiativ:** NIS2 Implementering, Cybersäkerhetssamverkan, Dataskyddsstrategi
- **2 Teman:** Cybersäkerhet, Dataskydd

Se `mcp-server/graph.json` för fullständig data.

## GitHub Codespaces

Klicka på "Code" → "Codespaces" → "Create codespace on main"

Allt är förkonfigurerat via `.devcontainer/devcontainer.json`.

## Nästa steg

- Läs [Architecture](architecture.md) för systemöversikt
- Se [Roadmap](roadmap.md) för planerade features
- Kolla [Frontend README](../frontend/README.md) för komponent-dokumentation
- Kolla [MCP Server README](../mcp-server/README.md) för API-dokumentation

## Felsökning

### MCP server startar inte
```bash
# Kontrollera att graph.json finns
ls mcp-server/graph.json

# Testa utan MCP
cd mcp-server
python test_graph.py
```

### Frontend visar fel
```bash
# Rensa node_modules och reinstallera
cd frontend
rm -rf node_modules
npm install
```

### Docker-problem
```bash
# Rebuild containers
docker-compose down
docker-compose build --no-cache
docker-compose up
```

## Support

Öppna ett issue på GitHub eller kontakta maintainers.
