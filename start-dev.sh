#!/bin/bash
#
# Start Development Environment
# This script sets up and starts all services needed to run the full application.
#
# Environment Variables:
#   GRAPH_SCHEMA_CONFIG - Path to custom schema configuration file (default: config/schema_config.json)
#   GRAPH_FILE - Path to graph data file (default: graph.json)
#   LLM_PROVIDER - LLM provider to use: "openai" or "claude" (auto-detected from API keys if not set)
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Community Knowledge Graph - Dev Start${NC}"
echo -e "${BLUE}========================================${NC}"

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

cd "$SCRIPT_DIR"

# =====================
# Configuration
# =====================
if [ -n "$GRAPH_SCHEMA_CONFIG" ]; then
    echo -e "${YELLOW}Using custom schema config: $GRAPH_SCHEMA_CONFIG${NC}"
    export SCHEMA_FILE="$GRAPH_SCHEMA_CONFIG"
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
if [ -n "$SCHEMA_FILE" ]; then
    echo -e "  ${BLUE}Schema:${NC}      $SCHEMA_FILE"
else
    echo -e "  ${BLUE}Schema:${NC}      config/schema_config.json (default)"
fi
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

exec uvicorn backend.api_host.server:get_app --factory --reload --host 0.0.0.0 --port 8000
