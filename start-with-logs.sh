#!/bin/bash
# Alternative startup script that shows ALL logs in the terminal
# Use this for debugging instead of ./start-dev.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Starting with Full Logging Enabled${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

# Cleanup function
cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down services...${NC}"

    if [ ! -z "$BACKEND_PID" ]; then
        kill $BACKEND_PID 2>/dev/null || true
    fi

    if [ ! -z "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null || true
    fi

    echo -e "${GREEN}✓ Services stopped${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

# Start backend first
echo -e "${GREEN}Starting Backend...${NC}"
cd "$PROJECT_ROOT/mcp-server"

# Start Python server with output visible
python3 server.py 2>&1 | sed 's/^/[BACKEND] /' &
BACKEND_PID=$!

echo -e "${GREEN}✓ Backend started (PID: $BACKEND_PID)${NC}"
echo -e "  Backend logs will be prefixed with ${BLUE}[BACKEND]${NC}"
echo ""

# Wait for backend to start (increased timeout for loading ML models)
echo -e "${YELLOW}Waiting for backend to start (this may take 10-15 seconds)...${NC}"
sleep 5

# Test if backend is responding (with retries)
MAX_RETRIES=6
RETRY_COUNT=0
BACKEND_READY=false

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -s http://localhost:8000/export_graph > /dev/null 2>&1; then
        BACKEND_READY=true
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo -e "${YELLOW}   Waiting... (attempt $RETRY_COUNT/$MAX_RETRIES)${NC}"
    sleep 2
done

if [ "$BACKEND_READY" = true ]; then
    echo -e "${GREEN}✓ Backend is responding!${NC}"
else
    echo -e "${RED}✗ Backend failed to start or not responding after $((MAX_RETRIES * 2)) seconds${NC}"
    echo -e "${YELLOW}   Check [BACKEND] logs above for errors${NC}"
    cleanup
fi

echo ""

# Start frontend
echo -e "${GREEN}Starting Frontend...${NC}"
cd "$PROJECT_ROOT/frontend"

# Start Vite dev server with output visible
npm run dev 2>&1 | sed 's/^/[FRONTEND] /' &
FRONTEND_PID=$!

echo -e "${GREEN}✓ Frontend started (PID: $FRONTEND_PID)${NC}"
echo -e "  Frontend logs will be prefixed with ${BLUE}[FRONTEND]${NC}"
echo ""

echo -e "${BLUE}================================================${NC}"
echo -e "${GREEN}✓ Both services running with logging${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""
echo -e "📱 Frontend: ${BLUE}http://localhost:3000${NC}"
echo -e "🔌 Backend:  ${BLUE}http://localhost:8000${NC}"
echo ""
echo -e "${YELLOW}Important Instructions:${NC}"
echo ""
echo -e "1. Open browser to: ${BLUE}http://localhost:3000${NC}"
echo -e "   ${RED}This is the correct port for your Vite config${NC}"
echo ""
echo -e "2. Open DevTools (F12) → Console tab"
echo ""
echo -e "3. Do a HARD REFRESH:"
echo -e "   - Windows/Linux: ${BLUE}Ctrl + Shift + R${NC}"
echo -e "   - Mac: ${BLUE}Cmd + Shift + R${NC}"
echo ""
echo -e "4. Click 'Export Graph' button"
echo ""
echo -e "5. Watch THIS terminal for logs:"
echo -e "   ${BLUE}[BACKEND] [Export] Starting graph export...${NC}"
echo -e "   ${BLUE}[BACKEND] [Export] Total nodes in storage: X${NC}"
echo ""
echo -e "6. Watch Browser Console for logs:"
echo -e "   ${BLUE}[Header] Starting graph export...${NC}"
echo -e "   ${BLUE}[Header] Response status: 200${NC}"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop both services${NC}"
echo ""
echo -e "${BLUE}================================================${NC}"
echo ""

# Wait for both processes
wait $BACKEND_PID $FRONTEND_PID
