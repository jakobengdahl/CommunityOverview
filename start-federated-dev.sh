#!/bin/bash
#
# Start Federated Development Environment
#
# Starts multiple graph instances that are federated with each other, enabling
# testing of cross-graph search, node adoption, and federation features.
#
# Usage:
#   ./start-federated-dev.sh [OPTIONS]
#
# Options:
#   --profile <name>   Add a profile instance (can be repeated for multi-profile)
#   --build            Force rebuild of web app and widget before starting
#   --lang <en|sv>     Set the application language (default: en)
#
# Examples:
#   ./start-federated-dev.sh                          # Default: two instances (A/B)
#   ./start-federated-dev.sh --profile esam --profile unece  # Profile per instance
#
# Without --profile: starts two instances (Graph A / Graph B) using default schema,
# with data auto-split from the default example.
#
# With --profile: each profile becomes a federated instance with its own schema,
# env vars, and graph data from the profile directory.
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

# Source shared profile utilities
source "$SCRIPT_DIR/config/profile-utils.sh"

# =====================
# Parse Arguments
# =====================
FORCE_BUILD=false
LANG_OVERRIDE=""
PROFILES=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --profile)
            PROFILES+=("$2")
            shift 2
            ;;
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
            echo "Usage: ./start-federated-dev.sh [--profile <name>]... [--build] [--lang <en|sv>]"
            exit 1
            ;;
    esac
done

# Determine mode: profile-based or legacy (A/B)
USE_PROFILES=false
if [ ${#PROFILES[@]} -gt 0 ]; then
    if [ ${#PROFILES[@]} -lt 2 ]; then
        echo -e "${RED}Error: At least two --profile flags are required for federation.${NC}"
        echo "Usage: ./start-federated-dev.sh --profile <name1> --profile <name2> [--profile <name3>] ..."
        exit 1
    fi
    USE_PROFILES=true
    # Validate all profiles
    for p in "${PROFILES[@]}"; do
        validate_profile "$p"
    done
fi

if [ "$USE_PROFILES" = true ]; then
    echo -e "${BLUE}============================================${NC}"
    echo -e "${BLUE}  Federated Knowledge Graph - Dev Start${NC}"
    echo -e "${BLUE}  (${#PROFILES[@]} profile instances)${NC}"
    echo -e "${BLUE}============================================${NC}"
else
    echo -e "${BLUE}============================================${NC}"
    echo -e "${BLUE}  Federated Knowledge Graph - Dev Start${NC}"
    echo -e "${BLUE}  (Two federated graph instances)${NC}"
    echo -e "${BLUE}============================================${NC}"
fi

# =====================
# Port Configuration
# =====================
BASE_PORT=8000

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
declare -a PIDS=()
declare -a PID_LABELS=()

cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down federated instances...${NC}"
    for i in "${!PIDS[@]}"; do
        local pid="${PIDS[$i]}"
        local label="${PID_LABELS[$i]}"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null || true
            echo -e "${GREEN}  $label stopped.${NC}"
        fi
    done
    echo -e "${GREEN}All instances stopped.${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

DEFAULT_EXAMPLE="$DATA_DIR/examples/default.json"

# =====================================================
# Helper: get base URL for a port
# =====================================================
get_base_url() {
    local port="$1"
    if [ -n "$CODESPACE_NAME" ]; then
        echo "https://$CODESPACE_NAME-$port.app.github.dev"
    else
        echo "http://localhost:$port"
    fi
}

# =====================================================
# Helper: generate federation config for one instance
# pointing to all other instances as peers
# =====================================================
generate_federation_config() {
    local output_file="$1"
    shift
    # Remaining args are: peer_id peer_name peer_port peer_id peer_name peer_port ...
    local peers_json=""
    while [ $# -gt 0 ]; do
        local peer_id="$1"
        local peer_name="$2"
        local peer_port="$3"
        shift 3
        local peer_url
        peer_url=$(get_base_url "$peer_port")
        [ -n "$peers_json" ] && peers_json="$peers_json,"
        peers_json="$peers_json
      {
        \"graph_id\": \"$peer_id\",
        \"display_name\": \"$peer_name\",
        \"enabled\": true,
        \"trust_level\": \"internal\",
        \"endpoints\": {
          \"graph_json_url\": \"$peer_url/export_graph\",
          \"mcp_url\": \"$peer_url/mcp/sse\",
          \"gui_url\": \"$peer_url/web/\"
        },
        \"capabilities\": {
          \"allow_read\": true,
          \"allow_write\": false,
          \"allow_adopt\": true
        },
        \"sync\": {
          \"mode\": \"scheduled\",
          \"interval_seconds\": 30,
          \"on_startup\": true,
          \"on_demand\": true
        },
        \"auth\": {
          \"type\": \"none\"
        }
      }"
    done

    cat > "$output_file" << FEDEOF
{
  "federation": {
    "enabled": true,
    "max_traversal_depth": 2,
    "default_timeout_ms": 5000,
    "allow_live_remote_enrichment": false,
    "depth_levels": [1, 2],
    "graphs": [$peers_json
    ]
  }
}
FEDEOF
}

if [ "$USE_PROFILES" = true ]; then
    # ==========================================================
    # PROFILE MODE: each --profile becomes a federated instance
    # ==========================================================
    echo -e "\n${YELLOW}[1/6] Setting up profile-based federated data...${NC}"

    NUM_INSTANCES=${#PROFILES[@]}
    declare -a INSTANCE_PORTS=()
    declare -a INSTANCE_DATA_DIRS=()
    declare -a INSTANCE_GRAPHS=()
    declare -a INSTANCE_FED_CONFIGS=()
    declare -a INSTANCE_SCHEMA_FILES=()
    declare -a INSTANCE_MCP_NAMES=()

    for i in "${!PROFILES[@]}"; do
        local_profile="${PROFILES[$i]}"
        local_port=$((BASE_PORT + i))
        local_data_dir="$DATA_DIR/federated-$local_profile"
        local_graph="$local_data_dir/graph.json"
        local_fed_config="$local_data_dir/federation_config.json"
        local_mcp_name="graph-$local_profile"

        INSTANCE_PORTS+=("$local_port")
        INSTANCE_DATA_DIRS+=("$local_data_dir")
        INSTANCE_GRAPHS+=("$local_graph")
        INSTANCE_FED_CONFIGS+=("$local_fed_config")
        INSTANCE_MCP_NAMES+=("$local_mcp_name")

        mkdir -p "$local_data_dir"

        # Resolve schema config for this profile
        local_schema=$(resolve_config "$local_profile" "schema_config.json")
        INSTANCE_SCHEMA_FILES+=("$local_schema")

        # Apply profile env (sets unset vars only, so first profile wins for shared vars)
        apply_profile_env "$local_profile"

        # Set up graph data from profile or empty
        if [ ! -f "$local_graph" ]; then
            profile_graph=$(resolve_config "$local_profile" "graph.json")
            if [ -n "$profile_graph" ]; then
                echo -e "  ${BLUE}$local_profile:${NC} Loading graph data from profile..."
                cp "$profile_graph" "$local_graph"
            elif [ -f "$DEFAULT_EXAMPLE" ]; then
                echo -e "  ${BLUE}$local_profile:${NC} Loading default example data..."
                cp "$DEFAULT_EXAMPLE" "$local_graph"
            else
                echo -e "  ${BLUE}$local_profile:${NC} Starting with empty graph."
                echo '{"nodes": [], "edges": [], "metadata": {"version": "1.0"}}' > "$local_graph"
            fi
        else
            echo -e "  ${GREEN}$local_profile: Using existing data.${NC}"
        fi
    done

    # Generate federation configs: each instance points to all others
    echo -e "\n${YELLOW}[2/6] Generating federation configs...${NC}"
    for i in "${!PROFILES[@]}"; do
        local_profile="${PROFILES[$i]}"
        # Build peer list (all instances except this one)
        peer_args=()
        for j in "${!PROFILES[@]}"; do
            if [ "$i" != "$j" ]; then
                peer_args+=("graph-${PROFILES[$j]}" "${PROFILES[$j]}" "${INSTANCE_PORTS[$j]}")
            fi
        done
        generate_federation_config "${INSTANCE_FED_CONFIGS[$i]}" "${peer_args[@]}"
        local_url=$(get_base_url "${INSTANCE_PORTS[$i]}")
        echo -e "  ${GREEN}$local_profile (port ${INSTANCE_PORTS[$i]}): federated with ${#peer_args[@]} peers${NC}"
    done

else
    # ==========================================================
    # LEGACY MODE: two instances (Graph A / Graph B)
    # ==========================================================
    echo -e "\n${YELLOW}[1/6] Setting up federated graph data...${NC}"

    PORT_A=$BASE_PORT
    PORT_B=$((BASE_PORT + 1))

    DATA_A="$DATA_DIR/federated-a"
    DATA_B="$DATA_DIR/federated-b"
    GRAPH_A="$DATA_A/graph.json"
    GRAPH_B="$DATA_B/graph.json"

    mkdir -p "$DATA_A"
    mkdir -p "$DATA_B"

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

    BASE_URL_A=$(get_base_url "$PORT_A")
    BASE_URL_B=$(get_base_url "$PORT_B")

    generate_federation_config "$FED_CONFIG_A" "graph-beta" "Graph Beta" "$PORT_B"
    echo -e "  ${GREEN}Graph A federation config: federates with Graph B at $BASE_URL_B${NC}"

    generate_federation_config "$FED_CONFIG_B" "graph-alpha" "Graph Alpha" "$PORT_A"
    echo -e "  ${GREEN}Graph B federation config: federates with Graph A at $BASE_URL_A${NC}"
fi

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
# Start Instances
# =====================
echo -e "\n${YELLOW}[6/6] Starting federated instances...${NC}"
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Federated Graph Services${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

if [ "$USE_PROFILES" = true ]; then
    for i in "${!PROFILES[@]}"; do
        local_profile="${PROFILES[$i]}"
        local_port="${INSTANCE_PORTS[$i]}"
        local_url=$(get_base_url "$local_port")
        echo -e "  ${CYAN}--- $local_profile (port $local_port) ---${NC}"
        echo -e "  ${BLUE}Web App:${NC}     $local_url/web/"
        echo -e "  ${BLUE}REST API:${NC}    $local_url/api/"
        echo -e "  ${BLUE}MCP:${NC}         $local_url/mcp"
        echo -e "  ${BLUE}Health:${NC}      $local_url/health"
        echo -e "  ${BLUE}Schema:${NC}      ${INSTANCE_SCHEMA_FILES[$i]}"
        echo -e "  ${BLUE}Data:${NC}        ${INSTANCE_GRAPHS[$i]}"
        echo ""
    done
else
    BASE_URL_A=$(get_base_url "$PORT_A")
    BASE_URL_B=$(get_base_url "$PORT_B")
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
fi

echo -e "  ${YELLOW}Federation sync interval: 30 seconds${NC}"
echo -e "  ${YELLOW}Open both Web Apps side-by-side to test federation!${NC}"
echo ""
echo -e "Press Ctrl+C to stop all instances."
echo -e "${GREEN}============================================${NC}"
echo ""

if [ -n "$CODESPACE_NAME" ]; then
    echo -e "${YELLOW}Running in Codespace: $CODESPACE_NAME${NC}"
    echo -e "${YELLOW}NOTE: Ensure all ports are set to Public in the Ports tab.${NC}"
    echo ""
fi

if [ "$USE_PROFILES" = true ]; then
    # Start profile-based instances
    for i in "${!PROFILES[@]}"; do
        local_profile="${PROFILES[$i]}"
        local_port="${INSTANCE_PORTS[$i]}"
        local_graph="${INSTANCE_GRAPHS[$i]}"
        local_fed_config="${INSTANCE_FED_CONFIGS[$i]}"
        local_schema="${INSTANCE_SCHEMA_FILES[$i]}"
        local_mcp_name="${INSTANCE_MCP_NAMES[$i]}"

        GRAPH_FILE="$local_graph" \
        FEDERATION_FILE="$local_fed_config" \
        SCHEMA_FILE="$local_schema" \
        PORT="$local_port" \
        MCP_NAME="$local_mcp_name" \
        CONFIG_PROFILE="$local_profile" \
        APP_LANGUAGE="$APP_LANGUAGE" \
        uvicorn backend.api_host.server:get_app --factory \
            --host 0.0.0.0 --port "$local_port" \
            --reload \
            --reload-dir backend \
            2>&1 | sed "s/^/[$local_profile] /" &
        PIDS+=($!)
        PID_LABELS+=("$local_profile (port $local_port)")

        # Brief pause between starts
        if [ $i -lt $((NUM_INSTANCES - 1)) ]; then
            sleep 2
        fi
    done
else
    # Start legacy A/B instances
    GRAPH_FILE="$GRAPH_A" \
    FEDERATION_FILE="$FED_CONFIG_A" \
    PORT="$PORT_A" \
    MCP_NAME="graph-alpha" \
    APP_LANGUAGE="$APP_LANGUAGE" \
    uvicorn backend.api_host.server:get_app --factory \
        --host 0.0.0.0 --port "$PORT_A" \
        --reload \
        --reload-dir backend \
        2>&1 | sed "s/^/[Graph A] /" &
    PIDS+=($!)
    PID_LABELS+=("Graph A (port $PORT_A)")

    # Brief pause to let Graph A start binding its port
    sleep 2

    GRAPH_FILE="$GRAPH_B" \
    FEDERATION_FILE="$FED_CONFIG_B" \
    PORT="$PORT_B" \
    MCP_NAME="graph-beta" \
    APP_LANGUAGE="$APP_LANGUAGE" \
    uvicorn backend.api_host.server:get_app --factory \
        --host 0.0.0.0 --port "$PORT_B" \
        --reload \
        --reload-dir backend \
        2>&1 | sed "s/^/[Graph B] /" &
    PIDS+=($!)
    PID_LABELS+=("Graph B (port $PORT_B)")
fi

# Wait for all processes
wait "${PIDS[@]}"
