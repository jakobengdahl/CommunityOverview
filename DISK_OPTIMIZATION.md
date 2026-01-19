# Disk Space Optimization for GitHub Codespaces

GitHub Codespaces har begränsat diskutrymme (32GB). Detta repo är optimerat för att fungera inom dessa gränser.

## Optimeringar vi har gjort

### 1. PyTorch CPU-only (~70% mindre)
- **Standard PyTorch:** ~4-5GB (inkluderar CUDA för GPU)
- **CPU-only PyTorch:** ~800MB
- **Implementerat i:** `mcp-server/requirements.txt:32`

```txt
--extra-index-url https://download.pytorch.org/whl/cpu
torch>=2.0.0
```

### 2. Ingen pip cache
- **Standard installation:** Sparar 1-2GB cache
- **Med --no-cache-dir:** 0GB cache
- **Implementerat i:**
  - `.devcontainer/devcontainer.json:37`
  - `start-dev.sh:73`

### 3. Separata dev dependencies
- **Test/dev tools:** Flyttade till `requirements-dev.txt`
- **Spar:** ~200-300MB om du inte kör tester

## Diskutrymme efter installation

```
Komponent                   Storlek
─────────────────────────────────────
Python venv                 ~2.5GB
├─ PyTorch CPU-only        ~800MB
├─ sentence-transformers   ~400MB
├─ Other ML packages       ~300MB
└─ Core dependencies       ~1GB

Node modules                ~400MB
├─ React & dependencies    ~250MB
└─ Dev tools               ~150MB

Totalt                      ~3GB
Ledigt utrymme              ~29GB
```

## Om du får diskutrymme-problem

### Kontrollera utrymme
```bash
df -h
du -sh /workspaces/CommunityOverview/*
```

### Rensa caches
```bash
# Pip cache (om --no-cache-dir inte användes)
pip cache purge

# NPM cache
npm cache clean --force

# Python bytecode
find . -type d -name __pycache__ -exec rm -rf {} +
find . -type f -name "*.pyc" -delete
```

### Rensa gamla venv
```bash
# Om du behöver börja om
cd /workspaces/CommunityOverview/mcp-server
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install --no-cache-dir -r requirements.txt
```

### Rensa node_modules
```bash
cd /workspaces/CommunityOverview/frontend
rm -rf node_modules
npm install
```

## Extrema åtgärder (om fortfarande problem)

### Använd system Python istället för venv
```bash
# VARNING: Kan orsaka konflikter med system-paket
pip install --no-cache-dir --user -r mcp-server/requirements.txt
```

### Minimal installation (bara core, inga ML-features)
```bash
# OBS: Detta bryter similarity search funktionalitet!
# Installera bara core dependencies utan sentence-transformers
pip install --no-cache-dir mcp fastmcp pydantic networkx fastapi uvicorn anthropic openai
```

## Prestandatips

- **CPU-only PyTorch:** Använder CPU för ML-inferens
  - Första embedding: ~2-3 sekunder
  - Senare embeddings: ~0.5 sekunder (cached model)
  - Helt ok för de flesta use cases!

- **Alternativ modell:** Om all-MiniLM-L6-v2 är för stor, prova:
  - `paraphrase-MiniLM-L3-v2` (ännu mindre, lite sämre kvalitet)
  - Ändra i `mcp-server/vector_store.py:24`

## Monitorera diskutrymme automatiskt

Lägg till i din shell config (`.bashrc` eller `.zshrc`):
```bash
alias diskcheck='df -h | grep -E "overlay|/workspaces" && du -sh /workspaces/CommunityOverview/{mcp-server/venv,frontend/node_modules}'
```

Kör sedan bara: `diskcheck`
