"""
backend.api_host - Unified server exposing GraphService over REST and MCP.

This package provides a FastAPI application that combines:
- REST API endpoints via backend.service.rest_api
- MCP tools via backend.service.mcp_tools
- Static file serving for web app and widget

Usage:
    from backend.api_host import create_app

    app = create_app()
    # Run with uvicorn: uvicorn backend.api_host.server:get_app --factory
"""

from .server import create_app
from .config import AppConfig

__version__ = "1.0.0"
__all__ = ["create_app", "AppConfig"]
