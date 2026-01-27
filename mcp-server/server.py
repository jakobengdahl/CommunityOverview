"""
MCP Server for Community Knowledge Graph
Exposes tools for graph operations via MCP and REST API

This module provides the HTTP/MCP interface layer using:
- graph_core: Core graph storage and persistence
- graph_services: Service layer with REST API and MCP tools

Architecture:
    graph_core (storage) -> graph_services (business logic) -> server.py (transport)
"""

from typing import List, Optional, Dict, Any
import os
import tempfile
import shutil
import requests
from datetime import datetime
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# Import from graph_core package
from graph_core import GraphStorage

# Import from graph_services package
from graph_services import GraphService, register_mcp_tools, json_serializer

from document_processor import DocumentProcessor

# Initialize MCP server
mcp = FastMCP("community-knowledge-graph")

# Initialize graph storage and service
graph_storage = GraphStorage("graph.json")
graph_service = GraphService(graph_storage)

# Register MCP tools and get tools map for ChatProcessor
TOOLS_MAP = register_mcp_tools(mcp, graph_service)


# --- Chat Endpoint (Backend Integration) ---

from chat_logic import ChatProcessor

# Initialize ChatProcessor (lazy load to avoid circular deps if any)
chat_processor = None

def get_chat_processor():
    global chat_processor
    if not chat_processor:
        chat_processor = ChatProcessor(TOOLS_MAP)
    return chat_processor


@mcp.custom_route("/chat", methods=["POST"])
async def chat_endpoint(request: Request):
    """
    Endpoint for the frontend to chat with LLM (Claude or OpenAI) via the backend.
    """
    try:
        body = await request.json()
        messages = body.get("messages", [])

        if not messages:
            return JSONResponse({"error": "No messages provided"}, status_code=400)

        # Check for API key in header (user-provided key takes precedence)
        api_key = request.headers.get("X-OpenAI-API-Key") or request.headers.get("X-Anthropic-API-Key")

        # Check for provider in header - but only use it if user provided their own API key
        provider_header = request.headers.get("X-LLM-Provider")
        provider = provider_header if api_key else None

        processor = get_chat_processor()
        result = processor.process_message(messages, api_key=api_key, provider=provider)

        import json
        return JSONResponse(json.loads(json.dumps(result, default=json_serializer)))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/upload", methods=["POST"])
async def upload_endpoint(request: Request):
    """
    Endpoint to upload files and extract text
    """
    try:
        form = await request.form()
        file = form.get("file")

        if not file:
            return JSONResponse({"error": "No file provided"}, status_code=400)

        filename = file.filename

        # Save temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name

        try:
            text = DocumentProcessor.extract_text(tmp_path)

            return JSONResponse({
                "success": True,
                "filename": filename,
                "text": text,
                "message": f"Successfully extracted {len(text)} characters"
            })

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/download_url", methods=["POST"])
async def download_url_endpoint(request: Request):
    """
    Endpoint to download a document from a URL and extract text
    """
    try:
        body = await request.json()
        url = body.get("url")

        if not url:
            return JSONResponse({"error": "No URL provided"}, status_code=400)

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return JSONResponse({"error": "Invalid URL"}, status_code=400)

        try:
            response = requests.get(url, timeout=30, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; CommunityGraph/1.0)'
            })
            response.raise_for_status()
        except requests.RequestException as e:
            return JSONResponse({"error": f"Failed to download file: {str(e)}"}, status_code=400)

        content_type = response.headers.get('Content-Type', '')
        filename = parsed.path.split('/')[-1] or 'document'

        if not os.path.splitext(filename)[1]:
            if 'pdf' in content_type:
                filename += '.pdf'
            elif 'word' in content_type or 'msword' in content_type:
                filename += '.docx'
            elif 'text' in content_type:
                filename += '.txt'

        ext = os.path.splitext(filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        try:
            text = DocumentProcessor.extract_text(tmp_path)

            return JSONResponse({
                "success": True,
                "filename": filename,
                "url": url,
                "text": text,
                "message": f"Successfully downloaded and extracted {len(text)} characters"
            })

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/export_graph", methods=["GET"])
async def export_graph_endpoint(request: Request):
    """
    Endpoint to export the entire graph (all nodes and edges)
    """
    try:
        import json
        result = graph_service.export_graph()
        return JSONResponse(result)
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        return JSONResponse({"error": str(e), "traceback": error_trace}, status_code=500)


@mcp.custom_route("/execute_tool", methods=["POST"])
async def execute_tool_endpoint(request: Request):
    """
    Endpoint for the frontend to execute tools directly
    """
    try:
        body = await request.json()
        tool_name = body.get("tool_name")
        arguments = body.get("arguments", {})

        if not tool_name:
            return JSONResponse({"error": "No tool_name provided"}, status_code=400)

        if tool_name not in TOOLS_MAP:
            return JSONResponse({"error": f"Tool {tool_name} not found"}, status_code=404)

        func = TOOLS_MAP[tool_name]
        result = func(**arguments)

        import json
        return JSONResponse(json.loads(json.dumps(result, default=json_serializer)))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


# Instructions for LLM when using MCP
SYSTEM_PROMPT = """
You are a helpful assistant for the Community Knowledge Graph system.

TERMINOLOGY:
- "Current visualization" = what is currently displayed in the GUI
- "Saved view" = a saved snapshot of nodes/edges/positions stored in the graph that can be loaded

METAMODEL:
- Actor (blue): Government agencies, organizations
- Community (purple): eSam, Myndigheter, Officiell Statistik
- Initiative (green): Projects, collaborative activities
- Capability (orange): Capabilities
- Resource (yellow): Reports, software
- Legislation (red): NIS2, GDPR
- Theme (teal): AI, data strategies
- SavedView (gray): Saved graph view snapshots

SECURITY RULES:
1. ALWAYS warn if the user tries to store personal data
2. For deletion: Max 10 nodes, require double confirmation
3. Always filter based on the user's active communities

WORKFLOW FOR ADDING NODES:
1. Run find_similar_nodes() to find duplicates
2. Present proposal + similar existing nodes
3. Wait for user approval
4. Run add_nodes() only after approval

WORKFLOW FOR DOCUMENT UPLOAD:
1. Extract text from document
2. Identify potential nodes according to metamodel
3. Run find_similar_nodes() for each
4. Present proposal + duplicates
5. Let user choose what to add
6. Automatically link to user's active communities

Always be clear about what you're doing and ask for confirmation for important operations.

TONE: Use a neutral, professional tone. Avoid excessive enthusiasm and superlatives (e.g., "Utmärkt!", "Perfekt!").
Start responses directly with information rather than enthusiastic acknowledgments.
"""


if __name__ == "__main__":
    # Start MCP server
    print("Starting Community Knowledge Graph MCP Server...")
    print(f"Loaded graph with {len(graph_storage.nodes)} nodes and {len(graph_storage.edges)} edges")
    print(SYSTEM_PROMPT)

    # Run as HTTP server on port 8000 (required for frontend)
    import uvicorn
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
