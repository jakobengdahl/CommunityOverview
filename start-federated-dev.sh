#!/bin/bash
#
# Start Federated Development Environment
#
# Starts TWO graph instances that are federated with each other, enabling
# testing of cross-graph search, node adoption, and federation features.
#
# Usage:
#   ./start-federated-dev.sh [OPTIONS]
#
# Options:
#   --build          Force rebuild of web app and widget before starting
#   --lang <en|sv>   Set the application language (default: en)
#
# Instances:
#   Graph A (port 8000): http://localhost:8000/web/
#   Graph B (port 8001): http://localhost:8001/web/
#
# Each instance has its own data directory, federation config, and MCP endpoint.
# They are configured to federate with each other so nodes from Graph B appear
# in Graph A and vice versa.
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
DATA_DIR="$SCRIPT_DIR/data"
CONFIG_DIR="$SCRIPT_DIR/config"

cd "$SCRIPT_DIR"

# =====================
# Parse Arguments
# =====================
FORCE_BUILD=false
LANG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --build)
            FORCE_BUILD=true
            shift
            ;;
        --lang)
            LANG_OVERRIDE="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Usage: ./start-federated-dev.sh [--build] [--lang <en|sv>]"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Federated Knowledge Graph - Dev Start${NC}"
echo -e "${BLUE}  (Two federated graph instances)${NC}"
echo -e "${BLUE}============================================${NC}"

# =====================
# Port Configuration
# =====================
PORT_A=8000
PORT_B=8001

# =====================
# Language Configuration
# =====================
if [ -n "$LANG_OVERRIDE" ]; then
    export APP_LANGUAGE="$LANG_OVERRIDE"
elif [ -z "$APP_LANGUAGE" ]; then
    export APP_LANGUAGE="en"
fi

# =====================
# Cleanup on exit
# =====================
PID_A=""
PID_B=""

cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down federated instances...${NC}"
    if [ -n "$PID_A" ] && kill -0 "$PID_A" 2>/dev/null; then
        kill "$PID_A" 2>/dev/null
        wait "$PID_A" 2>/dev/null || true
        echo -e "${GREEN}  Graph A (port $PORT_A) stopped.${NC}"
    fi
    if [ -n "$PID_B" ] && kill -0 "$PID_B" 2>/dev/null; then
        kill "$PID_B" 2>/dev/null
        wait "$PID_B" 2>/dev/null || true
        echo -e "${GREEN}  Graph B (port $PORT_B) stopped.${NC}"
    fi
    echo -e "${GREEN}All instances stopped.${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# =====================
# Data Directories
# =====================
echo -e "\n${YELLOW}[1/6] Setting up federated graph data...${NC}"

DATA_A="$DATA_DIR/federated-a"
DATA_B="$DATA_DIR/federated-b"
GRAPH_A="$DATA_A/graph.json"
GRAPH_B="$DATA_B/graph.json"

mkdir -p "$DATA_A"
mkdir -p "$DATA_B"

DEFAULT_EXAMPLE="$DATA_DIR/examples/default.json"

# Create Graph A data - use default example or existing
if [ ! -f "$GRAPH_A" ]; then
    if [ -f "$DEFAULT_EXAMPLE" ]; then
        echo -e "  Initializing Graph A from default example data..."
        python3 -c "
import json, sys
with open('$DEFAULT_EXAMPLE') as f:
    data = json.load(f)
# Use first half of nodes for Graph A
nodes = [n for n in data.get('nodes', []) if n.get('type') not in ('SavedView', 'VisualizationView')]
half = len(nodes) // 2
a_nodes = nodes[:half]
a_ids = {n['id'] for n in a_nodes}
a_edges = [e for e in data.get('edges', []) if e.get('source') in a_ids and e.get('target') in a_ids]
result = {
    'nodes': a_nodes,
    'edges': a_edges,
    'metadata': {'version': '1.0', 'graph_name': 'Graph Alpha'}
}
with open('$GRAPH_A', 'w') as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print(f'  Graph A: {len(a_nodes)} nodes, {len(a_edges)} edges')
"
    else
        echo -e "  Creating empty Graph A..."
        echo '{"nodes": [], "edges": [], "metadata": {"version": "1.0", "graph_name": "Graph Alpha"}}' > "$GRAPH_A"
    fi
else
    echo -e "${GREEN}  Using existing Graph A data.${NC}"
fi

# Create Graph B data - use second half of default example or empty
if [ ! -f "$GRAPH_B" ]; then
    if [ -f "$DEFAULT_EXAMPLE" ]; then
        echo -e "  Initializing Graph B from default example data..."
        python3 -c "
import json, sys
with open('$DEFAULT_EXAMPLE') as f:
    data = json.load(f)
# Use second half of nodes for Graph B
nodes = [n for n in data.get('nodes', []) if n.get('type') not in ('SavedView', 'VisualizationView')]
half = len(nodes) // 2
b_nodes = nodes[half:]
b_ids = {n['id'] for n in b_nodes}
b_edges = [e for e in data.get('edges', []) if e.get('source') in b_ids and e.get('target') in b_ids]
result = {
    'nodes': b_nodes,
    'edges': b_edges,
    'metadata': {'version': '1.0', 'graph_name': 'Graph Beta'}
}
with open('$GRAPH_B', 'w') as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print(f'  Graph B: {len(b_nodes)} nodes, {len(b_edges)} edges')
"
    else
        echo -e "  Creating empty Graph B..."
        echo '{"nodes": [], "edges": [], "metadata": {"version": "1.0", "graph_name": "Graph Beta"}}' > "$GRAPH_B"
    fi
else
    echo -e "${GREEN}  Using existing Graph B data.${NC}"
fi

# =====================
# Federation Configs
# =====================
echo -e "\n${YELLOW}[2/6] Generating federation configs...${NC}"

FED_CONFIG_A="$DATA_A/federation_config.json"
FED_CONFIG_B="$DATA_B/federation_config.json"

# Determine base URL (support Codespace or local)
if [ -n "$CODESPACE_NAME" ]; then
    BASE_URL_A="https://$CODESPACE_NAME-$PORT_A.app.github.dev"
    BASE_URL_B="https://$CODESPACE_NAME-$PORT_B.app.github.dev"
else
    BASE_URL_A="http://localhost:$PORT_A"
    BASE_URL_B="http://localhost:$PORT_B"
fi

# Graph A's federation config: points to Graph B
cat > "$FED_CONFIG_A" << EOF
{
  "federation": {
    "enabled": true,
    "max_traversal_depth": 2,
    "default_timeout_ms": 5000,
    "allow_live_remote_enrichment": false,
    "depth_levels": [1, 2],
    "graphs": [
      {
        "graph_id": "graph-beta",
        "display_name": "Graph Beta",
        "enabled": true,
        "trust_level": "internal",
        "endpoints": {
          "graph_json_url": "$BASE_URL_B/export_graph",
          "mcp_url": "$BASE_URL_B/mcp/sse",
          "gui_url": "$BASE_URL_B/web/"
        },
        "capabilities": {
          "allow_read": true,
          "allow_write": false,
          "allow_adopt": true
        },
        "sync": {
          "mode": "scheduled",
          "interval_seconds": 30,
          "on_startup": true,
          "on_demand": true
        },
        "auth": {
          "type": "none"
        }
      }
    ]
  }
}
EOF
echo -e "  ${GREEN}Graph A federation config: federates with Graph B at $BASE_URL_B${NC}"

# Graph B's federation config: points to Graph A
cat > "$FED_CONFIG_B" << EOF
{
  "federation": {
    "enabled": true,
    "max_traversal_depth": 2,
    "default_timeout_ms": 5000,
    "allow_live_remote_enrichment": false,
    "depth_levels": [1, 2],
    "graphs": [
      {
        "graph_id": "graph-alpha",
        "display_name": "Graph Alpha",
        "enabled": true,
        "trust_level": "internal",
        "endpoints": {
          "graph_json_url": "$BASE_URL_A/export_graph",
          "mcp_url": "$BASE_URL_A/mcp/sse",
          "gui_url": "$BASE_URL_A/web/"
        },
        "capabilities": {
          "allow_read": true,
          "allow_write": false,
          "allow_adopt": true
        },
        "sync": {
          "mode": "scheduled",
          "interval_seconds": 30,
          "on_startup": true,
          "on_demand": true
        },
        "auth": {
          "type": "none"
        }
      }
    ]
  }
}
EOF
echo -e "  ${GREEN}Graph B federation config: federates with Graph A at $BASE_URL_A${NC}"

# =====================
# Python Environment
# =====================
echo -e "\n${YELLOW}[3/6] Setting up Python environment...${NC}"

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "Installing Python dependencies..."
pip install -q -r "$BACKEND_DIR/requirements.txt"

echo -e "${GREEN}Python environment ready.${NC}"

# =====================
# Node.js Dependencies
# =====================
echo -e "\n${YELLOW}[4/6] Installing Node.js dependencies...${NC}"

if [ ! -d "node_modules" ]; then
    npm install
else
    echo "Node modules already installed."
fi

echo -e "${GREEN}Node.js dependencies ready.${NC}"

# =====================
# Build (if needed)
# =====================
echo -e "\n${YELLOW}[5/6] Building web applications...${NC}"

WEB_DIST="$SCRIPT_DIR/frontend/web/dist"
WIDGET_DIST="$SCRIPT_DIR/frontend/widget/dist"

if [ "$FORCE_BUILD" = true ] || [ ! -d "$WEB_DIST" ] || [ ! -d "$WIDGET_DIST" ]; then
    echo "Building web app..."
    npm run build:web
    echo "Building widget..."
    npm run build:widget
    echo -e "${GREEN}Builds complete.${NC}"
else
    echo -e "${GREEN}Using existing builds. Use --build to rebuild.${NC}"
fi

# =====================
# Start Both Instances
# =====================
echo -e "\n${YELLOW}[6/6] Starting federated instances...${NC}"
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Federated Graph Services${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  ${CYAN}--- Graph A (Alpha) ---${NC}"
echo -e "  ${BLUE}Web App:${NC}     $BASE_URL_A/web/"
echo -e "  ${BLUE}REST API:${NC}    $BASE_URL_A/api/"
echo -e "  ${BLUE}MCP:${NC}         $BASE_URL_A/mcp"
echo -e "  ${BLUE}Health:${NC}      $BASE_URL_A/health"
echo -e "  ${BLUE}Data:${NC}        $GRAPH_A"
echo ""
echo -e "  ${CYAN}--- Graph B (Beta) ---${NC}"
echo -e "  ${BLUE}Web App:${NC}     $BASE_URL_B/web/"
echo -e "  ${BLUE}REST API:${NC}    $BASE_URL_B/api/"
echo -e "  ${BLUE}MCP:${NC}         $BASE_URL_B/mcp"
echo -e "  ${BLUE}Health:${NC}      $BASE_URL_B/health"
echo -e "  ${BLUE}Data:${NC}        $GRAPH_B"
echo ""
echo -e "  ${YELLOW}Federation sync interval: 30 seconds${NC}"
echo -e "  ${YELLOW}Open both Web Apps side-by-side to test federation!${NC}"
echo ""
echo -e "Press Ctrl+C to stop both instances."
echo -e "${GREEN}============================================${NC}"
echo ""

if [ -n "$CODESPACE_NAME" ]; then
    echo -e "${YELLOW}Running in Codespace: $CODESPACE_NAME${NC}"
    echo -e "${YELLOW}NOTE: Ensure ports $PORT_A and $PORT_B are set to Public in the Ports tab.${NC}"
    echo ""
fi

# Start Graph A in background
GRAPH_FILE="$GRAPH_A" \
FEDERATION_FILE="$FED_CONFIG_A" \
PORT="$PORT_A" \
MCP_NAME="graph-alpha" \
APP_LANGUAGE="$APP_LANGUAGE" \
uvicorn backend.api_host.server:get_app --factory \
    --host 0.0.0.0 --port "$PORT_A" \
    --reload \
    --reload-exclude 'venv/*' \
    --reload-exclude 'data/*' \
    --reload-exclude 'node_modules/*' \
    --reload-exclude '.git/*' \
    --reload-exclude 'frontend/web/dist/*' \
    --reload-exclude 'frontend/widget/dist/*' \
    2>&1 | sed "s/^/[Graph A] /" &
PID_A=$!

# Brief pause to let Graph A start binding its port
sleep 2

# Start Graph B in background
GRAPH_FILE="$GRAPH_B" \
FEDERATION_FILE="$FED_CONFIG_B" \
PORT="$PORT_B" \
MCP_NAME="graph-beta" \
APP_LANGUAGE="$APP_LANGUAGE" \
uvicorn backend.api_host.server:get_app --factory \
    --host 0.0.0.0 --port "$PORT_B" \
    --reload \
    --reload-exclude 'venv/*' \
    --reload-exclude 'data/*' \
    --reload-exclude 'node_modules/*' \
    --reload-exclude '.git/*' \
    --reload-exclude 'frontend/web/dist/*' \
    --reload-exclude 'frontend/widget/dist/*' \
    2>&1 | sed "s/^/[Graph B] /" &
PID_B=$!

# Wait for both processes
wait $PID_A $PID_B
