#!/bin/bash
#
# E2E Test Runner
#
# Usage:
#   ./scripts/test-e2e.sh           # Start server, run tests, stop server
#   ./scripts/test-e2e.sh --no-server  # Run tests against existing server
#   E2E_SERVER_URL=http://... ./scripts/test-e2e.sh --no-server
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PORT=${E2E_PORT:-8765}
SERVER_URL=${E2E_SERVER_URL:-"http://localhost:$PORT"}
SERVER_PID=""
START_SERVER=true

# Parse arguments
for arg in "$@"; do
    case $arg in
        --no-server)
            START_SERVER=false
            shift
            ;;
    esac
done

cleanup() {
    if [ -n "$SERVER_PID" ]; then
        echo "Stopping server (PID: $SERVER_PID)..."
        kill $SERVER_PID 2>/dev/null || true
        wait $SERVER_PID 2>/dev/null || true
    fi
}

trap cleanup EXIT

cd "$PROJECT_DIR"

if [ "$START_SERVER" = true ]; then
    echo "Starting server on port $PORT..."

    # Use a temporary graph file for tests
    export GRAPH_FILE="$PROJECT_DIR/backend/test_graph_e2e.json"

    # Create empty graph if not exists
    if [ ! -f "$GRAPH_FILE" ]; then
        echo '{"nodes": [], "edges": []}' > "$GRAPH_FILE"
    fi

    # Start server in background
    uvicorn backend.api_host.server:get_app --factory --host 0.0.0.0 --port $PORT &
    SERVER_PID=$!

    # Wait for server to be ready
    echo "Waiting for server to start..."
    for i in {1..30}; do
        if curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
            echo "Server is ready"
            break
        fi
        sleep 1
        if [ $i -eq 30 ]; then
            echo "Server failed to start within 30 seconds"
            exit 1
        fi
    done
fi

# Run tests
echo "Running E2E tests against $SERVER_URL..."
export E2E_SERVER_URL="$SERVER_URL"
python "$SCRIPT_DIR/test-e2e-live.py"

# Cleanup is handled by trap
echo "E2E tests completed!"
