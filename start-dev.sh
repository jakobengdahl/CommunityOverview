#!/bin/bash
# Community Knowledge Graph - Development Startup Script
# Starts both MCP server (backend) and React frontend (frontend) concurrently

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Project root directory
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Community Knowledge Graph - Development Mode${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

# Function to cleanup on exit
cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down services...${NC}"

    # Kill backend if running
    if [ ! -z "$BACKEND_PID" ]; then
        echo -e "${YELLOW}Stopping backend (PID: $BACKEND_PID)...${NC}"
        kill $BACKEND_PID 2>/dev/null || true
    fi

    # Kill frontend if running
    if [ ! -z "$FRONTEND_PID" ]; then
        echo -e "${YELLOW}Stopping frontend (PID: $FRONTEND_PID)...${NC}"
        kill $FRONTEND_PID 2>/dev/null || true
    fi

    echo -e "${GREEN}✓ Services stopped${NC}"
    exit 0
}

# Set up trap to catch Ctrl+C and other termination signals
trap cleanup SIGINT SIGTERM

# Check for Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ Python 3 is not installed${NC}"
    echo "Please install Python 3.8 or higher"
    exit 1
fi

# Check for Node.js
if ! command -v node &> /dev/null; then
    echo -e "${RED}✗ Node.js is not installed${NC}"
    echo "Please install Node.js 16 or higher"
    exit 1
fi

echo -e "${BLUE}1. Checking Backend Dependencies...${NC}"
cd "$PROJECT_ROOT/mcp-server"

if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating Python virtual environment...${NC}"
    python3 -m venv venv
fi

source venv/bin/activate

# Check if requirements are installed
if ! python -c "import anthropic" 2>/dev/null; then
    echo -e "${YELLOW}Installing backend dependencies...${NC}"
    pip install -q -r requirements.txt
    echo -e "${GREEN}✓ Backend dependencies installed${NC}"
else
    echo -e "${GREEN}✓ Backend dependencies already installed${NC}"
fi

echo ""
echo -e "${BLUE}2. Checking Frontend Dependencies...${NC}"
cd "$PROJECT_ROOT/frontend"

if [ ! -d "node_modules" ]; then
    echo -e "${YELLOW}Installing frontend dependencies...${NC}"
    npm install
    echo -e "${GREEN}✓ Frontend dependencies installed${NC}"
else
    echo -e "${GREEN}✓ Frontend dependencies already installed${NC}"
fi

echo ""
echo -e "${BLUE}3. Checking LLM Provider Configuration...${NC}"

# Check which provider is configured
if [ ! -z "$LLM_PROVIDER" ]; then
    echo -e "${GREEN}✓ LLM_PROVIDER set to: $LLM_PROVIDER${NC}"
elif [ ! -z "$OPENAI_API_KEY" ]; then
    echo -e "${GREEN}✓ OPENAI_API_KEY found (will auto-select OpenAI)${NC}"
elif [ ! -z "$ANTHROPIC_API_KEY" ]; then
    echo -e "${GREEN}✓ ANTHROPIC_API_KEY found (will auto-select Claude)${NC}"
else
    echo -e "${YELLOW}⚠️  No API keys found in environment${NC}"
    echo -e "${YELLOW}   Backend will start but LLM features won't work${NC}"
    echo -e "${YELLOW}   Set OPENAI_API_KEY or ANTHROPIC_API_KEY to enable AI${NC}"
fi

echo ""
echo -e "${BLUE}4. Starting Services...${NC}"
echo ""

# Start backend
echo -e "${GREEN}Starting Backend (MCP Server)...${NC}"
cd "$PROJECT_ROOT/mcp-server"
source venv/bin/activate
python server.py &
BACKEND_PID=$!
echo -e "${GREEN}✓ Backend started (PID: $BACKEND_PID)${NC}"
echo -e "  Backend URL: ${BLUE}http://localhost:8000${NC}"

# Wait a bit for backend to start
sleep 2

# Start frontend
echo -e "${GREEN}Starting Frontend (React)...${NC}"
cd "$PROJECT_ROOT/frontend"
npm run dev &
FRONTEND_PID=$!
echo -e "${GREEN}✓ Frontend started (PID: $FRONTEND_PID)${NC}"
echo -e "  Frontend URL: ${BLUE}http://localhost:5173${NC}"

echo ""
echo -e "${BLUE}================================================${NC}"
echo -e "${GREEN}✓ Both services are running!${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""
echo -e "📱 Frontend: ${BLUE}http://localhost:5173${NC}"
echo -e "🔌 Backend:  ${BLUE}http://localhost:8000${NC}"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop both services${NC}"
echo ""
echo -e "${BLUE}================================================${NC}"
echo ""

# Wait for both processes
wait $BACKEND_PID $FRONTEND_PID
