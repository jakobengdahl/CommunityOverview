# Analys: Samtidiga Användare & Deployment Setup

> Analysrapport för CommunityOverview-applikationen
> Datum: 2026-02-04

---

## Innehåll

1. [Samtidiga Användare - Analys](#1-samtidiga-användare---analys)
2. [Deployment Setup - Analys](#2-deployment-setup---analys)
3. [Google Cloud Deployment Guide](#3-google-cloud-deployment-guide)
4. [CI/CD-upplägg](#4-cicd-upplägg)
5. [Saknad Konfiguration i Docker](#5-saknad-konfiguration-i-docker)
6. [Rekommenderade Förändringar](#6-rekommenderade-förändringar)

---

## 1. Samtidiga Användare - Analys

### 1.1 Nuvarande Sessionshantering

**Status: INGEN SESSIONSHANTERING**

Applikationen använder enbart Basic Authentication utan sessionshantering:

```python
# backend/api_host/server.py (rad 66-105)
@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    # Endast username/password-kontroll per request
    # Ingen session skapas eller spåras
```

**Konsekvenser:**
- Användare med samma inloggningsuppgifter kan inte särskiljas
- Ingen spårning av vem som gjort vilka ändringar
- Ingen möjlighet till per-användare rate limiting
- Fungerar för single-user men problematisk för multi-user

### 1.2 Filåtkomst till graph.json

**Status: KRITISKA RACE CONDITIONS**

`GraphStorage`-klassen (`backend/core/storage.py`) har **inga låsmekanismer**:

```python
# Nuvarande implementation (rad 46-100)
def load(self) -> None:
    with open(self.json_path, 'r') as f:
        data = json.load(f)  # INGEN LÅS

def save(self) -> None:
    with open(self.json_path, 'w') as f:
        json.dump(data, f)  # INGEN LÅS
```

**Race Condition Scenario:**
```
Användare A: läser graph.json (inga noder)
Användare B: läser graph.json (inga noder)
Användare A: lägger till nod X, sparar (nu finns X)
Användare B: lägger till nod Y, sparar (skriver över - X FÖRLORAD!)
```

**Operationer som triggar save():**
| Operation | Antal saves | Risk |
|-----------|-------------|------|
| `add_nodes()` | 2 | Hög - dubbla skrivningar |
| `update_node()` | 1 | Medel |
| `delete_nodes()` | 1 | Hög - kan bryta relationer |

### 1.3 Delat State

**Status: SINGEL INSTANS FÖR ALLA ANVÄNDARE**

```python
# backend/api_host/server.py (rad 117-126)
graph_storage = GraphStorage(str(graph_path))  # EN instans
graph_service = GraphService(graph_storage)    # Delas av alla
app.state.graph_service = graph_service
```

**Problem:**
- Python-dictionaries (`self.nodes`, `self.edges`) är inte trådsäkra
- Vector store (embeddings) kan bli osynkroniserat
- Inga transaktioner - delvis state synligt för andra användare

### 1.4 SSE/MCP-anslutningar

Alla MCP-klienter delar samma `GraphService`:

```python
mcp = FastMCP(config.mcp_name, instructions=instructions)
tools_map = register_mcp_tools(mcp, graph_service)  # Samma instans
```

### 1.5 Sammanfattning - Samtidiga Användare

| Problem | Allvarlighet | Status |
|---------|--------------|--------|
| Inga fillås på graph.json | **KRITISK** | Datakorruption möjlig |
| Inget in-memory lås | **KRITISK** | Race conditions |
| Ingen sessionshantering | Medel | Kan ej särskilja användare |
| Inga transaktioner | Hög | Inkonsistent state |

**Slutsats:** Applikationen är **INTE säker för samtidiga användare**. Med 2-3 användare som redigerar grafen samtidigt är dataförlust högst sannolik.

---

## 2. Deployment Setup - Analys

### 2.1 Nuvarande Docker-konfiguration

**Dockerfile** - Multi-stage build:
```dockerfile
# Stage 1: Node.js för frontend-build
FROM node:18-alpine AS builder
RUN npm run build:web && npm run build:widget

# Stage 2: Python runtime
FROM python:3.11-slim
# ... kopierar backend + byggda frontend-assets
```

**docker-compose.yml:**
```yaml
services:
  app:
    build: .
    ports: ["8000:8000"]
    volumes: ["./data:/data"]
    environment:
      - GRAPH_FILE=/data/graph.json
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

### 2.2 Identifierade Problem

#### Kritiska Säkerhetsproblem

1. **CORS alltför permissiv:**
   ```python
   # backend/api_host/server.py
   allow_origins=["*"]       # Tillåter alla origins
   allow_credentials=True    # Säkerhetsproblem med wildcard
   ```

2. **Container kör som root:**
   ```dockerfile
   # Dockerfile saknar:
   # USER appuser  <- körs som root!
   ```

3. **API-nycklar i miljövariabler:**
   - Synliga via `docker inspect`
   - Ingen secrets management

#### Operationella Problem

1. **Ingen healthcheck i docker-compose:**
   ```yaml
   # Saknas:
   healthcheck:
     test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
   ```

2. **Ingen restart policy:**
   ```yaml
   # Saknas:
   restart: unless-stopped
   ```

3. **Inga resursbegränsningar:**
   ```yaml
   # Saknas:
   deploy:
     resources:
       limits:
         memory: 2G
   ```

### 2.3 Vad Fungerar Bra

- Multi-stage build minskar image-storlek
- Health endpoint finns (`/health`)
- Info endpoint finns (`/info`)
- Volume mount för data-persistens
- Environment variables för de flesta inställningar

---

## 3. Google Cloud Deployment Guide

### 3.1 Alternativ för Google Cloud

| Tjänst | Komplexitet | Kostnad | Skalbarhet |
|--------|-------------|---------|------------|
| **Cloud Run** | Låg | Låg (pay-per-use) | Auto |
| Cloud Compute Engine | Medel | Fast/timme | Manuell |
| Google Kubernetes Engine | Hög | Hög | Full kontroll |

**Rekommendation för PoC: Cloud Run**

### 3.2 Manuell Deployment till Google Cloud Run

#### Steg 1: Förberedelser

```bash
# Installera gcloud CLI
curl https://sdk.cloud.google.com | bash
gcloud init

# Skapa projekt (om det inte finns)
gcloud projects create community-overview-poc
gcloud config set project community-overview-poc

# Aktivera tjänster
gcloud services enable run.googleapis.com
gcloud services enable containerregistry.googleapis.com
gcloud services enable artifactregistry.googleapis.com
```

#### Steg 2: Bygg och pusha Docker-image

```bash
# Konfigurera Docker för Google Container Registry
gcloud auth configure-docker

# Bygg image
docker build -t gcr.io/community-overview-poc/app:v1 .

# Pusha till Google Container Registry
docker push gcr.io/community-overview-poc/app:v1
```

#### Steg 3: Skapa persistent storage (Cloud Storage)

```bash
# Skapa bucket för graph.json
gsutil mb -l europe-north1 gs://community-overview-data

# Ladda upp initial graph.json (om den finns)
gsutil cp data/graph.json gs://community-overview-data/
```

#### Steg 4: Deploya till Cloud Run

```bash
gcloud run deploy community-overview \
  --image gcr.io/community-overview-poc/app:v1 \
  --platform managed \
  --region europe-north1 \
  --allow-unauthenticated \
  --port 8000 \
  --memory 2Gi \
  --cpu 1 \
  --set-env-vars "ANTHROPIC_API_KEY=sk-ant-xxx" \
  --set-env-vars "AUTH_ENABLED=true" \
  --set-env-vars "AUTH_PASSWORD=ditt-lösenord"
```

**OBS:** Cloud Run är stateless - för persistent storage behövs:
- Google Cloud Storage med FUSE mount, eller
- Cloud SQL/Firestore för data

#### Steg 5: Konfigurera custom domain (valfritt)

```bash
gcloud run domain-mappings create \
  --service community-overview \
  --domain app.din-domän.se \
  --region europe-north1
```

### 3.3 Hantera Uppdateringar från Main

#### Manuellt

```bash
# Hämta senaste ändringar
git pull origin main

# Bygg ny version
docker build -t gcr.io/community-overview-poc/app:v2 .

# Pusha
docker push gcr.io/community-overview-poc/app:v2

# Deploya ny version
gcloud run deploy community-overview \
  --image gcr.io/community-overview-poc/app:v2 \
  --region europe-north1
```

### 3.4 Alternativ: Annan Hosting

#### DigitalOcean App Platform

```bash
# Installera doctl
brew install doctl
doctl auth init

# Deploya direkt från repo
doctl apps create --spec .do/app.yaml
```

#### Railway.app (Enklast)

```bash
# Installera Railway CLI
npm install -g @railway/cli
railway login

# Deploya
railway init
railway up
```

#### Fly.io

```bash
# Installera flyctl
curl -L https://fly.io/install.sh | sh
fly auth login

# Deploya
fly launch
fly deploy
```

---

## 4. CI/CD-upplägg

### 4.1 Enkel CI/CD med GitHub Actions

Skapa `.github/workflows/deploy.yml`:

```yaml
name: Deploy to Cloud Run

on:
  push:
    branches: [main]

env:
  PROJECT_ID: community-overview-poc
  REGION: europe-north1
  SERVICE: community-overview

jobs:
  deploy:
    runs-on: ubuntu-latest

    steps:
    - name: Checkout
      uses: actions/checkout@v4

    - name: Setup Google Cloud
      uses: google-github-actions/setup-gcloud@v2
      with:
        project_id: ${{ env.PROJECT_ID }}

    - name: Authenticate to Google Cloud
      uses: google-github-actions/auth@v2
      with:
        credentials_json: ${{ secrets.GCP_SA_KEY }}

    - name: Configure Docker
      run: gcloud auth configure-docker

    - name: Build and Push
      run: |
        docker build -t gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE }}:${{ github.sha }} .
        docker push gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE }}:${{ github.sha }}

    - name: Deploy to Cloud Run
      run: |
        gcloud run deploy ${{ env.SERVICE }} \
          --image gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE }}:${{ github.sha }} \
          --platform managed \
          --region ${{ env.REGION }} \
          --allow-unauthenticated

    - name: Show URL
      run: |
        gcloud run services describe ${{ env.SERVICE }} \
          --region ${{ env.REGION }} \
          --format 'value(status.url)'
```

### 4.2 Konfigurera Secrets i GitHub

```bash
# Skapa service account
gcloud iam service-accounts create github-actions

# Ge rättigheter
gcloud projects add-iam-policy-binding community-overview-poc \
  --member="serviceAccount:github-actions@community-overview-poc.iam.gserviceaccount.com" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding community-overview-poc \
  --member="serviceAccount:github-actions@community-overview-poc.iam.gserviceaccount.com" \
  --role="roles/storage.admin"

# Skapa nyckel
gcloud iam service-accounts keys create key.json \
  --iam-account=github-actions@community-overview-poc.iam.gserviceaccount.com

# Lägg till som GitHub secret (GCP_SA_KEY)
cat key.json | base64
```

Lägg sedan till i GitHub → Settings → Secrets → Actions:
- `GCP_SA_KEY`: Base64-encoded service account key
- `ANTHROPIC_API_KEY`: Din API-nyckel

### 4.3 Fullständig CI/CD Pipeline

```yaml
name: CI/CD Pipeline

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4

    - name: Setup Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.11'

    - name: Install dependencies
      run: |
        pip install -r backend/requirements.txt
        pip install pytest pytest-asyncio

    - name: Run tests
      run: pytest backend/tests/ -v

  build:
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'

    steps:
    - uses: actions/checkout@v4

    - name: Build Docker image
      run: docker build -t app:${{ github.sha }} .

    - name: Push to registry
      # ... (som ovan)

  deploy:
    needs: build
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'

    steps:
    - name: Deploy to production
      # ... (som ovan)
```

---

## 5. Saknad Konfiguration i Docker

### 5.1 Nuvarande vs Tillgängliga Variabler

| Variabel | I docker-compose? | I config.py? | Kommentar |
|----------|-------------------|--------------|-----------|
| `GRAPH_FILE` | ✅ | ✅ | |
| `ANTHROPIC_API_KEY` | ✅ | Via kod | |
| `OPENAI_API_KEY` | ❌ | Via kod | **SAKNAS** |
| `LLM_PROVIDER` | ❌ | Via kod | **SAKNAS** |
| `OPENAI_MODEL` | ❌ | Via kod | **SAKNAS** |
| `AUTH_ENABLED` | ⚠️ Kommenterad | ✅ | |
| `AUTH_USERNAME` | ⚠️ Kommenterad | ✅ | |
| `AUTH_PASSWORD` | ⚠️ Kommenterad | ✅ | |
| `SCHEMA_FILE` | ❌ | Via kod | **SAKNAS** |
| `EMBEDDINGS_FILE` | ❌ | ✅ | **SAKNAS** |
| `MCP_NAME` | ❌ | ✅ | **SAKNAS** |
| `API_PREFIX` | ❌ | ✅ | **SAKNAS** |
| `HOST` | ⚠️ I Dockerfile | ✅ | |
| `PORT` | ⚠️ I Dockerfile | ✅ | |

### 5.2 Föreslagen Uppdaterad docker-compose.yml

```yaml
services:
  app:
    build: .
    ports:
      - "${PORT:-8000}:8000"
    volumes:
      - ./data:/data
      - ./config:/app/config:ro  # Schema-config som volume
    environment:
      # === Data & Storage ===
      - GRAPH_FILE=/data/graph.json
      - EMBEDDINGS_FILE=/data/embeddings.pkl
      - SCHEMA_FILE=/app/config/schema_config.json

      # === LLM Provider ===
      - LLM_PROVIDER=${LLM_PROVIDER:-claude}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - OPENAI_MODEL=${OPENAI_MODEL:-gpt-4o}

      # === Authentication ===
      - AUTH_ENABLED=${AUTH_ENABLED:-false}
      - AUTH_USERNAME=${AUTH_USERNAME:-admin}
      - AUTH_PASSWORD=${AUTH_PASSWORD}

      # === Server ===
      - HOST=0.0.0.0
      - PORT=8000
      - API_PREFIX=${API_PREFIX:-/api}
      - MCP_NAME=${MCP_NAME:-community-knowledge-graph}

    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

    restart: unless-stopped

    deploy:
      resources:
        limits:
          memory: 2G
```

### 5.3 Skapa .env.example

```bash
# === LLM Provider Configuration ===
# Välj provider: "claude" eller "openai"
LLM_PROVIDER=claude

# Anthropic API Key (krävs om LLM_PROVIDER=claude)
ANTHROPIC_API_KEY=sk-ant-api03-xxx

# OpenAI API Key (krävs om LLM_PROVIDER=openai)
OPENAI_API_KEY=sk-xxx

# OpenAI Model (endast för OpenAI)
OPENAI_MODEL=gpt-4o

# === Authentication ===
AUTH_ENABLED=false
AUTH_USERNAME=admin
AUTH_PASSWORD=change-this-password

# === Data Paths ===
# Använd default /data/ i Docker
# GRAPH_FILE=/data/graph.json
# SCHEMA_FILE=/app/config/schema_config.json

# === Optional ===
# MCP_NAME=community-knowledge-graph
# API_PREFIX=/api
```

---

## 6. Rekommenderade Förändringar

### 6.1 Kritiska (Före Deployment)

1. **Uppdatera docker-compose.yml** med saknade variabler
2. **Skapa .env.example** för dokumentation
3. **Begränsa CORS** för produktion:
   ```python
   allow_origins=["https://din-domän.se"]
   ```
4. **Lägg till non-root user i Dockerfile**:
   ```dockerfile
   RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app /data
   USER appuser
   ```

### 6.2 Rekommenderat (För Bättre Stabilitet)

5. **Implementera fillås för graph.json**:
   ```python
   import fcntl

   def save(self):
       with open(self.json_path, 'w') as f:
           fcntl.flock(f, fcntl.LOCK_EX)
           try:
               json.dump(data, f)
           finally:
               fcntl.flock(f, fcntl.LOCK_UN)
   ```

6. **Lägg till asyncio.Lock för thread-safety**:
   ```python
   class GraphStorage:
       def __init__(self):
           self._lock = asyncio.Lock()

       async def add_nodes(self, ...):
           async with self._lock:
               # ... kritisk sektion
   ```

### 6.3 Framtida Förbättringar

7. **Byt från JSON till SQLite** för inbyggda transaktioner
8. **Lägg till reverse proxy** (nginx/Caddy) för SSL
9. **Implementera audit logging** för spårbarhet
10. **Lägg till backup-strategi** för data

---

## Sammanfattning

### Kan jag deploya nu?

**Ja, för PoC/single-user**, med följande begränsningar:
- ✅ Docker-upplägget fungerar
- ✅ Basic Auth finns
- ⚠️ Inte säkert för flera samtidiga användare
- ⚠️ Saknar några miljövariabler i docker-compose

### Vad behövs för produktion?

1. Fixa CORS-konfigurationen
2. Implementera fillås
3. Lägg till alla miljövariabler
4. Konfigurera HTTPS (via Cloud Run eller reverse proxy)
5. Överväg SQLite istället för JSON för bättre concurrency

### Snabbaste vägen till deployment

1. Uppdatera docker-compose.yml (se sektion 5.2)
2. Skapa .env-fil med dina API-nycklar
3. Kör `docker compose up -d`
4. Eller deploya till Cloud Run (se sektion 3.2)
