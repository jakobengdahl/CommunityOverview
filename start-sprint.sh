#!/bin/bash
#
# Stockholm Sprint — Setup & Launch
#
# One-shot setup and launcher for the Stockholm sprint/hackathon environment.
# Installs all prerequisites automatically, configures the LLM connection
# interactively, and starts the app.
#
# Usage:
#   ./start-sprint.sh
#
# Re-run at any time to update LLM settings or restart the server.
#

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
DATA_DIR="$SCRIPT_DIR/data"
ACTIVE_DATA="$DATA_DIR/active/graph.json"
SPRINT_PROFILE="stockholmsprint"
SPRINT_CONFIG_DIR="$SCRIPT_DIR/config/$SPRINT_PROFILE"
SPRINT_ENV_FILE="$SPRINT_CONFIG_DIR/.env"

cd "$SCRIPT_DIR"

echo -e "${BOLD}${BLUE}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Community Knowledge Graph              ║"
echo "  ║   Stockholm Sprint — Setup & Launch      ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# =====================
# Node.js (auto-install)
# =====================
echo -e "${YELLOW}[1/5] Checking Node.js...${NC}"

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"

# Load nvm if available
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

if ! command -v node &>/dev/null; then
    echo "  Node.js not found. Installing nvm..."
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
    \. "$NVM_DIR/nvm.sh"
fi

# Ensure Node 20 is installed and active
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

if [ ! -f "$SCRIPT_DIR/venv/.sprint-deps-installed" ]; then
    echo "  Installing Python dependencies (first run downloads ~750 MB)..."
    pip install -r "$BACKEND_DIR/requirements.txt"
    touch "$SCRIPT_DIR/venv/.sprint-deps-installed"
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
    PROFILE_GRAPH="$SPRINT_CONFIG_DIR/graph.json"
    DEFAULT_EXAMPLE="$DATA_DIR/examples/default.json"
    if [ -f "$PROFILE_GRAPH" ]; then
        echo "  Loading sprint graph data..."
        cp "$PROFILE_GRAPH" "$ACTIVE_DATA"
    elif [ -f "$DEFAULT_EXAMPLE" ]; then
        echo "  Loading default example data..."
        cp "$DEFAULT_EXAMPLE" "$ACTIVE_DATA"
    else
        echo '{"nodes":[],"edges":[],"metadata":{"version":"1.0"}}' > "$ACTIVE_DATA"
    fi
fi

export GRAPH_FILE="$ACTIVE_DATA"

# =====================
# LLM configuration wizard
# =====================
echo -e "\n${YELLOW}[5/5] LLM configuration...${NC}"

mkdir -p "$SPRINT_CONFIG_DIR"

# Read a single value from the sprint .env file
_get_saved() {
    local key="$1"
    [ -f "$SPRINT_ENV_FILE" ] || { echo ""; return; }
    grep -E "^${key}=" "$SPRINT_ENV_FILE" 2>/dev/null | tail -1 \
        | cut -d'=' -f2- | sed 's/^["'"'"']//;s/["'"'"']$//'
}

# Prompt with current value shown; press Enter to keep
_prompt() {
    local label="$1"
    local key="$2"
    local default="$3"
    local current
    current=$(_get_saved "$key")
    current="${current:-$default}"

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
echo -e "  Press ${BOLD}Enter${NC} to keep the value shown in ${CYAN}[brackets]${NC}.\n"

OPENAI_BASE_URL=$(_prompt "API base URL" "OPENAI_BASE_URL" "")
OPENAI_API_KEY=$(_prompt "API key / token" "OPENAI_API_KEY" "")
OPENAI_MODEL=$(_prompt "Model name" "OPENAI_MODEL" "gpt-4o")
OPENAI_TOOL_CALLING=$(_prompt "Tool calling enabled? (true/false)" "OPENAI_TOOL_CALLING" "true")

# Validate base URL
if [ -z "$OPENAI_BASE_URL" ]; then
    echo -e "\n  ${YELLOW}Warning: No API base URL set. Chat will be unavailable.${NC}"
fi

# Write config
{
    echo "LLM_PROVIDER=openai"
    [ -n "$OPENAI_BASE_URL" ]     && echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"
    [ -n "$OPENAI_API_KEY" ]      && echo "OPENAI_API_KEY=$OPENAI_API_KEY"
    [ -n "$OPENAI_MODEL" ]        && echo "OPENAI_MODEL=$OPENAI_MODEL"
    echo "OPENAI_TOOL_CALLING=$OPENAI_TOOL_CALLING"
} > "$SPRINT_ENV_FILE"

echo -e "\n  ${GREEN}Configuration saved to config/stockholmsprint/.env${NC}"

# Load saved config into environment
# shellcheck source=/dev/null
source "$SPRINT_ENV_FILE"

# =====================
# Resolve profile config
# =====================
source "$SCRIPT_DIR/config/profile-utils.sh"

export CONFIG_PROFILE="$SPRINT_PROFILE"

RESOLVED_SCHEMA=$(resolve_config "$SPRINT_PROFILE" "schema_config.json")
[ -n "$RESOLVED_SCHEMA" ] && export SCHEMA_FILE="$RESOLVED_SCHEMA"

RESOLVED_FEDERATION=$(resolve_config "$SPRINT_PROFILE" "federation_config.json")
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
echo -e "  ${BLUE}Health:${NC}   http://localhost:8000/health"
echo ""
echo -e "  ${BLUE}Profile:${NC}  $SPRINT_PROFILE"
echo -e "  ${BLUE}Model:${NC}    ${OPENAI_MODEL}"
[ -n "$OPENAI_BASE_URL" ] && echo -e "  ${BLUE}Endpoint:${NC} $OPENAI_BASE_URL"
echo ""
echo -e "  Press ${BOLD}Ctrl+C${NC} to stop."
echo -e "${GREEN}  ══════════════════════════════════════════${NC}\n"

if [ -n "$CODESPACE_NAME" ]; then
    echo -e "${YELLOW}  Codespace detected: https://$CODESPACE_NAME-8000.app.github.dev/web/${NC}\n"
fi

trap '' INT
exec uvicorn backend.api_host.server:get_app --factory --reload --host 0.0.0.0 --port 8000 \
    --reload-dir backend
