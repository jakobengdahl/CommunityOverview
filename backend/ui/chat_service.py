"""
ChatService - Wraps ChatProcessor and integrates with GraphService.

This module provides chat functionality by:
- Using ChatProcessor for LLM interactions
- Routing tool calls through GraphService (not direct graph access)
- Supporting multiple LLM providers (OpenAI, Claude)
- Loading and injecting skills for expert agents (Phase 3)

All graph mutations MUST go through GraphService to ensure
consistency and proper validation.
"""

from typing import List, Dict, Any, Optional, Callable
import asyncio
import logging
import os
import json
import inspect
from datetime import datetime

from backend.chat_logic import ChatProcessor
from backend.llm_providers import create_provider, LLMProvider
from backend.service import GraphService

logger = logging.getLogger(__name__)


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

        # Expert agent registry — populated by load_expert_skills() at startup.
        self._expert_contexts: Dict[str, str] = {}
        # Stage 1: skill metadata (frontmatter only, no body content).
        self._expert_skills: Dict[str, List] = {}       # List[SkillMetadata]
        # Stage 2: full skill content, populated lazily on first _build_expert_context call.
        self._expert_skills_full: Dict[str, List] = {}  # List[SkillDefinition]
        # Shared SkillsLoader instance — holds the raw-text cache for Stage 1→2 promotion.
        self._skills_loader = None

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
            "get_subtypes": self._graph_service.get_subtypes,
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

    # ------------------------------------------------------------------
    # Expert agent skills loading
    # ------------------------------------------------------------------

    async def load_expert_skills(self, experts: list, skills_config) -> None:
        """
        Progressive skill loading (Stage 1 → Stage 2) for all configured expert agents.

        Stage 1: fetches YAML frontmatter only (SkillMetadata), stored in
        _expert_skills.  Stage 2: immediately promotes each set of metadata
        to full SkillDefinition objects (with body content) using the shared
        SkillsLoader text cache, so no additional HTTP round-trips are needed.
        Full content is stored in _expert_skills_full and used by
        _build_expert_context() at request time without any async work.

        Both stages run in this async context, eliminating any need to spawn
        a new event loop inside synchronous request handlers.

        Clears all expert state before loading so that experts removed from
        config are not retained across reloads.

        Args:
            experts: List of ExpertAgentConfig objects
            skills_config: SkillsConfig to use for the loader
        """
        from backend.skills.loader import SkillsLoader

        # Clear stale state from any previous load (handles config reload)
        self._expert_contexts.clear()
        self._expert_skills.clear()
        self._expert_skills_full.clear()

        self._skills_loader = SkillsLoader(skills_config)
        for expert in experts:
            self._expert_contexts[expert.id] = expert.system_context
            if expert.skills_urls:
                try:
                    # Stage 1: frontmatter only (text cached for Stage 2)
                    metas = await self._skills_loader.load_metadata_from_urls(expert.skills_urls)
                    self._expert_skills[expert.id] = metas
                    # Stage 2: full content — uses text cache, no extra HTTP requests
                    full_skills = await self._skills_loader.load_full_skills(metas)
                    self._expert_skills_full[expert.id] = full_skills
                    logger.info(
                        "Expert %s: loaded %d skill(s) from %d URL(s)",
                        expert.id, len(full_skills), len(expert.skills_urls),
                    )
                except Exception as exc:
                    logger.warning("Expert %s: skills load failed: %s", expert.id, exc)
                    self._expert_skills[expert.id] = []
                    self._expert_skills_full[expert.id] = []
            else:
                self._expert_skills[expert.id] = []
                self._expert_skills_full[expert.id] = []

    def load_expert_skills_sync(self, experts: list, skills_config) -> None:
        """Synchronous wrapper for startup use outside an async context."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.load_expert_skills(experts, skills_config))
        finally:
            loop.close()

    def _build_expert_context(self, expert_agent_id: str) -> Optional[str]:
        """
        Build the extra system context string for a given expert agent.

        Full skill content is pre-loaded by load_expert_skills() at startup
        and stored in _expert_skills_full — this method is purely a read.

        Returns None if the ID is unknown, or a combined string of:
          - expert's system_context (if set)
          - skills section (if skills were loaded for this expert)
        """
        if expert_agent_id not in self._expert_contexts:
            logger.debug("Unknown expert_agent_id: %s", expert_agent_id)
            return None

        from backend.agents.prompts import build_skills_section
        from backend.skills.loader import SkillDefinition

        parts: List[str] = []
        ctx = self._expert_contexts.get(expert_agent_id, "")
        if ctx:
            parts.append(ctx)

        # Full skills pre-populated by load_expert_skills().
        # Fall back to _expert_skills if items are already SkillDefinition
        # (test-compat: tests may set _expert_skills directly).
        full_skills = self._expert_skills_full.get(expert_agent_id)
        if full_skills is None:
            items = self._expert_skills.get(expert_agent_id, [])
            full_skills = [s for s in items if isinstance(s, SkillDefinition)]
        if full_skills:
            parts.append(build_skills_section(full_skills))

        return "\n\n".join(parts) if parts else None

    # ------------------------------------------------------------------

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
        federation_depth: Optional[int] = None,
        expert_agent_id: Optional[str] = None,
        skills_context: Optional[str] = None,
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
            federation_depth: Optional federated search depth override
            expert_agent_id: Optional expert agent ID — when provided, the
                agent's system_context and skills are prepended to the system
                prompt for this request.
            skills_context: Optional temporary skill instructions built from
                Skill nodes the user selected in the visualization. Injected
                as extra system context for this single request only; it is
                NOT persisted in conversation history.

        Returns:
            Dict with:
            - content: The text response from the LLM
            - toolUsed: Name of the last tool used (if any)
            - toolResult: Result from the tool (if any)
        """
        self._current_federation_depth = federation_depth
        try:
            extra_context = self._build_expert_context(expert_agent_id) if expert_agent_id else None
            if expert_agent_id:
                if extra_context:
                    full = self._expert_skills_full.get(expert_agent_id)
                    skill_count = len(full or self._expert_skills.get(expert_agent_id) or [])
                    logger.info(
                        "Chat request routed to expert '%s' (skills loaded: %d)",
                        expert_agent_id, skill_count,
                    )
                else:
                    logger.warning(
                        "Chat request specified expert '%s' but no context built"
                        " — expert may not be registered",
                        expert_agent_id,
                    )
            # Merge Skill-node instructions (from frontend selection) into extra_context.
            # skills_context is active for this single request only and is NOT stored
            # in conversation history, so the persona doesn't bleed into later turns.
            if skills_context:
                extra_context = (
                    f"{extra_context}\n\n{skills_context}"
                    if extra_context
                    else skills_context
                )

            return self._processor.process_message(
                messages=messages,
                api_key=api_key,
                provider=provider,
                extra_context=extra_context,
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
        federation_depth: Optional[int] = None,
        expert_agent_id: Optional[str] = None,
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
            federation_depth=federation_depth,
            expert_agent_id=expert_agent_id,
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
1. type: The node type (Actor, Initiative, Capability, Resource, Legislation, Theme, Data, Risk, Goal, Event)
2. name: The entity name
3. description: A brief description based on the text
4. summary: A one-line summary (max 100 characters)
5. tags: Relevant tags for categorization
6. subtypes: Sub-classifications within the node type (e.g., for Actor: "Government agency", "Municipality")

Return the entities as a JSON array. Only extract entities that are clearly identifiable and relevant.
Do NOT include generic terms or overly broad categories.

Text to analyze:
---
{text[:8000]}
---

Respond with ONLY a JSON array of extracted entities, no other text. Example format:
[
  {{"type": "Actor", "name": "Example Agency", "description": "...", "summary": "...", "tags": ["tag1", "tag2"], "subtypes": ["Government agency"]}}
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
