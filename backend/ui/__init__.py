"""
UI Backend Package - Chat and document analysis services.

This package provides user-facing functionality for:
- Chat with LLM-powered graph assistant
- Document upload and analysis

Architecture:
- ChatService wraps the existing ChatProcessor
- All graph operations go through GraphService
- DocumentService handles file uploads and text extraction

Key design principle:
Graph mutations ALWAYS go through GraphService, never directly
to GraphStorage. This ensures proper validation and consistency.
"""

from .chat_service import ChatService
from .document_service import DocumentService
from .rest_api import create_ui_router

__all__ = [
    "ChatService",
    "DocumentService",
    "create_ui_router",
]
