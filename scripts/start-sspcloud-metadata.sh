#!/bin/bash
#
# SSPCloud — ESS Metadata Graph: Setup & Launch
#
# One-shot setup and launcher for the stat-metadata profile in the SSPCloud
# environment. Installs all prerequisites automatically, configures the LLM
# connection (reads from Vault-injected environment variables or prompts
# interactively), and starts the application.
#
# Usage:
#   ./scripts/start-sspcloud-metadata.sh
#
# Re-run at any time to update LLM settings or restart the server.
#
# Environment variables (injected automatically when using the SSPCloud
# Vault secret communityoverview-secrets):
#   OPENAI_API_KEY   — API key for the LLM endpoint
#   OPENAI_BASE_URL  — LLM base URL (default: https://llm.lab.sspcloud.fr/api)
#   OPENAI_MODEL     — model name  (default: gemma4-26b-moe)
#

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Repo root (this script lives in scripts/, so resolve one level up).
# config/profile-utils.sh expects SCRIPT_DIR to point at the repo root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
DATA_DIR="$SCRIPT_DIR/data"
ACTIVE_DATA="$DATA_DIR/active/graph.json"
PROFILE="stat-metadata"
PROFILE_CONFIG_DIR="$SCRIPT_DIR/config/$PROFILE"
PROFILE_ENV_FILE="$PROFILE_CONFIG_DIR/.env"

cd "$SCRIPT_DIR"

echo -e "${BOLD}${BLUE}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Community Knowledge Graph              ║"
echo "  ║   SSPCloud — ESS Metadata Graph          ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# =====================
# Node.js (auto-install)
# =====================
echo -e "${YELLOW}[1/5] Checking Node.js...${NC}"

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"

[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

if ! command -v node &>/dev/null; then
    echo "  Node.js not found. Installing nvm..."
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
    \. "$NVM_DIR/nvm.sh"
fi

if ! command -v node &>/dev/null || ! node -e "process.exit(parseInt(process.version.slice(1)) >= 18 ? 0 : 1)" 2>/dev/null; then
    echo "  Installing Node.js 20..."
    nvm install 20
    nvm use 20
else
    echo -e "  ${GREEN}Node.js $(node --version) ready.${NC}"
fi

# =====================
# Python environment
# =====================
echo -e "\n${YELLOW}[2/5] Setting up Python environment...${NC}"

if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "  Creating virtual environment..."
    python3 -m venv "$SCRIPT_DIR/venv"
fi

source "$SCRIPT_DIR/venv/bin/activate"

FLAG="$SCRIPT_DIR/venv/.sspcloud-metadata-deps-installed"
if [ ! -f "$FLAG" ]; then
    echo "  Installing Python dependencies (first run downloads ~750 MB for ML packages)..."
    pip install -r "$BACKEND_DIR/requirements.txt"
    touch "$FLAG"
else
    echo "  Checking for updates..."
    pip install -q -r "$BACKEND_DIR/requirements.txt"
fi

echo -e "  ${GREEN}Python environment ready.${NC}"

# =====================
# Node.js dependencies
# =====================
echo -e "\n${YELLOW}[3/5] Installing Node.js dependencies...${NC}"

if [ ! -d "$SCRIPT_DIR/node_modules" ]; then
    npm install
else
    echo -e "  ${GREEN}Node modules already installed.${NC}"
fi

# =====================
# Frontend build
# =====================
echo -e "\n${YELLOW}[4/5] Building frontend...${NC}"

if [ ! -f "$SCRIPT_DIR/frontend/web/dist/index.html" ]; then
    echo "  Building web app..."
    npm run build:web
    echo "  Building widget..."
    npm run build:widget
else
    echo -e "  ${GREEN}Frontend already built.${NC}"
    echo -e "  (Run 'npm run build:web' manually to force a rebuild.)"
fi

# =====================
# Graph data
# =====================
mkdir -p "$DATA_DIR/active" "$DATA_DIR/examples"

if [ ! -f "$ACTIVE_DATA" ]; then
    PROFILE_GRAPH="$PROFILE_CONFIG_DIR/graph.json"
    if [ -f "$PROFILE_GRAPH" ]; then
        echo "  Loading stat-metadata graph data..."
        cp "$PROFILE_GRAPH" "$ACTIVE_DATA"
    else
        echo '{"nodes":[],"edges":[],"metadata":{"version":"1.0"}}' > "$ACTIVE_DATA"
    fi
    # A journal left behind by a previous graph would be replayed onto this
    # seed at startup (docs/DATA_MANAGEMENT.md, Graph Journal). Only here,
    # after a replacement is in place - never on an ordinary start.
    rm -f "${ACTIVE_DATA%.*}.journal.ndjson"
fi

export GRAPH_FILE="$ACTIVE_DATA"

# =====================
# LLM configuration
# =====================
echo -e "\n${YELLOW}[5/5] LLM configuration...${NC}"

mkdir -p "$PROFILE_CONFIG_DIR"

# Priority: environment variable > saved .env value > default
_resolve() {
    local key="$1"
    local default="$2"
    local env_val="${!key}"
    if [ -n "$env_val" ]; then
        echo "$env_val"
        return
    fi
    if [ -f "$PROFILE_ENV_FILE" ]; then
        local saved
        saved=$(grep -E "^${key}=" "$PROFILE_ENV_FILE" 2>/dev/null | tail -1 \
            | cut -d'=' -f2- | sed 's/^["'"'"']//;s/["'"'"']$//')
        [ -n "$saved" ] && { echo "$saved"; return; }
    fi
    echo "$default"
}

if [ -n "$OPENAI_BASE_URL" ] && [ -n "$OPENAI_API_KEY" ]; then
    echo -e "  ${GREEN}LLM config loaded from environment variables — skipping wizard.${NC}"
    echo -e "  ${BLUE}Endpoint:${NC} $OPENAI_BASE_URL"
    echo -e "  ${BLUE}Model:${NC}    ${OPENAI_MODEL:-gemma4-26b-moe}"
    export LLM_PROVIDER=openai
    export OPENAI_MODEL="${OPENAI_MODEL:-gemma4-26b-moe}"
    export OPENAI_TOOL_CALLING="${OPENAI_TOOL_CALLING:-true}"
else
    _prompt() {
        local label="$1"
        local key="$2"
        local default="$3"

        if [ -n "${!key}" ]; then
            printf '%s' "${!key}"
            return
        fi

        local current
        current=$(_resolve "$key" "$default")

        local display="$current"
        if [[ "$key" == *KEY* ]] && [ -n "$current" ]; then
            display="${current:0:8}...${current: -4}"
        fi

        if [ -n "$current" ]; then
            printf "  %-36s ${CYAN}[%s]${NC}: " "$label" "$display"
        else
            printf "  %-36s: " "$label"
        fi

        read -r input
        printf '%s' "${input:-$current}"
    }

    echo -e "  Configure the OpenAI-compatible LLM endpoint."
    echo -e "  Press ${BOLD}Enter${NC} to keep the value shown in ${CYAN}[brackets]${NC}."
    echo -e "  Values set as environment variables are used automatically.\n"
    echo -e "  ${CYAN}SSPCloud defaults:${NC} https://llm.lab.sspcloud.fr/api · gemma4-26b-moe\n"

    OPENAI_BASE_URL=$(_prompt "API base URL" "OPENAI_BASE_URL" "https://llm.lab.sspcloud.fr/api")
    OPENAI_API_KEY=$(_prompt "API key / token" "OPENAI_API_KEY" "")
    OPENAI_MODEL=$(_prompt "Model name" "OPENAI_MODEL" "gemma4-26b-moe")
    OPENAI_TOOL_CALLING=$(_prompt "Tool calling enabled? (true/false)" "OPENAI_TOOL_CALLING" "true")

    if [ -z "$OPENAI_API_KEY" ]; then
        echo -e "\n  ${YELLOW}Warning: No API key set. Chat will be unavailable.${NC}"
    fi

    {
        echo "LLM_PROVIDER=openai"
        [ -n "$OPENAI_BASE_URL" ]    && echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"
        [ -n "$OPENAI_API_KEY" ]     && echo "OPENAI_API_KEY=$OPENAI_API_KEY"
        [ -n "$OPENAI_MODEL" ]       && echo "OPENAI_MODEL=$OPENAI_MODEL"
        echo "OPENAI_TOOL_CALLING=$OPENAI_TOOL_CALLING"
    } > "$PROFILE_ENV_FILE"

    echo -e "\n  ${GREEN}Configuration saved to config/stat-metadata/.env${NC}"

    export LLM_PROVIDER=openai
    export OPENAI_BASE_URL OPENAI_API_KEY OPENAI_MODEL OPENAI_TOOL_CALLING
fi

# =====================
# Resolve profile config
# =====================
source "$SCRIPT_DIR/config/profile-utils.sh"

export CONFIG_PROFILE="$PROFILE"

RESOLVED_SCHEMA=$(resolve_config "$PROFILE" "schema_config.json")
[ -n "$RESOLVED_SCHEMA" ] && export SCHEMA_FILE="$RESOLVED_SCHEMA"

RESOLVED_FEDERATION=$(resolve_config "$PROFILE" "federation_config.json")
[ -n "$RESOLVED_FEDERATION" ] && export FEDERATION_FILE="$RESOLVED_FEDERATION"

export APP_LANGUAGE="${APP_LANGUAGE:-en}"

# =====================
# Launch
# =====================
echo ""
echo -e "${GREEN}  ══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Ready — starting server on port 8000      ${NC}"
echo -e "${GREEN}  ══════════════════════════════════════════${NC}"
echo -e "  ${BLUE}Web app:${NC}  http://localhost:8000/web/"
echo -e "  ${BLUE}API:${NC}      http://localhost:8000/api/"
echo -e "  ${BLUE}MCP:${NC}      http://localhost:8000/mcp/sse"
echo -e "  ${BLUE}Health:${NC}   http://localhost:8000/health"
echo ""
echo -e "  ${BLUE}Profile:${NC}  $PROFILE"
echo -e "  ${BLUE}Model:${NC}    ${OPENAI_MODEL}"
[ -n "$OPENAI_BASE_URL" ] && echo -e "  ${BLUE}Endpoint:${NC} $OPENAI_BASE_URL"
echo ""
echo -e "  Press ${BOLD}Ctrl+C${NC} to stop."
echo -e "${GREEN}  ══════════════════════════════════════════${NC}\n"

if [ -n "$CODESPACE_NAME" ]; then
    echo -e "${YELLOW}  Codespace: https://$CODESPACE_NAME-8000.app.github.dev/web/${NC}\n"
fi

trap '' INT
exec uvicorn backend.api_host.server:get_app --factory --reload --host 0.0.0.0 --port 8000 \
    --reload-dir backend
