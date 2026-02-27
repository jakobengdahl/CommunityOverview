#!/bin/bash
#
# Start Development Environment
# This script sets up and starts all services needed to run the full application.
#
# Usage:
#   ./start-dev.sh [OPTIONS]
#
# Options:
#   --data <path|url>   Load graph data from a file path or URL (overwrites active data)
#   --lang <en|sv>      Set the application language (default: en)
#
# Environment Variables:
#   GRAPH_SCHEMA_CONFIG - Path to custom schema configuration file (default: config/schema_config.json)
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
DEFAULT_EXAMPLE="$DATA_DIR/examples/default.json"

cd "$SCRIPT_DIR"

# =====================
# Parse Arguments
# =====================
DATA_SOURCE=""
LANG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case $1 in
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
            echo "Usage: ./start-dev.sh [--data <path|url>] [--lang <en|sv>]"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Community Knowledge Graph - Dev Start${NC}"
echo -e "${BLUE}========================================${NC}"

# =====================
# Configuration
# =====================
if [ -n "$GRAPH_SCHEMA_CONFIG" ]; then
    echo -e "${YELLOW}Using custom schema config: $GRAPH_SCHEMA_CONFIG${NC}"
    export SCHEMA_FILE="$GRAPH_SCHEMA_CONFIG"
fi

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

if [ -n "$DATA_SOURCE" ]; then
    # Data source specified - load from path or URL
    if [[ "$DATA_SOURCE" =~ ^https?:// ]]; then
        echo -e "Downloading graph data from: ${BLUE}$DATA_SOURCE${NC}"
        if command -v curl &> /dev/null; then
            curl -sL "$DATA_SOURCE" -o "$ACTIVE_DATA"
        elif command -v wget &> /dev/null; then
            wget -q "$DATA_SOURCE" -O "$ACTIVE_DATA"
        else
            echo -e "${RED}Error: curl or wget required to download data from URL${NC}"
            exit 1
        fi
        echo -e "${GREEN}Graph data downloaded to $ACTIVE_DATA${NC}"
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
    fi
elif [ ! -f "$ACTIVE_DATA" ]; then
    # No active data and no source specified - use default example
    if [ -f "$DEFAULT_EXAMPLE" ]; then
        echo -e "No active graph data found. Copying default example data..."
        cp "$DEFAULT_EXAMPLE" "$ACTIVE_DATA"
        echo -e "${GREEN}Default example data loaded.${NC}"
    else
        echo -e "${YELLOW}No example data found. Starting with empty graph.${NC}"
        echo '{"nodes": [], "edges": [], "metadata": {"version": "1.0"}}' > "$ACTIVE_DATA"
    fi
else
    echo -e "${GREEN}Using existing active graph data.${NC}"
fi

# Set GRAPH_FILE to point to active data
export GRAPH_FILE="$ACTIVE_DATA"

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
if [ -n "$SCHEMA_FILE" ]; then
    echo -e "  ${BLUE}Schema:${NC}      $SCHEMA_FILE"
else
    echo -e "  ${BLUE}Schema:${NC}      config/schema_config.json (default)"
fi
echo -e "  ${BLUE}Graph data:${NC}  $GRAPH_FILE"
echo -e "  ${BLUE}Language:${NC}    $APP_LANGUAGE"
echo ""
echo -e "  ${YELLOW}Language can also be set via URL: http://localhost:8000/web/?lang=sv${NC}"
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

exec uvicorn backend.api_host.server:get_app --factory --reload --host 0.0.0.0 --port 8000 \
    --reload-dir backend
