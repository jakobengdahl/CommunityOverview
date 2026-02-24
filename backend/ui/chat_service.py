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

from backend.chat_logic import ChatProcessor
from backend.llm_providers import create_provider, LLMProvider
from backend.service import GraphService


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
        self._current_federation_depth: Optional[int] = None

    def _build_tools_map(self) -> Dict[str, Callable]:
        """
        Build a mapping from tool names to GraphService methods.

        All tool calls are routed through GraphService to ensure
        proper validation and consistency.

        Returns:
            Dict mapping tool names to callable methods
        """
        return {
            "search_graph": self._search_graph_tool,
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
            "get_schema": self._graph_service.get_schema,
            "get_presentation": self._graph_service.get_presentation,
        }


    def _search_graph_tool(
        self,
        query: str,
        node_types: Optional[List[str]] = None,
        limit: int = 50,
        action: Optional[str] = None,
        federation_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        effective_depth = federation_depth if federation_depth is not None else self._current_federation_depth
        return self._graph_service.search_graph(
            query=query,
            node_types=node_types,
            limit=limit,
            action=action,
            federation_depth=effective_depth,
        )

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
        provider: Optional[str] = None,
        federation_depth: Optional[int] = None
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
        self._current_federation_depth = federation_depth
        try:
            return self._processor.process_message(
                messages=messages,
                api_key=api_key,
                provider=provider
            )
        finally:
            self._current_federation_depth = None

    def process_chat_request(
        self,
        user_message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
        document_context: Optional[str] = None,
        federation_depth: Optional[int] = None
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
            provider=provider,
            federation_depth=federation_depth
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

    def propose_nodes_from_text(
        self,
        text: str,
        node_type: Optional[str] = None,
        communities: Optional[List[str]] = None,
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
        federation_depth: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Extract and propose nodes from text using LLM analysis.

        This method:
        1. Analyzes the text using LLM to extract entities
        2. Checks for similar existing nodes using find_similar_nodes_batch
        3. Returns proposed nodes with similarity information for user confirmation

        Args:
            text: The text to analyze (document content)
            node_type: Optional specific node type to extract (Actor, Initiative, etc.)
            communities: Optional list of communities to associate with new nodes
            api_key: Optional API key override
            provider: Optional provider override

        Returns:
            Dict with:
            - proposed_nodes: List of extracted nodes
            - similar_existing: Dict mapping proposed names to similar existing nodes
            - requires_confirmation: Always True (user must confirm before adding)
        """
        # Build the extraction prompt
        type_instruction = ""
        if node_type:
            type_instruction = f"Focus specifically on extracting {node_type} entities."

        community_instruction = ""
        if communities:
            community_instruction = f"Associate extracted nodes with communities: {', '.join(communities)}"

        extraction_prompt = f"""Analyze the following text and extract relevant entities that should be added to the knowledge graph.

{type_instruction}
{community_instruction}

For each entity you find, provide:
1. type: The node type (Actor, Initiative, Capability, Resource, Legislation, Theme)
2. name: The entity name
3. description: A brief description based on the text
4. summary: A one-line summary (max 100 characters)
5. tags: Relevant tags for categorization

Return the entities as a JSON array. Only extract entities that are clearly identifiable and relevant.
Do NOT include generic terms or overly broad categories.

Text to analyze:
---
{text[:8000]}
---

Respond with ONLY a JSON array of extracted entities, no other text. Example format:
[
  {{"type": "Actor", "name": "Example Agency", "description": "...", "summary": "...", "tags": ["tag1", "tag2"]}}
]"""

        messages = [{"role": "user", "content": extraction_prompt}]

        try:
            # Get LLM to extract entities
            key_to_use = api_key if api_key else self._processor.default_api_key
            provider_to_use = provider if provider else self._processor.provider_type

            if not key_to_use:
                return {
                    "success": False,
                    "error": "No API key available",
                    "proposed_nodes": [],
                    "similar_existing": {}
                }

            llm_provider = create_provider(key_to_use, provider_to_use)

            response = llm_provider.create_completion(
                messages=messages,
                system_prompt="You are a precise entity extraction assistant. Extract entities from text and return them as a JSON array.",
                tools=[],
                max_tokens=4096
            )

            # Extract JSON from response
            response_text = ""
            for block in response.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    response_text += block.get("text", "")

            # Parse the JSON response
            import re
            # Find JSON array in response
            json_match = re.search(r'\[[\s\S]*\]', response_text)
            if not json_match:
                return {
                    "success": False,
                    "error": "Could not parse entity extraction result",
                    "proposed_nodes": [],
                    "similar_existing": {}
                }

            proposed_nodes = json.loads(json_match.group())

            # Add communities to each node if specified
            if communities:
                for node in proposed_nodes:
                    node['communities'] = communities

            # Check for similar existing nodes using batch search
            if proposed_nodes:
                names = [node.get('name', '') for node in proposed_nodes if node.get('name')]
                similar_results = self._graph_service.find_similar_nodes_batch(
                    names=names,
                    node_type=node_type,
                    threshold=0.7,
                    limit=3
                )
            else:
                similar_results = {"results": {}}

            return {
                "success": True,
                "proposed_nodes": proposed_nodes,
                "similar_existing": similar_results.get("results", {}),
                "requires_confirmation": True,
                "message": f"Found {len(proposed_nodes)} potential entities. Please review before adding."
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "proposed_nodes": [],
                "similar_existing": {}
            }
