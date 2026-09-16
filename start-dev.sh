#!/bin/bash
#
# Start Development Environment
# This script sets up and starts all services needed to run the full application.
#
# Usage:
#   ./start-dev.sh [OPTIONS]
#
# Options:
#   --profile <name>    Use a configuration profile (default: "default")
#   --data <path|url>   Load graph data from a file path or URL (overwrites active data)
#   --lang <en|sv>      Set the application language (default: en)
#
# Profiles:
#   Profiles are directories under config/ (e.g. config/esam/, config/default/).
#   Each can contain: schema_config.json, federation_config.json, .env, graph.json.
#   Missing files fall back to config/default/.
#
# Environment Variables:
#   SCHEMA_FILE - Path to custom schema configuration file
#   GRAPH_FILE - Path to graph data file (default: data/active/graph.json)
#   LLM_PROVIDER - LLM provider to use: "openai" or "claude" (auto-detected from API keys if not set)
#   APP_LANGUAGE - Application language: "en" or "sv" (default: en)
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
DATA_DIR="$SCRIPT_DIR/data"
ACTIVE_DATA="$DATA_DIR/active/graph.json"
DEFAULT_ACTIVE_DATA_EMBEDDINGS="$DATA_DIR/active/graph.embeddings.bin"
DEFAULT_EXAMPLE="$DATA_DIR/examples/default.json"

cd "$SCRIPT_DIR"

# Source shared profile utilities
source "$SCRIPT_DIR/config/profile-utils.sh"

# =====================
# Parse Arguments
# =====================
DATA_SOURCE=""
LANG_OVERRIDE=""
PROFILE_NAME="default"

while [[ $# -gt 0 ]]; do
    case $1 in
        --profile)
            PROFILE_NAME="$2"
            shift 2
            ;;
        --data)
            DATA_SOURCE="$2"
            shift 2
            ;;
        --lang)
            LANG_OVERRIDE="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Usage: ./start-dev.sh [--profile <name>] [--data <path|url>] [--lang <en|sv>]"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Community Knowledge Graph - Dev Start${NC}"
echo -e "${BLUE}========================================${NC}"

# =====================
# Profile Resolution
# =====================
validate_profile "$PROFILE_NAME"
PROFILE_DIR="$CONFIG_BASE_DIR/$PROFILE_NAME"

echo -e "  ${BLUE}Profile:${NC}     $PROFILE_NAME"

# Source .env files with fallback chain: profile → default → root
apply_profile_env "$PROFILE_NAME"

# The profile/.env files may set EMBEDDINGS_FILE. Resolve it the same way the
# backend does (relative values sit beside the graph file) so the sidecar this
# script deletes on a graph replacement is the one the app will actually read;
# otherwise the cleanup below silently misses it.
if [ -n "${EMBEDDINGS_FILE:-}" ]; then
    case "$EMBEDDINGS_FILE" in
        /*) ACTIVE_DATA_EMBEDDINGS="$EMBEDDINGS_FILE" ;;
        *)  ACTIVE_DATA_EMBEDDINGS="$(dirname "$ACTIVE_DATA")/$EMBEDDINGS_FILE" ;;
    esac
else
    ACTIVE_DATA_EMBEDDINGS="$DEFAULT_ACTIVE_DATA_EMBEDDINGS"
fi

# Auto-detect LLM provider when not explicitly configured.
# In SSPCloud (and similar environments), OPENAI_API_KEY + OPENAI_BASE_URL are
# injected via Vault secrets but LLM_PROVIDER is not set. Without this, the
# backend defaults to the Claude provider and reports LLM as unavailable.
if [ -z "$LLM_PROVIDER" ]; then
    if [ -n "$OPENAI_API_KEY" ] && [ -n "$OPENAI_BASE_URL" ]; then
        export LLM_PROVIDER=openai
        echo -e "  ${BLUE}LLM:${NC}         openai (auto-detected from OPENAI_BASE_URL)"
    elif [ -n "$OPENAI_API_KEY" ]; then
        export LLM_PROVIDER=openai
        echo -e "  ${BLUE}LLM:${NC}         openai (auto-detected from OPENAI_API_KEY)"
    elif [ -n "$ANTHROPIC_API_KEY" ]; then
        export LLM_PROVIDER=claude
        echo -e "  ${BLUE}LLM:${NC}         claude (auto-detected from ANTHROPIC_API_KEY)"
    else
        echo -e "  ${YELLOW}LLM:${NC}         not configured — AI assistant will be unavailable"
    fi
fi

# Resolve schema config: env var takes precedence, then profile fallback
if [ -n "$GRAPH_SCHEMA_CONFIG" ]; then
    export SCHEMA_FILE="$GRAPH_SCHEMA_CONFIG"
elif [ -z "$SCHEMA_FILE" ]; then
    RESOLVED_SCHEMA=$(resolve_config "$PROFILE_NAME" "schema_config.json")
    if [ -n "$RESOLVED_SCHEMA" ]; then
        export SCHEMA_FILE="$RESOLVED_SCHEMA"
    fi
fi

# Resolve federation config: env var takes precedence, then profile fallback
if [ -z "$FEDERATION_FILE" ] && [ -z "$GRAPH_FEDERATION_CONFIG" ]; then
    RESOLVED_FEDERATION=$(resolve_config "$PROFILE_NAME" "federation_config.json")
    if [ -n "$RESOLVED_FEDERATION" ]; then
        export FEDERATION_FILE="$RESOLVED_FEDERATION"
    fi
elif [ -n "$GRAPH_FEDERATION_CONFIG" ]; then
    export FEDERATION_FILE="$GRAPH_FEDERATION_CONFIG"
fi

# Export profile name for backend /info endpoint
export CONFIG_PROFILE="$PROFILE_NAME"

# =====================
# Language Configuration
# =====================
if [ -n "$LANG_OVERRIDE" ]; then
    export APP_LANGUAGE="$LANG_OVERRIDE"
    echo -e "${YELLOW}Language set to: $APP_LANGUAGE${NC}"
elif [ -z "$APP_LANGUAGE" ]; then
    export APP_LANGUAGE="en"
fi

# =====================
# Data Management
# =====================
echo -e "\n${YELLOW}[0/5] Setting up graph data...${NC}"

mkdir -p "$DATA_DIR/active"
mkdir -p "$DATA_DIR/examples"

# A sidecar belongs to the graph it was built from: node ids shared between the
# old and the new dataset would otherwise keep the OLD dataset's vectors, and
# nothing regenerates a vector for a node that is merely loaded. Vectors are
# derived data, so drop the sidecar whenever the graph beneath it is replaced.
# Call this only AFTER the replacement is actually in place - a failed download
# or a mistyped path must leave the existing pair intact.
drop_stale_embeddings() {
    if [ ! -f "$ACTIVE_DATA_EMBEDDINGS" ]; then
        return
    fi
    # Only ever delete a sidecar. EMBEDDINGS_FILE can be pointed at something
    # else - the old .env.example named a legacy embeddings.pkl for it - and
    # that file is not derived data we can regenerate.
    if [ -s "$ACTIVE_DATA_EMBEDDINGS" ] && \
       ! head -c 7 "$ACTIVE_DATA_EMBEDDINGS" | cmp -s - <(printf 'CKGEMB\001'); then
        echo -e "${YELLOW}Not removing $ACTIVE_DATA_EMBEDDINGS: it is not an embedding sidecar.${NC}"
        echo -e "${YELLOW}Point EMBEDDINGS_FILE at a path of its own.${NC}"
        return
    fi
    rm -f "$ACTIVE_DATA_EMBEDDINGS"
    echo -e "Removed stale embedding sidecar: ${BLUE}$ACTIVE_DATA_EMBEDDINGS${NC}"
}

# The journal beside graph.json holds mutations not yet folded into it, and is
# replayed onto the graph at startup. Replayed onto a DIFFERENT graph it would
# resurrect nodes of the old dataset, or overwrite same-id nodes of the new one
# with stale payloads - so it goes whenever the graph beneath it is replaced.
# Same rule as the sidecar: only after the replacement is in place.
drop_stale_journal() {
    local journal="${ACTIVE_DATA%.*}.journal.ndjson"
    if [ -f "$journal" ]; then
        rm -f "$journal"
        echo -e "Removed stale graph journal: ${BLUE}$journal${NC}"
    fi
}

if [ -n "$DATA_SOURCE" ]; then
    # Data source specified - load from path or URL
    if [[ "$DATA_SOURCE" =~ ^https?:// ]]; then
        echo -e "Downloading graph data from: ${BLUE}$DATA_SOURCE${NC}"
        if command -v curl &> /dev/null; then
            # -f so an HTTP error is a failure: without it curl exits 0 on a
            # 404, writes the error page over the graph, and the cleanup
            # below then deletes the sidecar of the graph that was there.
            curl -fsL "$DATA_SOURCE" -o "$ACTIVE_DATA"
        elif command -v wget &> /dev/null; then
            # wget -O opens its target before the request, so a 404 would
            # empty the graph to zero bytes and then fail. Land the body
            # beside it and move it into place only once the transfer
            # succeeded, which is what -f already gives the curl path.
            download="$ACTIVE_DATA.download"
            if ! wget -q "$DATA_SOURCE" -O "$download"; then
                rm -f "$download"
                echo -e "${RED}Error: download failed: $DATA_SOURCE${NC}"
                exit 1
            fi
            mv -f "$download" "$ACTIVE_DATA"
        else
            echo -e "${RED}Error: curl or wget required to download data from URL${NC}"
            exit 1
        fi
        echo -e "${GREEN}Graph data downloaded to $ACTIVE_DATA${NC}"
        drop_stale_embeddings
        drop_stale_journal
    else
        # Resolve relative paths
        if [[ ! "$DATA_SOURCE" = /* ]]; then
            DATA_SOURCE="$SCRIPT_DIR/$DATA_SOURCE"
        fi
        if [ ! -f "$DATA_SOURCE" ]; then
            echo -e "${RED}Error: Data file not found: $DATA_SOURCE${NC}"
            exit 1
        fi
        echo -e "Copying graph data from: ${BLUE}$DATA_SOURCE${NC}"
        cp "$DATA_SOURCE" "$ACTIVE_DATA"
        echo -e "${GREEN}Graph data copied to $ACTIVE_DATA${NC}"
        drop_stale_embeddings
        drop_stale_journal
    fi
elif [ ! -f "$ACTIVE_DATA" ]; then
    # No active data and no source specified - try profile graph.json, then default example
    PROFILE_GRAPH=$(resolve_config "$PROFILE_NAME" "graph.json")
    if [ -n "$PROFILE_GRAPH" ]; then
        echo -e "Loading graph data from profile: ${BLUE}$PROFILE_GRAPH${NC}"
        cp "$PROFILE_GRAPH" "$ACTIVE_DATA"
        echo -e "${GREEN}Profile graph data loaded.${NC}"
        drop_stale_embeddings
        drop_stale_journal
    elif [ -f "$DEFAULT_EXAMPLE" ]; then
        echo -e "No active graph data found. Copying default example data..."
        cp "$DEFAULT_EXAMPLE" "$ACTIVE_DATA"
        echo -e "${GREEN}Default example data loaded.${NC}"
        drop_stale_embeddings
        drop_stale_journal
    else
        echo -e "${YELLOW}No example data found. Starting with empty graph.${NC}"
        echo '{"nodes": [], "edges": [], "metadata": {"version": "1.0"}}' > "$ACTIVE_DATA"
        drop_stale_embeddings
        drop_stale_journal
    fi
else
    echo -e "${GREEN}Using existing active graph data.${NC}"
fi

# Set GRAPH_FILE to point to active data
export GRAPH_FILE="$ACTIVE_DATA"
export EMBEDDINGS_FILE="$ACTIVE_DATA_EMBEDDINGS"

# =====================
# Node.js Check / Auto-install
# =====================

# Source nvm so nvm-managed node versions are in PATH.
# In environments like SSPCloud, node is managed via nvm but not in PATH
# until nvm is sourced. The || true prevents set -e from exiting when
# nvm.sh itself returns non-zero (e.g. no default node version set yet).
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh" || true

if ! command -v node &> /dev/null; then
    echo -e "${YELLOW}Node.js not found — installing automatically via nvm...${NC}"

    # Install nvm if not already present
    if [ ! -s "$NVM_DIR/nvm.sh" ]; then
        echo "Installing nvm..."
        curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
        export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
        \. "$NVM_DIR/nvm.sh" || true
    fi

    # Install and activate Node.js 20
    echo "Installing Node.js 20 (this may take a minute)..."
    nvm install 20
    nvm use 20

    if ! command -v node &> /dev/null; then
        echo -e "${RED}Error: Node.js installation failed. Install it manually:${NC}"
        echo -e "  nvm install 20"
        exit 1
    fi

    echo -e "${GREEN}Node.js $(node --version) installed.${NC}"
fi

# =====================
# Python Environment
# =====================
echo -e "\n${YELLOW}[1/5] Setting up Python environment...${NC}"

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -q -r "$BACKEND_DIR/requirements.txt"

echo -e "${GREEN}Python environment ready.${NC}"

# =====================
# Node.js Dependencies
# =====================
echo -e "\n${YELLOW}[2/5] Installing Node.js dependencies...${NC}"

if [ ! -d "node_modules" ]; then
    npm install
else
    echo "Node modules already installed. Run 'npm install' manually to update."
fi

echo -e "${GREEN}Node.js dependencies ready.${NC}"

# =====================
# Build Web App
# =====================
echo -e "\n${YELLOW}[3/5] Building web application...${NC}"

npm run build:web

echo -e "${GREEN}Web app built to frontend/web/dist/${NC}"

# =====================
# Build Widget
# =====================
echo -e "\n${YELLOW}[4/5] Building ChatGPT widget...${NC}"

npm run build:widget

echo -e "${GREEN}Widget built to frontend/widget/dist/${NC}"

# =====================
# Start Server
# =====================
echo -e "\n${YELLOW}[5/5] Starting FastAPI server...${NC}"
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Services available at:${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "  ${BLUE}Web App:${NC}     http://localhost:8000/web/"
echo -e "  ${BLUE}Widget:${NC}      http://localhost:8000/widget/"
echo -e "  ${BLUE}REST API:${NC}    http://localhost:8000/api/"
echo -e "  ${BLUE}Chat API:${NC}    http://localhost:8000/ui/"
echo -e "  ${BLUE}MCP:${NC}         http://localhost:8000/mcp"
echo -e "  ${BLUE}Health:${NC}      http://localhost:8000/health"
echo ""
echo -e "${GREEN}  Configuration:${NC}"
echo -e "  ${BLUE}Profile:${NC}     $PROFILE_NAME"
if [ -n "$SCHEMA_FILE" ]; then
    echo -e "  ${BLUE}Schema:${NC}      $SCHEMA_FILE"
else
    echo -e "  ${BLUE}Schema:${NC}      config/default/schema_config.json (default)"
fi
if [ -n "$FEDERATION_FILE" ]; then
    echo -e "  ${BLUE}Federation:${NC}  $FEDERATION_FILE"
fi
echo -e "  ${BLUE}Graph data:${NC}  $GRAPH_FILE"
echo -e "  ${BLUE}Language:${NC}    $APP_LANGUAGE"
echo ""
echo -e "  ${YELLOW}Note: the UI is temporarily locked to English; ?lang and --lang have no effect.${NC}"
echo ""
echo -e "Press Ctrl+C to stop the server."
echo -e "${GREEN}========================================${NC}"
echo ""

# Start the server
# Check for Codespace environment to print public URL
if [ -n "$CODESPACE_NAME" ]; then
    echo -e "${YELLOW}Running in Codespace: $CODESPACE_NAME${NC}"
    echo -e "${YELLOW}Public MCP URL: https://$CODESPACE_NAME-8000.app.github.dev/mcp/sse${NC}"
    echo -e "${YELLOW}NOTE: Ensure port 8000 is set to Public visibility in the Ports tab.${NC}"
fi

# Ignore SIGINT until uvicorn registers its own signal handlers.
# In Codespace terminals, a spurious SIGINT is delivered during process startup
# which kills the server before it finishes initializing. The ignored disposition
# is inherited across exec, and uvicorn's reloader overrides it with its own
# handler via signal.signal(), so Ctrl+C still works once the server is ready.
trap '' INT
exec uvicorn backend.api_host.server:get_app --factory --reload --host 0.0.0.0 --port 8000 \
    --reload-dir backend
