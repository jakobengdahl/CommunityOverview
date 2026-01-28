"""
ChatService - Wraps ChatProcessor and integrates with GraphService.

This module provides chat functionality by:
- Using ChatProcessor for LLM interactions
- Routing tool calls through GraphService (not direct graph access)
- Supporting multiple LLM providers (OpenAI, Claude)

All graph mutations MUST go through GraphService to ensure
consistency and proper validation.
"""

from typing import List, Dict, Any, Optional, Callable
import os
import json
import inspect
from datetime import datetime

# Import from parent directory (legacy modules)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from chat_logic import ChatProcessor
from llm_providers import create_provider, LLMProvider
from graph_services import GraphService


class ChatService:
    """
    High-level chat service that handles user conversations.

    This class wraps ChatProcessor and ensures all graph operations
    go through the provided GraphService instance.

    Key responsibilities:
    - Process chat messages through LLM providers
    - Execute tool calls via GraphService methods
    - Handle conversation history
    - Support multiple LLM providers

    Note: This class does NOT access GraphStorage directly.
    All graph operations go through GraphService.
    """

    def __init__(self, graph_service: GraphService):
        """
        Initialize ChatService with a GraphService instance.

        Args:
            graph_service: The GraphService instance to use for all graph operations
        """
        self._graph_service = graph_service

        # Build tools map that routes to GraphService methods
        self._tools_map = self._build_tools_map()

        # Create the underlying ChatProcessor with our tools map
        self._processor = ChatProcessor(self._tools_map)

    def _build_tools_map(self) -> Dict[str, Callable]:
        """
        Build a mapping from tool names to GraphService methods.

        All tool calls are routed through GraphService to ensure
        proper validation and consistency.

        Returns:
            Dict mapping tool names to callable methods
        """
        return {
            "search_graph": self._graph_service.search_graph,
            "get_node_details": self._graph_service.get_node_details,
            "get_related_nodes": self._graph_service.get_related_nodes,
            "find_similar_nodes": self._graph_service.find_similar_nodes,
            "find_similar_nodes_batch": self._graph_service.find_similar_nodes_batch,
            "add_nodes": self._graph_service.add_nodes,
            "update_node": self._graph_service.update_node,
            "delete_nodes": self._graph_service.delete_nodes,
            "list_node_types": self._graph_service.list_node_types,
            "get_graph_stats": self._graph_service.get_graph_stats,
            "save_view": self._graph_service.save_view,
            "get_saved_view": self._graph_service.get_saved_view,
            "list_saved_views": self._graph_service.list_saved_views,
        }

    @property
    def graph_service(self) -> GraphService:
        """Access the underlying GraphService."""
        return self._graph_service

    @property
    def provider_type(self) -> str:
        """Get the current LLM provider type (openai or claude)."""
        return self._processor.provider_type

    def process_message(
        self,
        messages: List[Dict[str, Any]],
        api_key: Optional[str] = None,
        provider: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Process a chat message and return the response.

        This method:
        1. Sends the message to the LLM provider
        2. If the LLM requests tool calls, executes them via GraphService
        3. Returns the final response with any tool results

        Args:
            messages: Conversation history as a list of message dicts
            api_key: Optional API key override (uses env var if not provided)
            provider: Optional provider override ('claude' or 'openai')

        Returns:
            Dict with:
            - content: The text response from the LLM
            - toolUsed: Name of the last tool used (if any)
            - toolResult: Result from the tool (if any)
        """
        return self._processor.process_message(
            messages=messages,
            api_key=api_key,
            provider=provider
        )

    def process_chat_request(
        self,
        user_message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
        document_context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Process a chat request with optional document context.

        This is a convenience method that builds the message list
        and handles document context injection.

        Args:
            user_message: The user's message text
            conversation_history: Optional previous messages
            api_key: Optional API key override
            provider: Optional provider override
            document_context: Optional extracted document text to include

        Returns:
            Dict with response content and tool results
        """
        # Build messages list
        messages = list(conversation_history) if conversation_history else []

        # If document context provided, prepend it to the user message
        if document_context:
            full_message = f"""[Document content uploaded by user:]
---
{document_context[:10000]}
---
[End of document]

User's question: {user_message}"""
        else:
            full_message = user_message

        # Add current user message
        messages.append({
            "role": "user",
            "content": full_message
        })

        return self.process_message(
            messages=messages,
            api_key=api_key,
            provider=provider
        )

    def get_system_info(self) -> Dict[str, Any]:
        """
        Get information about the chat service configuration.

        Returns:
            Dict with provider info and available tools
        """
        return {
            "provider": self._processor.provider_type,
            "available_tools": list(self._tools_map.keys()),
            "graph_stats": self._graph_service.get_graph_stats()
        }
