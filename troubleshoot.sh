#!/bin/bash
# Troubleshooting script for "Failed to export graph" issue
# Run this while the system is running with ./start-dev.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Export Troubleshooting Diagnostics${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

# Test 1: Is backend running?
echo -e "${YELLOW}[1/5] Checking if backend is running...${NC}"
if curl -s http://localhost:8000/export_graph > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Backend is responding on port 8000${NC}"
else
    echo -e "${RED}✗ Backend is NOT responding on port 8000${NC}"
    echo -e "${YELLOW}    Solution: Check if backend started correctly${NC}"
    echo -e "${YELLOW}    Run: ps aux | grep 'python.*server.py'${NC}"
    exit 1
fi

echo ""

# Test 2: Can we get valid JSON from export endpoint?
echo -e "${YELLOW}[2/5] Testing export endpoint...${NC}"
RESPONSE=$(curl -s http://localhost:8000/export_graph)

if echo "$RESPONSE" | python3 -m json.tool > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Export endpoint returns valid JSON${NC}"

    # Parse and show stats
    NODES=$(echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('total_nodes', 0))" 2>/dev/null || echo "?")
    EDGES=$(echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('total_edges', 0))" 2>/dev/null || echo "?")

    echo -e "    Nodes: ${BLUE}$NODES${NC}, Edges: ${BLUE}$EDGES${NC}"
else
    echo -e "${RED}✗ Export endpoint returned invalid JSON${NC}"
    echo -e "${YELLOW}    Response: $RESPONSE${NC}"
    exit 1
fi

echo ""

# Test 3: Is frontend running?
echo -e "${YELLOW}[3/5] Checking if frontend is running...${NC}"
if curl -s http://localhost:5173 > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Frontend is responding on port 5173${NC}"
else
    echo -e "${RED}✗ Frontend is NOT responding on port 5173${NC}"
    echo -e "${YELLOW}    Solution: Check if frontend started correctly${NC}"
    echo -e "${YELLOW}    Run: ps aux | grep 'vite\\|npm'${NC}"
    exit 1
fi

echo ""

# Test 4: Check if Header.jsx has the logging code
echo -e "${YELLOW}[4/5] Checking if logging code exists in Header.jsx...${NC}"
if grep -q "console.log.*Header.*Starting graph export" frontend/src/components/Header.jsx 2>/dev/null; then
    echo -e "${GREEN}✓ Logging code found in Header.jsx${NC}"
else
    echo -e "${RED}✗ Logging code NOT found in Header.jsx${NC}"
    echo -e "${YELLOW}    This might be a git checkout issue${NC}"
    exit 1
fi

echo ""

# Test 5: Show current git status
echo -e "${YELLOW}[5/5] Git status check...${NC}"
MODIFIED=$(git status --porcelain | grep -c "^ M" || echo "0")
UNTRACKED=$(git status --porcelain | grep -c "^??" || echo "0")

if [ "$MODIFIED" -gt 0 ] || [ "$UNTRACKED" -gt 0 ]; then
    echo -e "${YELLOW}⚠️  You have uncommitted changes:${NC}"
    echo -e "    Modified: ${BLUE}$MODIFIED${NC} files"
    echo -e "    Untracked: ${BLUE}$UNTRACKED${NC} files"
    echo -e "${YELLOW}    These changes might not be reflected in the running app${NC}"
else
    echo -e "${GREEN}✓ No uncommitted changes${NC}"
fi

echo ""
echo -e "${BLUE}================================================${NC}"
echo -e "${GREEN}✓ All Backend/Frontend Health Checks Passed${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""
echo -e "${YELLOW}Next Steps for Browser Testing:${NC}"
echo ""
echo -e "1. Open browser to: ${BLUE}http://localhost:5173${NC}"
echo -e "   ${RED}NOT http://localhost:3000${NC}"
echo ""
echo -e "2. Open DevTools (F12) → Console tab"
echo ""
echo -e "3. Do a HARD REFRESH to clear cache:"
echo -e "   - Windows/Linux: ${BLUE}Ctrl + Shift + R${NC}"
echo -e "   - Mac: ${BLUE}Cmd + Shift + R${NC}"
echo ""
echo -e "4. Click 'Export Graph' button in header"
echo ""
echo -e "5. You should see these logs in Console:"
echo -e "   ${BLUE}[Header] Starting graph export...${NC}"
echo -e "   ${BLUE}[Header] Fetching from http://localhost:8000/export_graph${NC}"
echo -e "   ${BLUE}[Header] Response status: 200${NC}"
echo ""
echo -e "6. If you see ${RED}'Failed to export graph'${NC}:"
echo -e "   - Check this terminal for backend errors"
echo -e "   - Look for ${BLUE}[Export] ERROR:${NC} messages"
echo ""
echo -e "${YELLOW}Common Issues:${NC}"
echo ""
echo -e "• ${RED}No logs in browser Console${NC}"
echo -e "  → Hard refresh (Ctrl+Shift+R) to reload JavaScript"
echo -e "  → Make sure you're on http://localhost:5173"
echo -e "  → Check DevTools → Sources tab, verify Header.jsx is loaded"
echo ""
echo -e "• ${RED}Failed to fetch${NC}"
echo -e "  → Backend not running or crashed"
echo -e "  → Check terminal where ./start-dev.sh is running"
echo -e "  → Look for Python errors"
echo ""
echo -e "• ${RED}500 error${NC}"
echo -e "  → Backend is running but export endpoint has error"
echo -e "  → Check terminal for ${BLUE}[Export] ERROR:${NC} with traceback"
echo ""
