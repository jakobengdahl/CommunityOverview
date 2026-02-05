# Deployment Guide

> Leverantörsoberoende guide för att deploya CommunityOverview

---

## Innehåll

1. [Översikt](#1-översikt)
2. [Förberedelser](#2-förberedelser)
3. [Lokal Deployment (Docker)](#3-lokal-deployment-docker)
4. [Cloud Deployment](#4-cloud-deployment)
   - [Google Cloud Run](#41-google-cloud-run)
   - [DigitalOcean App Platform](#42-digitalocean-app-platform)
   - [Fly.io](#43-flyio)
   - [Railway](#44-railway)
   - [AWS (ECS/Fargate)](#45-aws-ecsfargate)
5. [CI/CD Pipeline](#5-cicd-pipeline)
6. [Uppdateringar & Underhåll](#6-uppdateringar--underhåll)
7. [Felsökning](#7-felsökning)

---

## 1. Översikt

CommunityOverview är paketerat som en Docker-container som kan köras på vilken plattform som helst som stödjer containers:

```
┌─────────────────────────────────────────────────┐
│  Docker Image (ghcr.io/user/communityoverview)  │
│  ├── Frontend (React)                           │
│  ├── Backend (FastAPI/Python)                   │
│  └── MCP Server                                 │
└─────────────────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
  ┌──────────┐ ┌──────────┐ ┌──────────┐
  │  Docker  │ │  Cloud   │ │   K8s    │
  │  Compose │ │   Run    │ │  Cluster │
  └──────────┘ └──────────┘ └──────────┘
```

### Arkitektur

| Komponent | Beskrivning |
|-----------|-------------|
| **Frontend** | React SPA serverad av FastAPI |
| **Backend** | FastAPI med REST API + SSE |
| **MCP Server** | Model Context Protocol för AI-integrationer |
| **Data** | JSON-fil (graph.json) med fillåsning |

### Krav

- **RAM:** Minimum 512MB, rekommenderat 2GB
- **CPU:** 1 vCPU minimum
- **Disk:** 1GB för applikation + data
- **Port:** 8000 (konfigurerbar)

---

## 2. Förberedelser

### 2.1 Hämta Koden

```bash
git clone https://github.com/jakobengdahl/CommunityOverview.git
cd CommunityOverview
```

### 2.2 Konfigurera Miljövariabler

Kopiera och redigera `.env`:

```bash
cp .env.example .env
```

**Obligatoriska variabler:**

```bash
# LLM Provider (välj en)
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-api03-xxxxx

# ELLER
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxx

# Autentisering (rekommenderas för produktion)
AUTH_ENABLED=true
AUTH_USERNAME=admin
AUTH_PASSWORD=<starkt-lösenord>
```

### 2.3 Bygg Docker Image Lokalt (Valfritt)

```bash
docker build -t communityoverview:latest .
```

---

## 3. Lokal Deployment (Docker)

### Utveckling

```bash
# Starta med docker compose
docker compose up -d

# Visa loggar
docker compose logs -f

# Stoppa
docker compose down
```

### Produktion (Self-hosted)

```bash
# Använd produktionskonfiguration
docker compose -f docker-compose.prod.yml up -d
```

Produktionskonfigurationen inkluderar:
- Resursbegränsningar (CPU/RAM)
- Automatisk omstart
- Loggrotation
- Named volumes för data

---

## 4. Cloud Deployment

### 4.1 Google Cloud Run

**Fördelar:** Automatisk skalning, pay-per-use, managed SSL

**Förberedelser:**

```bash
# Installera gcloud CLI
# https://cloud.google.com/sdk/docs/install

# Logga in och sätt projekt
gcloud auth login
gcloud config set project DITT_PROJEKT_ID

# Aktivera tjänster
gcloud services enable run.googleapis.com artifactregistry.googleapis.com
```

**Deployment:**

```bash
# Alternativ 1: Bygg och deploya direkt
gcloud run deploy communityoverview \
  --source . \
  --region europe-north1 \
  --allow-unauthenticated \
  --port 8000 \
  --memory 2Gi \
  --cpu 1 \
  --set-env-vars "LLM_PROVIDER=claude,AUTH_ENABLED=true" \
  --set-env-vars "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
  --set-env-vars "AUTH_PASSWORD=$AUTH_PASSWORD"

# Alternativ 2: Använd förbyggd image från GHCR
gcloud run deploy communityoverview \
  --image ghcr.io/jakobengdahl/communityoverview:latest \
  --region europe-north1 \
  --allow-unauthenticated \
  --port 8000 \
  --memory 2Gi
```

**Persistent Storage (Cloud Run + Cloud Storage):**

Cloud Run är stateless. För persistent data, använd Cloud Storage:

```bash
# Skapa bucket
gsutil mb -l europe-north1 gs://ditt-projekt-data

# Montera via Cloud Run (kräver 2nd gen)
gcloud run deploy communityoverview \
  --execution-environment gen2 \
  --add-volume name=data,type=cloud-storage,bucket=ditt-projekt-data \
  --add-volume-mount volume=data,mount-path=/data
```

---

### 4.2 DigitalOcean App Platform

**Fördelar:** Enkel setup, integrerad databas, managed SSL

**Via UI:**

1. Gå till [DigitalOcean App Platform](https://cloud.digitalocean.com/apps)
2. Klicka "Create App"
3. Välj "GitHub" som källa
4. Välj repository och branch
5. Konfigurera miljövariabler
6. Deploy

**Via CLI:**

```bash
# Installera doctl
brew install doctl  # eller apt install doctl

# Logga in
doctl auth init

# Skapa app.yaml
cat > .do/app.yaml << 'EOF'
name: communityoverview
services:
  - name: web
    dockerfile_path: Dockerfile
    http_port: 8000
    instance_count: 1
    instance_size_slug: basic-xs
    envs:
      - key: LLM_PROVIDER
        value: claude
      - key: ANTHROPIC_API_KEY
        type: SECRET
        value: ${ANTHROPIC_API_KEY}
      - key: AUTH_ENABLED
        value: "true"
      - key: AUTH_PASSWORD
        type: SECRET
        value: ${AUTH_PASSWORD}
EOF

# Deploya
doctl apps create --spec .do/app.yaml
```

---

### 4.3 Fly.io

**Fördelar:** Global edge deployment, enkel skalning, persistent volumes

**Setup:**

```bash
# Installera flyctl
curl -L https://fly.io/install.sh | sh

# Logga in
fly auth login

# Initiera app (första gången)
fly launch --no-deploy
```

**Skapa fly.toml:**

```toml
app = "communityoverview"
primary_region = "arn"  # Stockholm

[build]
  dockerfile = "Dockerfile"

[env]
  LLM_PROVIDER = "claude"
  AUTH_ENABLED = "true"
  PORT = "8000"

[http_service]
  internal_port = 8000
  force_https = true
  auto_start_machines = true
  auto_stop_machines = true
  min_machines_running = 1

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory_mb = 1024

[mounts]
  source = "data"
  destination = "/data"
```

**Deployment:**

```bash
# Sätt secrets
fly secrets set ANTHROPIC_API_KEY=sk-ant-xxx AUTH_PASSWORD=xxx

# Skapa volume för data
fly volumes create data --region arn --size 1

# Deploya
fly deploy
```

---

### 4.4 Railway

**Fördelar:** Extremt enkel, GitHub-integration, automatisk SSL

**Via UI:**

1. Gå till [Railway](https://railway.app)
2. "New Project" → "Deploy from GitHub repo"
3. Välj repository
4. Lägg till miljövariabler i Settings
5. Deployment sker automatiskt

**Via CLI:**

```bash
# Installera Railway CLI
npm install -g @railway/cli

# Logga in
railway login

# Länka till projekt
railway link

# Sätt variabler
railway variables set LLM_PROVIDER=claude
railway variables set ANTHROPIC_API_KEY=sk-ant-xxx
railway variables set AUTH_ENABLED=true
railway variables set AUTH_PASSWORD=xxx

# Deploya
railway up
```

---

### 4.5 AWS (ECS/Fargate)

**Fördelar:** Enterprise-grad, full kontroll, integrerat med AWS-ekosystemet

**Förberedelser:**

```bash
# Installera AWS CLI
# https://aws.amazon.com/cli/

# Konfigurera
aws configure
```

**Skapa ECS Task Definition (task-definition.json):**

```json
{
  "family": "communityoverview",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::ACCOUNT:role/ecsTaskExecutionRole",
  "containerDefinitions": [
    {
      "name": "app",
      "image": "ghcr.io/jakobengdahl/communityoverview:latest",
      "portMappings": [
        {
          "containerPort": 8000,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {"name": "LLM_PROVIDER", "value": "claude"},
        {"name": "AUTH_ENABLED", "value": "true"}
      ],
      "secrets": [
        {
          "name": "ANTHROPIC_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:anthropic-key"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/communityoverview",
          "awslogs-region": "eu-north-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

**Deployment:**

```bash
# Registrera task definition
aws ecs register-task-definition --cli-input-json file://task-definition.json

# Skapa service
aws ecs create-service \
  --cluster default \
  --service-name communityoverview \
  --task-definition communityoverview \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],securityGroups=[sg-xxx],assignPublicIp=ENABLED}"
```

---

## 5. CI/CD Pipeline

### Automatisk Build & Publish

Projektet inkluderar GitHub Actions workflows:

| Workflow | Fil | Beskrivning |
|----------|-----|-------------|
| CI | `.github/workflows/ci.yml` | Tester + Docker build + push till GHCR |
| Deploy | `.github/workflows/deploy.yml` | Deploy till valfri plattform |

### Aktivera CI/CD

1. **Pusha till GitHub** - CI körs automatiskt på main
2. **Image publiceras** till `ghcr.io/USERNAME/communityoverview:latest`
3. **Aktivera deployment** genom att redigera `.github/workflows/deploy.yml`

### Konfigurera Secrets

Gå till Repository → Settings → Secrets and variables → Actions:

| Secret | Beskrivning |
|--------|-------------|
| `GCP_PROJECT_ID` | Google Cloud projekt-ID |
| `GCP_SA_KEY` | Service account JSON (base64) |
| `DIGITALOCEAN_ACCESS_TOKEN` | DO API-token |
| `FLY_API_TOKEN` | Fly.io API-token |
| `RAILWAY_TOKEN` | Railway API-token |

---

## 6. Uppdateringar & Underhåll

### Manuell Uppdatering

```bash
# Hämta senaste kod
git pull origin main

# Bygg ny image
docker build -t communityoverview:latest .

# Starta om container
docker compose down && docker compose up -d
```

### Automatisk Uppdatering (Watchtower)

```bash
# Lägg till Watchtower för automatiska uppdateringar
docker run -d \
  --name watchtower \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower \
  --interval 3600 \
  communityoverview
```

### Cloud-specifika Uppdateringar

**Google Cloud Run:**
```bash
gcloud run deploy communityoverview \
  --image ghcr.io/jakobengdahl/communityoverview:latest
```

**Fly.io:**
```bash
fly deploy
```

**Railway:**
Automatiskt vid push till main (om konfigurerat).

---

## 7. Felsökning

### Container Startar Inte

```bash
# Kolla loggar
docker logs communityoverview

# Vanliga problem:
# - Saknad ANTHROPIC_API_KEY
# - Fel PORT-konfiguration
# - Permissions på /data-mappen
```

### Health Check Failar

```bash
# Testa manuellt
curl http://localhost:8000/health

# Förväntat svar:
# {"status":"healthy","graph_nodes":X,"graph_edges":Y}
```

### Data Försvinner (Cloud)

Cloud-plattformar är ofta stateless. Säkerställ:
- Volume mount är konfigurerad korrekt
- Cloud Storage/persistent disk är uppsatt
- Backup-strategi finns

### SSL/HTTPS Problem

De flesta cloud-plattformar hanterar SSL automatiskt. För self-hosted:

```bash
# Lägg till Caddy som reverse proxy
# Se docker-compose.prod.yml för exempel
```

---

## Sammanfattning

| Plattform | Komplexitet | Kostnad | Skalbarhet | Persistent Storage |
|-----------|-------------|---------|------------|-------------------|
| **Docker Compose** | Låg | Egen server | Manuell | Ja (volumes) |
| **Cloud Run** | Låg | Pay-per-use | Auto | Cloud Storage |
| **DigitalOcean** | Låg | ~$5/mån | Manuell | Ja |
| **Fly.io** | Låg | Free tier | Auto | Volumes |
| **Railway** | Mycket låg | Pay-per-use | Auto | Ja |
| **AWS ECS** | Hög | Variabel | Auto | EFS/EBS |

**Rekommendation för PoC:**
- **Enklast:** Railway eller Fly.io
- **Google-ekosystem:** Cloud Run
- **Full kontroll:** Docker Compose på egen server
