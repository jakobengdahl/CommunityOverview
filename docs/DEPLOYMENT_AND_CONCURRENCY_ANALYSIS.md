# Analys: Samtidiga Användare & Deployment Setup

> Analysrapport för CommunityOverview-applikationen
> Datum: 2026-02-04
> **Uppdaterad: 2026-02-04 - Concurrency-skydd implementerat**

---

## Innehåll

1. [Samtidiga Användare - Implementerat Skydd](#1-samtidiga-användare---implementerat-skydd)
2. [Deployment Setup - Analys](#2-deployment-setup---analys)
3. [Google Cloud Deployment Guide](#3-google-cloud-deployment-guide)
4. [CI/CD-upplägg](#4-cicd-upplägg)
5. [Saknad Konfiguration i Docker](#5-saknad-konfiguration-i-docker)
6. [Rekommenderade Förändringar](#6-rekommenderade-förändringar)

---

## 1. Samtidiga Användare - Implementerat Skydd

### 1.1 Implementerad Trådsäkerhet (PoC-nivå)

**Status: IMPLEMENTERAT**

Följande skyddsmekanismer har implementerats i `GraphStorage` (`backend/core/storage.py`):

#### Threading Lock (In-Memory Protection)

```python
class GraphStorage:
    def __init__(self, ...):
        # RLock tillåter samma tråd att ta låset flera gånger (reentrant)
        self._lock = threading.RLock()

    def add_nodes(self, nodes, edges):
        with self._lock:  # Skyddar hela operationen
            # ... all logik körs atomärt
            self.save()
```

**Skyddade operationer:**
- `load()` - Läsning av graph.json
- `save()` - Skrivning till graph.json
- `add_nodes()` - Lägga till noder och kanter
- `update_node()` - Uppdatera befintlig nod
- `delete_nodes()` - Ta bort noder

#### File Locking (Multi-Process Protection)

```python
# Cross-platform fillåsning
if sys.platform == 'win32':
    import msvcrt
    # Windows-specifik låsning
else:
    import fcntl
    # Unix/Linux/Mac låsning med flock()
```

**Funktioner:**
- Shared lock för läsning (flera kan läsa samtidigt)
- Exclusive lock för skrivning (endast en skrivare åt gången)
- Cross-platform stöd (Windows, Linux, macOS)

#### Atomic Writes (Corruption Prevention)

```python
def save(self):
    # 1. Skriv till temporär fil
    temp_fd, temp_path = tempfile.mkstemp(...)

    # 2. Synka till disk
    os.fsync(f.fileno())

    # 3. Atomic rename
    os.rename(temp_path, self.json_path)
```

**Fördelar:**
- Om processen dör mitt i skrivning förblir original-filen intakt
- Rename är atomärt på de flesta filsystem
- `fsync()` säkerställer att data når disk innan rename

### 1.2 Sessionshantering

**Status: GRUNDLÄGGANDE (Basic Auth)**

Applikationen använder Basic Authentication utan sessioner:
- Lämpligt för PoC med få användare
- Alla användare med samma credentials behandlas lika
- Ingen per-användare spårning

### 1.3 Kvarvarande Begränsningar

| Aspekt | Status | Kommentar |
|--------|--------|-----------|
| Trådsäkerhet | ✅ Implementerat | `threading.RLock` på alla mutationer |
| Fillåsning | ✅ Implementerat | `fcntl.flock` / `msvcrt.locking` |
| Atomic writes | ✅ Implementerat | Temp-fil + rename |
| Sessionshantering | ⚠️ Grundläggande | Basic Auth utan per-user tracking |
| Transaktioner | ⚠️ Ej stöd | Rollback vid fel ej implementerat |
| Multi-process | ✅ Fungerar | Fillås skyddar mellan processer |

### 1.4 Test Coverage

Concurrent access testas i `backend/core/tests/test_storage.py`:

```python
class TestGraphStorageConcurrency:
    def test_concurrent_add_nodes_no_data_loss()     # 10 trådar, 5 noder var
    def test_concurrent_update_nodes_no_data_loss()  # 10 trådar uppdaterar samma nod
    def test_concurrent_mixed_operations()           # 20 trådar med blandade operationer
    def test_atomic_save_prevents_corruption()       # 20 trådar sparar samtidigt
    def test_reload_during_concurrent_writes()       # Reload under pågående skrivningar
```

**Kör testerna:**
```bash
cd backend
pytest core/tests/test_storage.py::TestGraphStorageConcurrency -v
```

### 1.5 Sammanfattning

| Problem | Ursprunglig Status | Nuvarande Status |
|---------|-------------------|------------------|
| Inga fillås på graph.json | **KRITISK** | ✅ **ÅTGÄRDAT** |
| Inget in-memory lås | **KRITISK** | ✅ **ÅTGÄRDAT** |
| Filkorruption vid crash | **HÖG** | ✅ **ÅTGÄRDAT** |
| Ingen sessionshantering | Medel | ⚠️ Kvarstår (PoC-acceptabelt) |
| Inga transaktioner | Hög | ⚠️ Kvarstår (PoC-acceptabelt) |

**Slutsats:** Applikationen är nu **säker för samtidiga användare i PoC-nivå**. Flera användare kan arbeta med samma graf utan risk för dataförlust på grund av race conditions.

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
