"""
App Host - Unified server exposing GraphService over REST and MCP.

This package provides a FastAPI application that combines:
- REST API endpoints via graph_services.rest_api
- MCP tools via graph_services.mcp_tools
- Static file serving for web app and widget

Usage:
    from app_host import create_app

    app = create_app()
    # Run with uvicorn: uvicorn app_host:create_app --factory
"""

from .server import create_app
from .config import AppConfig

__version__ = "1.0.0"
__all__ = ["create_app", "AppConfig"]
