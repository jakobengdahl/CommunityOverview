# Deployment Guide

> Provider-agnostic guide for deploying CommunityOverview

---

## Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Local Deployment (Docker)](#3-local-deployment-docker)
4. [Cloud Deployment](#4-cloud-deployment)
   - [Google Cloud Run](#41-google-cloud-run)
   - [DigitalOcean App Platform](#42-digitalocean-app-platform)
   - [Fly.io](#43-flyio)
   - [Railway](#44-railway)
   - [AWS (ECS/Fargate)](#45-aws-ecsfargate)
5. [CI/CD Pipeline](#5-cicd-pipeline)
6. [Updates & Maintenance](#6-updates--maintenance)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Overview

CommunityOverview is packaged as a Docker container that can run on any platform that supports containers:

```
┌─────────────────────────────────────────────────┐
│  Docker Image (ghcr.io/owner/communityoverview)  │
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

### Architecture

| Component | Description |
|-----------|-------------|
| **Frontend** | React SPA served by FastAPI |
| **Backend** | FastAPI with REST API + SSE |
| **MCP Server** | Model Context Protocol for AI integrations |
| **Data** | JSON file (graph.json) with file locking |

### Requirements

- **RAM:** Minimum 512 MB, recommended 2 GB
- **CPU:** 1 vCPU minimum
- **Disk:** 1 GB for application + data
- **Port:** 8000 (configurable)

---

## 2. Prerequisites

### 2.1 Clone the Repository

```bash
git clone https://github.com/jakobengdahl/CommunityOverview.git
cd CommunityOverview
```

### 2.2 Configure Environment Variables

Copy and edit `.env`:

```bash
cp config/default/.env.example config/default/.env
```

**Required variables:**

```bash
# LLM Provider (choose one)
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-api03-xxxxx

# OR
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxx

# Authentication (recommended for production)
AUTH_ENABLED=true
AUTH_USERNAME=admin
AUTH_PASSWORD=<strong-password>
```

### 2.3 Build Docker Image Locally (Optional)

```bash
docker build -t communityoverview:latest .
```

---

## 3. Local Deployment (Docker)

### Development

```bash
# Start with docker compose
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

### Production (Self-hosted)

```bash
# Use production configuration
docker compose -f docker-compose.prod.yml up -d
```

The production configuration includes:
- Resource limits (CPU/RAM)
- Automatic restart
- Log rotation
- Named volumes for data

---

## 4. Cloud Deployment

### 4.1 Google Cloud Run

**Benefits:** Automatic scaling, pay-per-use, managed SSL

**Prerequisites:**

```bash
# Install gcloud CLI
# https://cloud.google.com/sdk/docs/install

# Log in and set project
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# Enable services
gcloud services enable run.googleapis.com artifactregistry.googleapis.com
```

**Deployment:**

```bash
# Option 1: Build and deploy directly from source
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

# Option 2: Use pre-built image from GHCR
gcloud run deploy communityoverview \
  --image ghcr.io/jakobengdahl/communityoverview:latest \
  --region europe-north1 \
  --allow-unauthenticated \
  --port 8000 \
  --memory 2Gi
```

**Persistent Storage (Cloud Run + Cloud Storage):**

Cloud Run is stateless. For persistent data, use Cloud Storage:

```bash
# Create bucket
gcloud storage buckets create gs://your-project-data --location=europe-north1

# Mount via Cloud Run (requires 2nd gen)
gcloud run deploy communityoverview \
  --execution-environment gen2 \
  --add-volume name=data,type=cloud-storage,bucket=your-project-data \
  --add-volume-mount volume=data,mount-path=/data
```

> **Production deployments:** For managed pilot deployments, the infra repository
> handles Cloud Run configuration, GCS volumes, and per-pilot secrets. See
> [DEPLOYMENT_CONTRACT.md](./DEPLOYMENT_CONTRACT.md) for the image artifact interface.

---

### 4.2 DigitalOcean App Platform

**Benefits:** Simple setup, integrated database, managed SSL

**Via UI:**

1. Go to [DigitalOcean App Platform](https://cloud.digitalocean.com/apps)
2. Click "Create App"
3. Select "GitHub" as source
4. Select repository and branch
5. Configure environment variables
6. Deploy

**Via CLI:**

```bash
# Install doctl
brew install doctl  # or apt install doctl

# Log in
doctl auth init

# Create app.yaml
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

# Deploy
doctl apps create --spec .do/app.yaml
```

---

### 4.3 Fly.io

**Benefits:** Global edge deployment, simple scaling, persistent volumes

**Setup:**

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Log in
fly auth login

# Initialize app (first time only)
fly launch --no-deploy
```

**Create fly.toml:**

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
# Set secrets
fly secrets set ANTHROPIC_API_KEY=sk-ant-xxx AUTH_PASSWORD=xxx

# Create volume for data
fly volumes create data --region arn --size 1

# Deploy
fly deploy
```

---

### 4.4 Railway

**Benefits:** Extremely simple, GitHub integration, automatic SSL

**Via UI:**

1. Go to [Railway](https://railway.app)
2. "New Project" → "Deploy from GitHub repo"
3. Select repository
4. Add environment variables in Settings
5. Deployment happens automatically

**Via CLI:**

```bash
# Install Railway CLI
npm install -g @railway/cli

# Log in
railway login

# Link to project
railway link

# Set variables
railway variables set LLM_PROVIDER=claude
railway variables set ANTHROPIC_API_KEY=sk-ant-xxx
railway variables set AUTH_ENABLED=true
railway variables set AUTH_PASSWORD=xxx

# Deploy
railway up
```

---

### 4.5 AWS (ECS/Fargate)

**Benefits:** Enterprise-grade, full control, integrated with the AWS ecosystem

**Prerequisites:**

```bash
# Install AWS CLI
# https://aws.amazon.com/cli/

# Configure
aws configure
```

**Create ECS Task Definition (task-definition.json):**

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
# Register task definition
aws ecs register-task-definition --cli-input-json file://task-definition.json

# Create service
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

### Automatic Build & Publish

The project includes GitHub Actions workflows. Images are published to GitHub Container Registry (GHCR) on every successful push to `preview` or `prod` (and on version tags). `main` is the integration branch — pushes to it run the tests but publish no image.

| Workflow | File | Description |
|----------|------|-------------|
| CI | `.github/workflows/ci.yml` | Tests + Docker build + push to GHCR + notify infra repo |
| Deploy | `.github/workflows/deploy.yml` | Deployment stub (actual deployments managed by infra repo) |

### Published Images

| Image | Registry path |
|-------|---------------|
| Core application | `ghcr.io/jakobengdahl/communityoverview` |
| MCP OAuth Gateway | `ghcr.io/jakobengdahl/communityoverview-gateway` |

### Image Tags

| Tag | Description |
|-----|-------------|
| `sha-<commit>` | Immutable — canonical reference for a specific build |
| `dev` | Floating — latest successful build from `preview` branch |
| `latest` | Floating — latest successful build from `prod` branch |

### How a Merge Flows

```
Developer opens a PR → merges to main (integration)
         │
         ▼
CI: tests run — no image is built on main
         │
         ▼
Owner promotes main → preview  (deployment action)
         │
         ▼
CI on preview: build job (on success)
  – builds core image → ghcr.io/jakobengdahl/communityoverview:sha-<sha>
  – builds gateway image → ghcr.io/jakobengdahl/communityoverview-gateway:sha-<sha>
  – pushes floating tag: dev
         │
         ▼
CI: notify-infra job (if INFRA_DISPATCH_TOKEN configured)
  – sends repository_dispatch app-release, channel=preview, to infra repo
         │
         ▼
Owner promotes preview → prod  → same flow with channel=prod, floating tag: latest
```

See [DEPLOYMENT_CONTRACT.md](./DEPLOYMENT_CONTRACT.md) for the full artifact interface specification.

---

## 6. Updates & Maintenance

### Manual Update

```bash
# Pull latest code
git pull origin main

# Build new image
docker build -t communityoverview:latest .

# Restart container
docker compose down && docker compose up -d
```

### Automatic Updates (Watchtower)

```bash
# Add Watchtower for automatic updates
docker run -d \
  --name watchtower \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower \
  --interval 3600 \
  communityoverview
```

### Cloud-specific Updates

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
Automatic on push to main (if configured).

---

## 7. Troubleshooting

### Container Won't Start

```bash
# Check logs
docker logs communityoverview

# Common causes:
# - Missing ANTHROPIC_API_KEY or OPENAI_API_KEY
# - Wrong PORT configuration
# - Permissions on /data directory
```

Note: the application starts without any LLM key — the chat panel is simply hidden. See [LLM_PROVIDERS.md](../LLM_PROVIDERS.md).

### Health Check Fails

```bash
# Test manually
curl http://localhost:8000/health

# Expected response:
# {"status":"healthy","graph_nodes":X,"graph_edges":Y}
```

### Data Disappears (Cloud)

Cloud platforms are often stateless. Ensure:
- Volume mount is configured correctly
- Cloud Storage / persistent disk is set up
- A backup strategy is in place

### SSL/HTTPS Issues

Most cloud platforms handle SSL automatically. For self-hosted setups, add a reverse proxy such as Caddy or nginx in front of the application.

---

## Summary

| Platform | Complexity | Cost | Scalability | Persistent Storage |
|-----------|------------|------|------------|-------------------|
| **Docker Compose** | Low | Own server | Manual | Yes (volumes) |
| **Cloud Run** | Low | Pay-per-use | Auto | Cloud Storage |
| **DigitalOcean** | Low | ~$5/month | Manual | Yes |
| **Fly.io** | Low | Free tier | Auto | Volumes |
| **Railway** | Very low | Pay-per-use | Auto | Yes |
| **AWS ECS** | High | Variable | Auto | EFS/EBS |

**Recommendation for PoC / first deployment:**
- **Simplest:** Railway or Fly.io
- **Google ecosystem:** Cloud Run
- **Full control:** Docker Compose on own server
