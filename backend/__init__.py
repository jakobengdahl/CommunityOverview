"""
Backend package - Community Knowledge Graph backend services.

This package provides the complete backend functionality organized into:
- core: Graph data structures, storage, and vector search
- service: Business logic layer and API routing
- ui: Chat and document analysis services
- api_host: FastAPI application server

Usage:
    from backend.core import GraphStorage, Node, Edge, NodeType
    from backend.service import GraphService, create_rest_router
    from backend.ui import ChatService, DocumentService
    from backend.api_host import create_app, AppConfig
"""

__version__ = "1.0.0"
