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
import re
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
        # Cache for resolved AKC configs — avoids repeated 500-node scans within a session.
        # Keyed by short_name; value is (prefix, perms) tuple from _resolve_collection.
        self._collection_cache: Dict[str, tuple] = {}

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
            "delete_edges": self._graph_service.delete_edges,
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

    def _resolve_collection(self, short_name: str) -> tuple:
        """Resolve an AKC short_name → (system_prompt_prefix, permissions_dict).

        Returns:
          - (prefix, perms) on success — both non-None; perms may be {} if no permissions
            are configured (enforced tools still installed, all writes denied).
          - (None, None) when short_name is not found — no enforcement applied.
            Not cached so a collection created after first miss is picked up immediately.
          - ("", {}) on exception — fail-closed; enforced tools installed, all writes denied.
            Not cached so transient errors are retried on the next message.

        permissions_dict maps node_type → {"create": bool, "update": bool, "delete": bool}.
        Successful lookups are cached on this instance to avoid repeated 500-node scans.
        """
        if short_name in self._collection_cache:
            return self._collection_cache[short_name]
        try:
            result = self._graph_service.search_graph(
                query="", node_types=["ActiveKnowledgeCollection"], limit=500
            )
            nodes = result.get("nodes", [])
            found = False
            for node in nodes:
                meta = node.get("metadata") or {}
                if meta.get("short_name") == short_name:
                    found = True
                    raw_perms = meta.get("node_type_permissions") or {}
                    # Guard against individual null entries in the permissions dict
                    perms = {k: (v or {}) for k, v in raw_perms.items()}

                    lines = ["COLLECTION MODE INSTRUCTIONS:"]
                    if meta.get("prompt"):
                        lines.append(meta["prompt"])
                        lines.append("")

                    perm_entries = list(perms.items())
                    any_permitted = any(
                        ops.get(op)
                        for _, ops in perm_entries
                        for op in ("create", "update", "delete")
                    )
                    if any_permitted:
                        lines.append("PERMITTED OPERATIONS:")
                        for node_type, ops in perm_entries:
                            allowed = [op for op in ("create", "update", "delete") if ops.get(op)]
                            if allowed:
                                lines.append(f"- {node_type}: {', '.join(allowed)}")
                    else:
                        lines.append(
                            "PERMITTED OPERATIONS: none — do not create, update, or delete any nodes."
                        )
                    lines.append("")
                    lines.append(
                        "IMPORTANT: Only perform operations that are explicitly listed as "
                        "permitted above. Do not create, update, or delete node types that "
                        "are not listed, or perform operations not permitted for a given type."
                    )

                    lines.append("")
                    lines.append(
                        "INITIALIZATION: If the very first user turn in the conversation "
                        "is '[COLLECTION_START]' and there are no prior assistant messages, "
                        "respond by: 1) briefly explaining that you are an AI assistant, "
                        "2) describing what data you are collecting based on the instructions "
                        "above, and 3) asking your first question to begin. "
                        "Do not re-introduce yourself on subsequent turns. "
                        "Do not mention or repeat '[COLLECTION_START]' in your response."
                    )

                    result_tuple = ("\n".join(lines), perms)
                    self._collection_cache[short_name] = result_tuple
                    return result_tuple

            if not found:
                logger.warning(
                    "AKC short_name %r not found in %d ActiveKnowledgeCollection node(s). "
                    "Collection may exceed the search limit of 500.",
                    short_name, len(nodes),
                )
        except Exception:
            logger.warning("Failed to resolve AKC short_name %r", short_name, exc_info=True)
            # Fail-closed: resolution error → deny all writes rather than fall through
            # to unconstrained mode. Empty perms installs enforced wrappers that block
            # every add/update/delete regardless of node type.
            return ("", {})
        return None, None

    def _make_enforced_tools(self, perms: dict) -> dict:
        """Return a tools_map overlay that enforces node_type_permissions at call time."""

        base_add = self._graph_service.add_nodes
        base_update = self._graph_service.update_node
        base_delete = self._graph_service.delete_nodes

        def _get_node_type(node_id: str) -> Optional[str]:
            """Look up the type of an existing node, returning None on failure."""
            try:
                node_data = self._graph_service.get_node_details(node_id)
                # get_node_details returns {"node": {...}} with type inside the node object
                node_obj = node_data.get("node") or {}
                return (
                    node_obj.get("type")
                    or node_obj.get("nodeType")
                    or node_obj.get("node_type")
                )
            except Exception:
                return None

        def add_nodes_enforced(nodes, edges=None, **kwargs):
            untyped = [
                i for i, n in enumerate(nodes)
                if not isinstance(n.get("type"), str) or not n.get("type")
            ]
            if untyped:
                return {
                    "success": False,
                    "error": (
                        f"Node(s) at position(s) {untyped} are missing a 'type' field. "
                        "Each node must specify a type."
                    ),
                }
            forbidden = sorted({
                n["type"] for n in nodes
                if not perms.get(n["type"], {}).get("create")
            })
            if forbidden:
                return {
                    "success": False,
                    "error": (
                        f"Node type(s) not permitted for creation in this collection: "
                        f"{', '.join(forbidden)}. "
                        "Please only create node types that are listed as permitted."
                    ),
                }
            return base_add(nodes=nodes, edges=edges or [], **kwargs)

        def update_node_enforced(node_id, updates, **kwargs):
            node_type = _get_node_type(node_id)
            if node_type is None:
                # Fail-closed: if we cannot determine the node type, deny the operation.
                # The base call would also return "not found", but the error below is clearer.
                return {
                    "success": False,
                    "error": "Cannot update node: node not found or type could not be determined.",
                }
            if not perms.get(node_type, {}).get("update"):
                return {
                    "success": False,
                    "error": f"Updating {node_type} nodes is not permitted in this collection.",
                }
            return base_update(node_id, updates, **kwargs)

        def delete_nodes_enforced(node_ids, confirmed=False, **kwargs):
            forbidden_ids = []
            unknown_ids = []
            for nid in node_ids:
                node_type = _get_node_type(nid)
                if node_type is None:
                    unknown_ids.append(nid)
                elif not perms.get(node_type, {}).get("delete"):
                    forbidden_ids.append(nid)
            errors = []
            if unknown_ids:
                errors.append(
                    f"not found or type undetermined: {', '.join(unknown_ids)}"
                )
            if forbidden_ids:
                errors.append(
                    f"deletion not permitted in this collection: {', '.join(forbidden_ids)}"
                )
            if errors:
                return {
                    "success": False,
                    "error": "Cannot delete node(s) — " + "; ".join(errors) + ".",
                }
            return base_delete(node_ids=node_ids, confirmed=confirmed, **kwargs)

        def delete_edges_enforced(edge_ids, confirmed=False, **kwargs):
            # Edge deletion is not permitted in collection mode because it can
            # sever relationships between restricted node types without a direct
            # node-type check. Operators who need edge deletion should enable it
            # explicitly — for now we block it uniformly in collection sessions.
            return {
                "success": False,
                "error": "Deleting edges is not permitted in collection mode.",
            }

        return {
            "add_nodes": add_nodes_enforced,
            "update_node": update_node_enforced,
            "delete_nodes": delete_nodes_enforced,
            "delete_edges": delete_edges_enforced,
        }

    @staticmethod
    def _sanitize_id(node_id: str) -> str:
        """Truncate at first C0/DEL control character to prevent prompt injection via node IDs."""
        for i, ch in enumerate(node_id):
            if ch < '\x20' or ch == '\x7f':
                return node_id[:i]
        return node_id

    @staticmethod
    def _format_visualization_context(
        visible_node_ids: Optional[List[str]],
        selected_node_ids: Optional[List[str]],
    ) -> Optional[str]:
        """
        Build a concise system-prompt snippet describing the current canvas state.

        Returns None when visible_node_ids is not provided (unknown canvas state).
        A visible list of [] means the canvas is explicitly empty, which is useful
        context. Caps printed IDs at 100 to avoid bloating the prompt.
        """
        if visible_node_ids is None:
            return None

        id_cap = 100
        visible = [ChatService._sanitize_id(i) for i in visible_node_ids]
        selected = [ChatService._sanitize_id(i) for i in (selected_node_ids or [])]

        if len(visible) <= id_cap:
            id_part = f"Node IDs: [{', '.join(visible)}]" if visible else "Node IDs: []"
        else:
            id_part = f"Node IDs: (omitted — {len(visible)} nodes visible)"

        if not selected:
            selected_part = "Selected nodes: none"
        elif len(selected) <= id_cap:
            selected_part = f"Selected nodes: {len(selected)} ({', '.join(selected)})"
        else:
            selected_part = f"Selected nodes: {len(selected)} (omitted — too many)"

        return (
            "CURRENT VISUALIZATION STATE:\n"
            f"Nodes currently displayed: {len(visible)}\n"
            f"{id_part}\n"
            f"{selected_part}"
        )

    def process_message(
        self,
        messages: List[Dict[str, Any]],
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
        federation_depth: Optional[int] = None,
        expert_agent_id: Optional[str] = None,
        skills_context: Optional[str] = None,
        collection_short_name: Optional[str] = None,
        visible_node_ids: Optional[List[str]] = None,
        selected_node_ids: Optional[List[str]] = None,
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
            visible_node_ids: IDs of nodes currently displayed in the browser
                canvas. Injected as situational context so the AI can distinguish
                between an empty canvas and a populated one.
            selected_node_ids: IDs of nodes the user has selected in the canvas.

        Returns:
            Dict with:
            - content: The text response from the LLM
            - toolUsed: Name of the last tool used (if any)
            - toolResult: Result from the tool (if any)
        """
        effective_prefix, collection_perms = (
            self._resolve_collection(collection_short_name)
            if collection_short_name
            else (None, None)
        )

        self._current_federation_depth = federation_depth
        try:
            expert_context = self._build_expert_context(expert_agent_id) if expert_agent_id else None
            if effective_prefix and expert_context:
                extra_context = f"{effective_prefix}\n\n{expert_context}"
            else:
                extra_context = effective_prefix or expert_context

            # collection_perms is None when no collection is active; {} means a collection
            # was found but has no node_type_permissions configured — enforce even then
            # so the enforced tool wrappers (e.g. delete_edges block) apply regardless.
            tools_override = (
                self._make_enforced_tools(collection_perms)
                if collection_perms is not None
                else None
            )

            visualization_context = self._format_visualization_context(
                visible_node_ids, selected_node_ids
            )

            # skills_context is passed separately as skills_override so it lands
            # AFTER the base system prompt (recency precedence for behavioral overrides).
            # extra_context (expert persona) stays BEFORE the base prompt.
            # visualization_context comes last — most immediate situational snapshot.
            return self._processor.process_message(
                messages=messages,
                api_key=api_key,
                provider=provider,
                extra_context=extra_context,
                skills_override=skills_context or None,
                tools_override=tools_override,
                visualization_context=visualization_context,
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
