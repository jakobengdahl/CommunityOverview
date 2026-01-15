@echo off
REM Community Knowledge Graph - Development Startup Script (Windows)
REM Starts both MCP server (backend) and React frontend (frontend) concurrently

setlocal enabledelayedexpansion

echo ================================================
echo   Community Knowledge Graph - Development Mode
echo ================================================
echo.

REM Check for Python
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not installed
    echo Please install Python 3.8 or higher
    pause
    exit /b 1
)

REM Check for Node.js
where node >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Node.js is not installed
    echo Please install Node.js 16 or higher
    pause
    exit /b 1
)

echo [1/4] Checking Backend Dependencies...
cd /d "%~dp0mcp-server"

if not exist "venv" (
    echo Creating Python virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

REM Check if requirements are installed
python -c "import anthropic" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Installing backend dependencies...
    pip install -q -r requirements.txt
    echo [OK] Backend dependencies installed
) else (
    echo [OK] Backend dependencies already installed
)

echo.
echo [2/4] Checking Frontend Dependencies...
cd /d "%~dp0frontend"

if not exist "node_modules" (
    echo Installing frontend dependencies...
    call npm install
    echo [OK] Frontend dependencies installed
) else (
    echo [OK] Frontend dependencies already installed
)

echo.
echo [3/4] Checking LLM Provider Configuration...

if defined LLM_PROVIDER (
    echo [OK] LLM_PROVIDER set to: %LLM_PROVIDER%
) else if defined OPENAI_API_KEY (
    echo [OK] OPENAI_API_KEY found (will auto-select OpenAI)
) else if defined ANTHROPIC_API_KEY (
    echo [OK] ANTHROPIC_API_KEY found (will auto-select Claude)
) else (
    echo [WARNING] No API keys found in environment
    echo            Backend will start but LLM features won't work
    echo            Set OPENAI_API_KEY or ANTHROPIC_API_KEY to enable AI
)

echo.
echo [4/4] Starting Services...
echo.

REM Start backend in new window
echo Starting Backend (MCP Server)...
cd /d "%~dp0mcp-server"
start "Backend - MCP Server" cmd /k "venv\Scripts\activate.bat && python server.py"
echo [OK] Backend started
echo      Backend URL: http://localhost:8000

REM Wait a bit for backend to start
timeout /t 2 /nobreak >nul

REM Start frontend in new window
echo Starting Frontend (React)...
cd /d "%~dp0frontend"
start "Frontend - React" cmd /k "npm run dev"
echo [OK] Frontend started
echo      Frontend URL: http://localhost:5173

echo.
echo ================================================
echo [OK] Both services are running!
echo ================================================
echo.
echo Frontend: http://localhost:5173
echo Backend:  http://localhost:8000
echo.
echo Close the terminal windows to stop the services
echo.
echo ================================================

REM Keep this window open
pause
