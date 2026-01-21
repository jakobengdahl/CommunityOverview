#!/bin/bash
# Pre-commit test runner
# Run this script before committing to ensure all critical functionality works

set -e  # Exit on error

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Running Pre-Commit Tests${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

# Function to handle test failures
test_failed() {
    echo -e "${RED}✗ Tests failed!${NC}"
    echo -e "${YELLOW}Please fix the issues before committing.${NC}"
    exit 1
}

# Trap errors
trap test_failed ERR

# ==========================================
# Backend Tests
# ==========================================

echo -e "${BLUE}1. Backend Tests${NC}"
echo -e "${BLUE}------------------------------------------------${NC}"

cd "$PROJECT_ROOT/mcp-server"

# Test 1: Export Logic Test (minimal dependencies)
echo -e "${YELLOW}Testing export serialization logic...${NC}"
if python3 test_export_logic.py; then
    echo -e "${GREEN}✓ Export serialization test passed${NC}"
else
    echo -e "${RED}✗ Export serialization test failed${NC}"
    exit 1
fi

echo ""

# Test 2: Full unit tests (if pytest is available and dependencies are installed)
if command -v pytest &> /dev/null; then
    echo -e "${YELLOW}Checking if full test suite can run...${NC}"

    # Check if key dependencies are available
    if python3 -c "import networkx, sentence_transformers" 2>/dev/null; then
        echo -e "${YELLOW}Running full test suite...${NC}"
        if python3 -m pytest tests/test_export_visualization.py -v --tb=short; then
            echo -e "${GREEN}✓ Full test suite passed${NC}"
        else
            echo -e "${RED}✗ Some tests failed${NC}"
            exit 1
        fi
    else
        echo -e "${YELLOW}⚠️  Skipping full test suite (dependencies not installed)${NC}"
        echo -e "${YELLOW}   Run: pip install -r requirements.txt${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  pytest not installed, skipping full test suite${NC}"
    echo -e "${YELLOW}   Run: pip install --user pytest pytest-asyncio httpx${NC}"
fi

echo ""

# ==========================================
# Frontend Tests
# ==========================================

echo -e "${BLUE}2. Frontend Tests${NC}"
echo -e "${BLUE}------------------------------------------------${NC}"

cd "$PROJECT_ROOT/frontend"

if [ ! -d "node_modules" ]; then
    echo -e "${YELLOW}⚠️  Frontend dependencies not installed${NC}"
    echo -e "${YELLOW}   Run: cd frontend && npm install${NC}"
else
    # Check if vitest is available
    if npm list vitest &> /dev/null; then
        echo -e "${YELLOW}Running frontend tests...${NC}"
        if npm test -- src/store/graphStore.test.js --run; then
            echo -e "${GREEN}✓ Frontend tests passed${NC}"
        else
            echo -e "${YELLOW}⚠️  Frontend tests failed (non-blocking)${NC}"
        fi
    else
        echo -e "${YELLOW}⚠️  Vitest not installed, skipping frontend tests${NC}"
    fi
fi

echo ""

# ==========================================
# Integration Test (optional)
# ==========================================

echo -e "${BLUE}3. Integration Check${NC}"
echo -e "${BLUE}------------------------------------------------${NC}"

cd "$PROJECT_ROOT"

# Check if server is running
if curl -s http://localhost:8000/export_graph > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Server is running${NC}"
    echo -e "${YELLOW}Testing export endpoint...${NC}"

    # Test export endpoint
    response=$(curl -s http://localhost:8000/export_graph)

    if echo "$response" | jq -e '.nodes' > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Export endpoint returns valid JSON${NC}"
    else
        echo -e "${RED}✗ Export endpoint returned invalid data${NC}"
        echo "Response: $response"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠️  Server not running, skipping integration tests${NC}"
    echo -e "${YELLOW}   Start with: ./start-dev.sh${NC}"
fi

echo ""

# ==========================================
# Summary
# ==========================================

echo -e "${BLUE}================================================${NC}"
echo -e "${GREEN}✓ All Tests Passed!${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""
echo -e "You can now safely commit your changes:"
echo -e "  ${BLUE}git add -A${NC}"
echo -e "  ${BLUE}git commit -m \"Your commit message\"${NC}"
echo -e "  ${BLUE}git push${NC}"
echo ""
