# Development Startup Scripts

Easy-to-use scripts for starting the Community Knowledge Graph development environment.

## Available Scripts

### `start-dev.sh` (Linux/Mac)

Bash script that starts both backend and frontend services concurrently.

**Usage:**
```bash
./start-dev.sh
```

**Features:**
- ✅ Automatically checks for Python 3 and Node.js
- ✅ Creates Python virtual environment if needed
- ✅ Installs backend dependencies if missing
- ✅ Installs frontend dependencies if missing
- ✅ Checks LLM provider configuration
- ✅ Starts backend on port 8000
- ✅ Starts frontend on port 5173
- ✅ Shows clear status messages with colors
- ✅ Graceful shutdown with Ctrl+C

**Environment Variables:**

The script will detect and use these environment variables:

```bash
# LLM Provider (optional - auto-detected if not set)
export LLM_PROVIDER=openai        # or 'claude'

# API Keys (at least one required for LLM features)
export OPENAI_API_KEY=sk-xxxxx
export ANTHROPIC_API_KEY=sk-ant-xxxxx

# Optional: OpenAI model selection
export OPENAI_MODEL=gpt-4o        # default if not set
```

**Example:**
```bash
# Set your API key
export OPENAI_API_KEY=sk-proj-xxxxx

# Start everything
./start-dev.sh

# Output:
# ================================================
#   Community Knowledge Graph - Development Mode
# ================================================
#
# 1. Checking Backend Dependencies...
# ✓ Backend dependencies already installed
#
# 2. Checking Frontend Dependencies...
# ✓ Frontend dependencies already installed
#
# 3. Checking LLM Provider Configuration...
# ✓ OPENAI_API_KEY found (will auto-select OpenAI)
#
# 4. Starting Services...
#
# Starting Backend (MCP Server)...
# ✓ Backend started (PID: 12345)
#   Backend URL: http://localhost:8000
# Starting Frontend (React)...
# ✓ Frontend started (PID: 12346)
#   Frontend URL: http://localhost:5173
#
# ================================================
# ✓ Both services are running!
# ================================================
#
# 📱 Frontend: http://localhost:5173
# 🔌 Backend:  http://localhost:8000
#
# Press Ctrl+C to stop both services
```

### `start-dev.bat` (Windows)

Batch script for Windows that starts backend and frontend in separate windows.

**Usage:**
```cmd
start-dev.bat
```

**Features:**
- ✅ Checks for Python and Node.js
- ✅ Creates virtual environment if needed
- ✅ Installs dependencies if missing
- ✅ Opens backend in separate terminal window
- ✅ Opens frontend in separate terminal window
- ✅ Shows clear status messages

**Environment Variables:**

Same as Linux/Mac version. Set them before running:

```cmd
REM Set your API key
set OPENAI_API_KEY=sk-xxxxx

REM Start everything
start-dev.bat
```

## Stopping Services

### Linux/Mac
Press `Ctrl+C` in the terminal where the script is running. The script will gracefully shut down both services.

### Windows
Close the individual terminal windows for backend and frontend, or press `Ctrl+C` in each window.

## Troubleshooting

### "Permission denied" on Linux/Mac

Make the script executable:
```bash
chmod +x start-dev.sh
```

### "Python not found"

Install Python 3.8 or higher:
- **Linux:** `sudo apt install python3 python3-venv`
- **Mac:** `brew install python3`
- **Windows:** Download from https://python.org

### "Node not found"

Install Node.js 16 or higher:
- **Linux:** `sudo apt install nodejs npm`
- **Mac:** `brew install node`
- **Windows:** Download from https://nodejs.org

### "No API keys found"

The services will start but AI features won't work. Set at least one API key:

```bash
# For OpenAI
export OPENAI_API_KEY=sk-xxxxx

# OR for Claude
export ANTHROPIC_API_KEY=sk-ant-xxxxx
```

### Port Already in Use

If ports 8000 or 5173 are already in use:

1. **Find and kill the process:**
   ```bash
   # Linux/Mac
   lsof -ti:8000 | xargs kill
   lsof -ti:5173 | xargs kill

   # Windows
   netstat -ano | findstr :8000
   taskkill /PID <PID> /F
   ```

2. **Or change the ports:**
   - Backend: Edit `mcp-server/server.py` and change port in `uvicorn.run()`
   - Frontend: Edit `frontend/vite.config.js` and change `server.port`

### Dependencies Not Installing

**Backend:**
```bash
cd mcp-server
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install --upgrade pip
pip install -r requirements.txt
```

**Frontend:**
```bash
cd frontend
rm -rf node_modules package-lock.json
npm install
```

## Advanced Usage

### Start with Specific Provider

```bash
# Force Claude even if OpenAI key is present
LLM_PROVIDER=claude ./start-dev.sh

# Force OpenAI even if Claude key is present
LLM_PROVIDER=openai ./start-dev.sh
```

### Run in Background (Linux/Mac)

```bash
nohup ./start-dev.sh > dev.log 2>&1 &

# View logs
tail -f dev.log

# Stop services
pkill -f "python server.py"
pkill -f "npm run dev"
```

### Custom Environment File

Create a `.env` file in the project root:

```bash
# .env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxx
OPENAI_MODEL=gpt-4o
```

Then start:
```bash
source .env
./start-dev.sh
```

## What Each Service Does

### Backend (MCP Server) - Port 8000
- FastAPI HTTP server
- Processes AI requests (Claude or OpenAI)
- Manages graph storage (NetworkX + JSON)
- Handles tool execution (search, add, update, delete nodes)
- Vector similarity search
- Document processing

### Frontend (React) - Port 5173
- React app with React Flow visualization
- Chat interface for interacting with AI
- Interactive graph visualization
- File upload for document analysis
- Settings UI for API key configuration

## Development Tips

### Hot Reload
Both services support hot reload:
- **Backend:** Uvicorn auto-reloads on Python file changes
- **Frontend:** Vite HMR reloads on React file changes

### Logs
- **Backend:** Shows in terminal with color-coded output
- **Frontend:** Shows in terminal + browser console (F12)

### Testing Different Providers

Quickly switch between providers:

```bash
# Terminal 1: OpenAI
export OPENAI_API_KEY=sk-xxxxx
./start-dev.sh

# Ctrl+C to stop

# Terminal 2: Claude
export ANTHROPIC_API_KEY=sk-ant-xxxxx
./start-dev.sh
```

### Development Workflow

1. Start services: `./start-dev.sh`
2. Make changes to code
3. Save files (services auto-reload)
4. Test in browser: http://localhost:5173
5. Check backend logs in terminal
6. Repeat steps 2-5
7. Stop with Ctrl+C when done

## Integration with IDEs

### VS Code

Add to `.vscode/tasks.json`:

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Start Dev Environment",
      "type": "shell",
      "command": "./start-dev.sh",
      "problemMatcher": [],
      "presentation": {
        "reveal": "always",
        "panel": "new"
      }
    }
  ]
}
```

Then press `Ctrl+Shift+P` → "Tasks: Run Task" → "Start Dev Environment"

### PyCharm / WebStorm

1. Create new "Shell Script" run configuration
2. Set script path to `start-dev.sh`
3. Set working directory to project root
4. Run with play button

## See Also

- [README.md](./README.md) - Main project documentation
- [LLM_PROVIDERS.md](./LLM_PROVIDERS.md) - Detailed LLM configuration
- [TROUBLESHOOTING_OPENAI.md](./TROUBLESHOOTING_OPENAI.md) - OpenAI-specific troubleshooting
