"""
MCP (Model Context Protocol) tools registration for graph operations.

This module registers all GraphService methods as MCP tools that can be
called by LLMs through the MCP protocol.

Usage:
    from mcp.server.fastmcp import FastMCP
    from backend.service import GraphService, register_mcp_tools
    from backend.core import GraphStorage

    mcp = FastMCP("community-knowledge-graph")
    storage = GraphStorage("graph.json")
    service = GraphService(storage)
    tools_map = register_mcp_tools(mcp, service)
"""

import secrets
from typing import List, Optional, Dict, Any, Callable

from backend.core.session_auto_add import AutoAddRuleError
from backend.core.storage_search import MATCH_MODE_SUBSTRING
from backend.core.session_store import OpError, is_valid_session_id
from backend.core.session_manager import (
    AnnotationNotFound,
    AnnotationRecentlyDeleted,
    ImageBudgetExceeded,
    LayoutBusy,
    OpBatchTooLarge,
    RateLimited,
    RevisionConflict,
    SessionLimitReached,
    SessionNotFound,
)
from backend.core.image_ingest import (
    ImageFetchError,
    InvalidImageData,
    OptimizedImageTooLarge,
    SourceImageTooLarge,
    UnsupportedImageType,
    decode_image_data,
    fetch_image_bytes,
    optimize_image,
)
from backend.core.session_annotations import (
    build_note_annotation,
    build_note_patch,
    is_note,
    project_note,
    ALL_ANNOTATION_TYPES,
    ATTACHABLE_ANNOTATION_TYPES,
    GENERIC_ANNOTATION_TYPES,
    IMAGE_TYPE,
    annotation_type_of,
    build_annotation,
    build_annotation_patch,
    build_group_annotation,
    is_generic_annotation,
    is_group,
    normalize_generic_type,
    project_annotation,
    resolve_annotation_type_alias,
    translate_freehand_points,
    translate_line_endpoints,
)
from backend.config.config_loader import build_session_url
from backend.runtime.authorization import GRAPH_ACTION_MUTATE, GRAPH_ACTION_READ
from . import access
from .service import GraphService

# Server-owned session state records node x/y but not rendered node dimensions
# (design §3.8: the browser no longer uploads canvas geometry). Agents still need
# a size to space nodes without overlap, so the layout tools advertise this
# assumed default — model-space units at zoom 1, matching the canvas node box.
_ASSUMED_NODE_SIZE = {"width": 220, "height": 120}

# Stable client id the shared-session op protocol attributes MCP layout writes to,
# so an AI agent is just another collaborator (design 3.8) and rate limiting /
# presence group all its writes together.
_MCP_LAYOUT_CLIENT_ID = "mcp-agent"

# The same agent identity, used to attribute session lifecycle writes (rename,
# delete) so an assistant's session management is auditable as one actor.
_MCP_SESSION_CLIENT_ID = "mcp-agent"

# Server-assigned default when an assistant creates a session without a name
# (contract §4: names are non-unique and the server fills a default).
_DEFAULT_SESSION_NAME = "Untitled session"


def register_mcp_tools(
    mcp,
    service: GraphService,
    session_registry=None,
    session_manager=None,
    auto_add_registry=None,
) -> Dict[str, Callable]:
    """
    Register all GraphService methods as MCP tools.

    Args:
        mcp: FastMCP instance to register tools with
        service: GraphService instance to use for operations
        session_registry: legacy single-consumer visualization push registry
        session_manager: new shared-session manager; pushes are additionally
            broadcast to its hub subscribers so an AI agent is just another
            collaborator (design 3.8)
        auto_add_registry: SessionAutoAddRegistry backing the session-scoped
            auto-add agents (create/list/remove tools below). None disables
            those tools (they return an error).

    Returns:
        Dict mapping tool names to their functions (for ChatProcessor)
    """
    tools_map = {}

    def _push(session_id, tool_name, result):
        _push_to_session(
            session_registry, session_id, tool_name, result, session_manager
        )

    def _claimed_node_ids(session_id, node_refs):
        """The session's *node* ids that currently hold a selection claim.

        Claims are advisory soft-locks on session *elements*, so the claim map
        can hold edge ids as well as node ids. Both read tools report this as
        ``selected_node_ids``, so it is narrowed to the session's node
        references — an agent must be able to feed the field straight into a
        node argument such as ``apply_visualization_layout``'s positions map.
        """
        refs = set(node_refs)
        return [e for e in session_manager.claimed_elements(session_id) if e in refs]

    def _session_view_state(session_id):
        """Return ``(visible_node_ids, selected_node_ids)`` as the server sees them.

        Session state is server-owned (design §3.8): visible nodes come from the
        shared-session store's node references, the current selection from the
        advisory claim map narrowed to those same references. The browser no
        longer uploads canvas state — an MCP tool reads the same state every
        collaborator converges on.

        Both halves are read off the stored session, so a session the manager
        does not hold reports an empty selection rather than claims that outlived
        it: the claim map is not purged when a session is deleted.
        """
        visible: list = []
        selected: list = []
        if session_manager is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                node_refs = session.state.get("node_refs", [])
                hidden = set(session.state.get("hidden_node_ids", []))
                visible = [n for n in node_refs if n not in hidden]
                selected = _claimed_node_ids(session_id, node_refs)
        return visible, selected

    def _session_facts(session_id):
        """Return ``(has_stored_state, clients, push_target)`` for *session_id*.

        Two independent things can be true of a session id, and a tool that
        conflates them misleads the caller either way:

        - **stored state** — the session exists in the session store (design
          §3.1). This is what the tools acting on stored state
          (``add_nodes_to_session``, the layout tools) need; it is created by
          ``create_visualization_session`` or lazily by a browser's first
          change, so a browser sitting on a fresh session has none yet.
        - **a reachable canvas** — either a client reporting presence on the op
          stream (``clients``) or an entry in the legacy push registry
          (``push_target``). ``_push_to_session`` delivers to both, so either
          one means a push lands somewhere.

        Gating the read tools on the registry alone made a session created and
        populated over MCP — with no browser ever opened — report not-found even
        though its state was there and ``get_visualization_layout`` read it fine.
        """
        stored = (
            session_manager is not None
            and session_manager.get_session(session_id) is not None
        )
        clients = (
            session_manager.connected_count(session_id)
            if session_manager is not None
            else 0
        )
        push_target = bool(
            session_registry and session_registry.session_exists(session_id)
        )
        return stored, clients, push_target

    def register_tool(func: Callable) -> Callable:
        """Register a function as both MCP tool and in tools_map."""
        mcp.tool()(func)
        tools_map[func.__name__] = func
        return func

    # ==================== Search Tools ====================

    @register_tool
    def search_graph(
        query: str,
        node_types: Optional[List[str]] = None,
        limit: int = 50,
        action: Optional[str] = None,
        federation_depth: Optional[int] = None,
        tags_any: Optional[List[str]] = None,
        tags_all: Optional[List[str]] = None,
        tags_none: Optional[List[str]] = None,
        metadata_filters: Optional[List[Dict[str, Any]]] = None,
        include_archived: bool = False,
        semantic: bool = False,
        match_mode: str = MATCH_MODE_SUBSTRING,
        visualization_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search for nodes in the graph based on text query, with optional generic
        tag and metadata filtering.

        The tag/metadata filters are use-case agnostic: they match on whatever
        tags and metadata a deployment has put on its nodes and hardcode no field
        names or values. Pass an empty query ("") to filter purely by tags and/or
        metadata. When no filter argument is given the search behaves exactly as
        before.

        By default the query is matched lexically (substring). Multi-word or
        natural-language queries that no node contains verbatim therefore return
        nothing; set ``match_mode="any_term"`` to match any single term instead,
        or ``semantic=True`` to rank nodes by embedding meaning.
        As a safety net the search also falls back to semantic ranking
        automatically when a non-empty lexical query yields zero results, so a
        conceptual query still surfaces the closest nodes. The response includes
        a ``"semantic"`` boolean indicating whether semantic ranking produced the
        returned nodes.

        Args:
            query: Search text (matches against name, description, summary). Use ""
                to match on the filters alone.
            node_types: List of node types to filter on (Actor, Initiative, etc.)
            limit: Max number of results (default 50)
            action: Optional action for frontend ('add_to_visualization' to add to current view)
            tags_any: Keep only nodes carrying at least one of these tags (OR).
            tags_all: Keep only nodes carrying every one of these tags (AND).
            tags_none: Drop nodes carrying any of these tags (exclude).
            metadata_filters: A list of generic metadata filters, each a dict
                ``{"key": <field>, "values": [...], "match": "any"|"all"|"none"}``.
                ``any`` (default) keeps a node whose metadata value(s) at ``key``
                intersect the given values; ``all`` requires every given value to
                be present (for list-valued metadata); ``none`` excludes nodes that
                match any given value. Values compare as strings. Multiple filters
                combine with AND.
            include_archived: When False (default) archived nodes and edges are
                excluded. Set True to include archived items in the results.
            semantic: When True, rank results by embedding meaning (cosine
                similarity) instead of lexical substring matching. Default False
                keeps the lexical behavior, which still auto-falls back to
                semantic ranking when it returns zero results.
            match_mode: How the lexical query is matched. ``"substring"``
                (default) requires the whole query verbatim — unchanged
                behaviour. ``"any_term"`` splits the query on whitespace into
                distinct terms and matches nodes containing **any** of them,
                which is what a multi-word query such as "plan pricing offering"
                usually means; a node still ranks by its single best-matching
                term, so more terms never outweigh a stronger match, and a term
                you repeat counts once. Each term is matched as a
                substring, not as a word, so pass the distinctive terms: a short
                or common one ("a", "the") matches almost everything and pads
                the tail of the result with noise. Ignored when
                ``semantic=True``. Applies to the local graph; federated search
                stays substring-matched.
            visualization_session_id: Optional browser session ID — when provided, the result
                is pushed live to the connected browser window via SSE

        Returns:
            Dict with matching nodes and edges connecting them
        """
        result = service.search_graph(
            query=query,
            node_types=node_types,
            limit=limit,
            action=action,
            federation_depth=federation_depth,
            tags_any=tags_any,
            tags_all=tags_all,
            tags_none=tags_none,
            metadata_filters=metadata_filters,
            include_archived=include_archived,
            semantic=semantic,
            match_mode=match_mode,
        )
        _push(visualization_session_id, "search_graph", result)
        return result

    @register_tool
    def get_node_details(node_id: str) -> Dict[str, Any]:
        """
        Get complete information about a specific node

        Args:
            node_id: ID of the node

        Returns:
            Dict with the node and its incident edges (``edges``, visible ones
            only — an edge whose other endpoint is not visible or is archived
            is omitted), or an error.
        """
        return service.get_node_details(node_id)

    @register_tool
    def get_related_nodes(
        node_id: str,
        relationship_types: Optional[List[str]] = None,
        depth: int = 1,
        include_archived: bool = False,
        visualization_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get nodes connected to the given node

        Args:
            node_id: ID of the starting node
            relationship_types: List of relationship types to filter on
            depth: How many hops from the starting node (default 1)
            include_archived: When False (default) archived edges are not traversed
                and archived neighbour nodes are excluded. Set True to include them.
            visualization_session_id: Optional browser session ID — when provided, the result
                is pushed live to the connected browser window via SSE

        Returns:
            Dict with nodes and edges
        """
        result = service.get_related_nodes(
            node_id=node_id,
            relationship_types=relationship_types,
            depth=depth,
            include_archived=include_archived,
        )
        _push(visualization_session_id, "get_related_nodes", result)
        return result

    # ==================== Similarity Tools ====================

    @register_tool
    def find_similar_nodes(
        name: str,
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, Any]:
        """
        Find similar nodes based on name (for duplicate detection)

        Args:
            name: The name to search for similar nodes
            node_type: Optional node type to filter on
            threshold: Similarity threshold 0.0-1.0 (default 0.7)
            limit: Max number of results (default 5)

        Returns:
            Dict with similar nodes and similarity scores
        """
        return service.find_similar_nodes(
            name=name, node_type=node_type, threshold=threshold, limit=limit
        )

    @register_tool
    def find_similar_nodes_batch(
        names: List[str],
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, Any]:
        """
        Find similar nodes for multiple names at once (batch processing)

        This is MUCH more efficient than calling find_similar_nodes multiple times
        when processing documents with many entities. Use this when extracting
        multiple nodes from a document.

        Args:
            names: List of names to search for similar nodes
            node_type: Optional node type to filter on
            threshold: Similarity threshold 0.0-1.0 (default 0.7)
            limit: Max number of results per name (default 5)

        Returns:
            Dict with results for each name
        """
        return service.find_similar_nodes_batch(
            names=names, node_type=node_type, threshold=threshold, limit=limit
        )

    # ==================== CRUD Tools ====================

    @register_tool
    def add_nodes(
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Add new nodes and edges to the graph

        Field limits for nodes:
          - name: required, 1-200 characters
          - description: optional, max 2000 characters
          - summary: optional, max 300 characters (short text for visualization)
          - tags: optional list of strings
          - subtypes: optional list of strings for sub-classification within the node type
          - aliases: optional list of alternative names/synonyms; also matched in search

        Edge type is optional. If omitted, it defaults to "RELATES_TO".

        Args:
            nodes: List of node objects to add
            edges: List of edge objects (source, target, type). Type is optional.
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (added_node_ids, added_edge_ids, success, message)
        """
        return service.add_nodes(
            nodes=nodes,
            edges=edges,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def update_node(
        node_id: str,
        updates: Dict[str, Any],
        metadata_merge: bool = False,
        expected_updated_at: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update an existing node

        Args:
            node_id: ID of the node to update
            updates: Dict with fields to update (name, description, summary, tags, aliases, metadata)
            metadata_merge: When True, the `metadata` object is merged field-by-field
                onto the node's existing metadata instead of replacing it wholesale:
                only the keys you send are changed, other keys are preserved, and a
                key whose value is null (None) is removed. Default False keeps the
                legacy behaviour where `metadata` replaces the whole object — so a
                caller must resend every key to avoid dropping it. Use merge mode for
                safe concurrent writebacks that each touch a different key.
            expected_updated_at: Optional optimistic-concurrency guard. Pass the
                `updated_at` value you last read for this node; the update is
                rejected (result has success=False and conflict=True) if the node
                has changed since then, instead of silently overwriting a
                concurrent write. On conflict the result includes the live
                `current_updated_at` so you can re-read and retry.
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with updated node or error
        """
        return service.update_node(
            node_id,
            updates,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
            metadata_merge=metadata_merge,
            expected_updated_at=expected_updated_at,
        )

    @register_tool
    def delete_nodes(
        node_ids: List[str],
        confirmed: bool = False,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete nodes from the graph (max 10 at a time)

        SECURITY: Requires confirmed=True and max 10 nodes per call

        Args:
            node_ids: List of node IDs to delete
            confirmed: Must be True to execute deletion
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (deleted_node_ids, affected_edge_ids, success, message)
        """
        return service.delete_nodes(
            node_ids=node_ids,
            confirmed=confirmed,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def delete_edges(
        edge_ids: List[str],
        confirmed: bool = False,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete edges from the graph (max 50 at a time)

        Args:
            edge_ids: List of edge IDs to delete
            confirmed: Must be True to execute deletion
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (deleted_edge_ids, success, message)
        """
        return service.delete_edges(
            edge_ids=edge_ids,
            confirmed=confirmed,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    # ==================== Archive Tools ====================
    # Archiving hides nodes/edges from search and traversal by default while
    # keeping them in the graph, in contrast to delete_* which is permanent.

    @register_tool
    def archive_nodes(
        node_ids: List[str],
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Archive nodes: hide them from search/traversal by default without deleting.

        Archived nodes remain in the graph and can be restored with unarchive_nodes.
        Prefer this over delete_nodes when the intent is to hide/retire a node
        rather than permanently remove it.

        Args:
            node_ids: List of node IDs to archive
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (archived, node_ids, nodes, success)
        """
        return service.archive_nodes(
            node_ids=node_ids,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def unarchive_nodes(
        node_ids: List[str],
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Unarchive nodes: make previously archived nodes visible again.

        Args:
            node_ids: List of node IDs to unarchive
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (archived, node_ids, nodes, success)
        """
        return service.unarchive_nodes(
            node_ids=node_ids,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def archive_edges(
        edge_ids: List[str],
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Archive edges: hide them from search/traversal by default without deleting.

        Archived edges remain in the graph and can be restored with unarchive_edges.

        Args:
            edge_ids: List of edge IDs to archive
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (archived, edge_ids, edges, success)
        """
        return service.archive_edges(
            edge_ids=edge_ids,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def unarchive_edges(
        edge_ids: List[str],
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Unarchive edges: make previously archived edges visible again.

        Args:
            edge_ids: List of edge IDs to unarchive
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (archived, edge_ids, edges, success)
        """
        return service.unarchive_edges(
            edge_ids=edge_ids,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    # ==================== Statistics & Metadata Tools ====================

    @register_tool
    def get_graph_stats() -> Dict[str, Any]:
        """
        Get statistics for the graph

        Returns:
            Dict with statistics (total_nodes, total_edges, nodes_by_type, edges_by_type)
        """
        return service.get_graph_stats()

    @register_tool
    def list_node_types() -> Dict[str, Any]:
        """
        List all allowed node types according to the metamodel

        Returns:
            Dict with node types and their color coding
        """
        return service.list_node_types()

    @register_tool
    def get_subtypes(node_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Get existing subtypes used in the graph, grouped by node type.
        Use this to suggest consistent subtypes when adding or updating nodes.

        Args:
            node_type: Optional filter for a specific node type (e.g. 'Actor')

        Returns:
            Dict with subtypes grouped by node type
        """
        return service.get_subtypes(node_type)

    @register_tool
    def list_relationship_types() -> Dict[str, Any]:
        """
        List all allowed relationship types

        Returns:
            Dict with relationship types
        """
        return service.list_relationship_types()

    @register_tool
    def audit_relationship_applicability() -> Dict[str, Any]:
        """
        Report existing edges that violate configured relationship applicability rules.

        This tool is read-only. It does not delete or modify legacy graph data.
        """
        return service.audit_relationship_applicability()

    @register_tool
    def get_schema() -> Dict[str, Any]:
        """
        Get the complete schema configuration.

        Returns the full schema including all node types with their fields,
        colors, and descriptions, as well as all relationship types.

        Returns:
            Dict with node_types and relationship_types
        """
        return service.get_schema()

    @register_tool
    def get_presentation() -> Dict[str, Any]:
        """
        Get the presentation configuration for the UI.

        Returns settings for UI display including colors, introduction text,
        and prompt configuration.

        Returns:
            Dict with title, introduction, colors, prompt_prefix, prompt_suffix
        """
        return service.get_presentation()

    @register_tool
    def get_capabilities() -> Dict[str, Any]:
        """Get the public capability manifest for client discovery."""
        return service.get_capabilities()

    @register_tool
    def get_runtime_info() -> Dict[str, Any]:
        """Get the public runtime metadata for deployment introspection."""
        return service.get_runtime_info()

    @register_tool
    def get_tenant_context() -> Dict[str, Any]:
        """Get the tenant/deployment context metadata.

        Returns the tenant identifier, name, and deployment environment
        for this CommunityOverview instance.

        Returns:
            Dict with tenant_id, tenant_name, and environment
        """
        return service.get_tenant_context()

    @register_tool
    def get_config_context() -> Dict[str, Any]:
        """Get the effective config scope and non-sensitive config source metadata."""
        return service.get_config_context()

    @register_tool
    def get_request_actor(
        actor_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        auth_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public request actor context with optional safe overrides."""
        return service.get_request_actor_info(
            actor_id=actor_id,
            actor_type=actor_type,
            auth_source=auth_source,
        )

    @register_tool
    def get_request_scope(
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public workspace/graph scope context with optional safe overrides."""
        return service.get_request_scope_info(
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    @register_tool
    def get_request_selection(
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public graph/workspace selection summary with optional safe overrides."""
        return service.get_request_graph_selection_info(
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    # ==================== Saved Views Tools ====================

    @register_tool
    def save_view(name: str) -> Dict[str, Any]:
        """
        Signal intent to save the current view state.

        This tool does NOT save the view data itself (positions, etc.) because
        the backend does not know the client state. Instead, it acts as a signal
        for the frontend to capture the current visualization state and save it
        as a SavedView.

        Args:
            name: Name of the view to save

        Returns:
            A signal object that the frontend will intercept.
        """
        return service.save_view(name)

    @register_tool
    def get_saved_view(
        name: str,
        visualization_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get a saved view by name and load its content for display.

        This returns the actual nodes and edges that are part of the saved view,
        NOT the SavedView node itself. The SavedView node is just metadata
        storage - what the user wants to see is the content it references.

        Note: "Saved view" = a snapshot of nodes/edges/positions saved in the graph.
              "Current visualization" = what is currently displayed in the GUI.

        Args:
            name: Name of the saved view
            visualization_session_id: Optional browser session ID — when provided, the view
                is loaded live in the connected browser window via SSE

        Returns:
            The nodes and edges to display in the visualization, with position and hidden node data
        """
        result = service.get_saved_view(name)
        _push(visualization_session_id, "get_saved_view", result)
        return result

    @register_tool
    def list_saved_views() -> Dict[str, Any]:
        """
        List all saved views.

        Returns a list of all saved view snapshots stored in the graph.
        These are NOT the current visualization - they are saved snapshots
        that can be loaded to restore a specific graph view.

        Returns:
            List of all SavedView nodes with their names and summaries
        """
        return service.list_saved_views()

    # ==================== Visualization Session Tools ====================

    @register_tool
    def clear_visualization(visualization_session_id: str) -> Dict[str, Any]:
        """
        Clear all nodes, edges, and annotations from the visualization canvas.

        Removes everything currently displayed in the browser window without
        affecting the underlying graph data. Use this to start a fresh view.

        This is a live-canvas command, and it *gates* on the legacy push
        channel: it refuses unless a browser is holding that channel open for
        the session, which is narrower than "someone is watching" — a browser
        that has moved to the op stream reports presence in
        ``connect_to_visualization_session`` and still gets refused here, even
        though the command would have reached it. The tools that act on the
        session's stored state have no such requirement.

        Args:
            visualization_session_id: The browser session ID shown in the header
                (e.g. "8244-1742")

        Returns:
            Dict with success status and message
        """
        if not session_registry:
            return {"success": False, "error": "Session registry not available"}
        if not session_registry.is_valid_session_id(visualization_session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        if not session_registry.session_exists(visualization_session_id):
            # Keep the contract's not-found error for an id that names no
            # session at all (§8); "nobody is holding the legacy channel" is a
            # different condition and must not be reported as if the session
            # existed.
            stored, _, _ = _session_facts(visualization_session_id)
            if not stored:
                return {
                    "success": False,
                    "error": (
                        f"Session '{visualization_session_id}' not found. "
                        "Create one with create_visualization_session, or use "
                        "the session ID displayed in an open browser."
                    ),
                }
            return {
                "success": False,
                "error": (
                    f"Session '{visualization_session_id}' exists, but no "
                    "browser is holding its legacy push channel open, which is "
                    "what this command is gated on."
                ),
            }
        result = {
            "action": "clear_visualization",
            "nodes": [],
            "edges": [],
            "success": True,
        }
        _push(visualization_session_id, "clear_visualization", result)
        return {
            "success": True,
            "message": f"Canvas cleared in session '{visualization_session_id}'",
        }

    def _auto_add_unavailable() -> Dict[str, Any]:
        return {"success": False, "error": "Auto-add agents are not available"}

    @register_tool
    def create_session_auto_add_agent(
        visualization_session_id: str,
        node_types: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a session-scoped agent that auto-adds matching new nodes to a view.

        The agent watches for nodes newly created anywhere in the graph and, for
        each one that matches the pattern, ADDS it to this session's live
        visualization — additively, without clearing what is already shown. It is
        bound to this one session: it only ever pushes to this session and stops
        when the session ends. It never modifies the graph.

        Give at least one of ``node_types`` or ``keywords``; a rule with neither
        is rejected because it would add every created node. When both are given a
        node must match both (e.g. an Actor whose text contains a keyword).

        Args:
            visualization_session_id: The browser session ID shown in the header
                (e.g. "8244-1742")
            node_types: Node types to match (e.g. ["Actor"]); empty = any type
            keywords: Case-insensitive substrings matched against a node's
                name/description/summary/tags; empty = any text

        Returns:
            Dict with success status and the created agent (agent_id, pattern)
        """
        if auto_add_registry is None or not session_registry:
            return _auto_add_unavailable()
        if not session_registry.is_valid_session_id(visualization_session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        try:
            rule = auto_add_registry.add_rule(
                visualization_session_id,
                node_types=node_types,
                keywords=keywords,
            )
        except AutoAddRuleError as exc:
            return {"success": False, "error": str(exc)}
        # Materialise the push session so the periodic prune keeps this agent
        # while the session is live, even if configured before the browser's SSE
        # stream has (re)connected — mirrors mint_trigger_token.
        session_registry.get_or_create(visualization_session_id)
        return {"success": True, "agent": rule.to_dict()}

    @register_tool
    def list_session_auto_add_agents(
        visualization_session_id: str,
    ) -> Dict[str, Any]:
        """
        List the auto-add agents configured on a visualization session.

        Args:
            visualization_session_id: The browser session ID shown in the header

        Returns:
            Dict with success status and the session's agents
        """
        if auto_add_registry is None or not session_registry:
            return _auto_add_unavailable()
        if not session_registry.is_valid_session_id(visualization_session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        agents = [
            r.to_dict() for r in auto_add_registry.list_rules(visualization_session_id)
        ]
        return {"success": True, "agents": agents}

    @register_tool
    def remove_session_auto_add_agent(
        visualization_session_id: str,
        agent_id: str,
    ) -> Dict[str, Any]:
        """
        Remove an auto-add agent from a visualization session.

        Args:
            visualization_session_id: The browser session ID shown in the header
            agent_id: The agent id returned by create_session_auto_add_agent

        Returns:
            Dict with success status
        """
        if auto_add_registry is None or not session_registry:
            return _auto_add_unavailable()
        if not session_registry.is_valid_session_id(visualization_session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        removed = auto_add_registry.remove_rule(visualization_session_id, agent_id)
        if not removed:
            return {
                "success": False,
                "error": f"Auto-add agent '{agent_id}' not found in this session",
            }
        return {"success": True}

    @register_tool
    def connect_to_visualization_session(session_id: str) -> Dict[str, Any]:
        """
        Verify that a visualization session exists and can be addressed.

        Use this tool first to confirm the session ID before using the
        visualization_session_id parameter in other tools.

        A session resolves as soon as it exists — whether a browser opened it or
        ``create_visualization_session`` did. No browser needs to be connected:
        session state is server-owned, so a client that opens the session later
        picks up whatever was put there meanwhile.

        Two facts decide what you can do with it, and the result reports both:

        - ``has_stored_state`` — the session exists in the session store. The
          tools that act on stored state (``add_nodes_to_session``,
          ``get_visualization_layout``, ``apply_visualization_layout``) work
          exactly when this is true. A browser sitting on a session it has not
          changed yet has none: the store entry materialises on the first
          change, so those tools report not-found until then.
        - ``connected_clients`` — how many clients report presence on the
          session's op stream (the same count ``get_visualization_layout``
          returns). Results pushed with the ``visualization_session_id``
          parameter reach a live canvas; ``message`` says whether one is there.

        Args:
            session_id: The session ID shown in the browser header, or the one
                returned by create_visualization_session (e.g. "8244-1742")

        Returns:
            Dict with connected status, has_stored_state, connected_clients and
            canvas summary
        """
        if session_registry is None and session_manager is None:
            return {
                "connected": False,
                "error": "Visualization sessions are not available",
            }
        if not is_valid_session_id(session_id):
            return {
                "connected": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        # Same read gate as get_visualization_session_state: this tool reports a
        # session's existence and node count, so a hook that narrows reads must
        # be asked here too. (Not every tool in this family is gated yet:
        # clear_visualization and the three session auto-add agent tools still
        # are not. This closes the one that discloses stored session state
        # without asking.)
        denied = _authorize_session(
            GRAPH_ACTION_READ, "connect_to_visualization_session"
        )
        if denied:
            return denied
        stored, clients, push_target = _session_facts(session_id)
        # Existence is the store or the legacy registry — deliberately not
        # presence. A client can still be attached to a session that was just
        # deleted, and reporting that as found would resurrect it.
        if not stored and not push_target:
            return {
                "connected": False,
                "message": (
                    f"Session '{session_id}' not found. "
                    "Create one with create_visualization_session, or use the "
                    "session ID displayed in an open browser."
                ),
            }
        visible, _ = _session_view_state(session_id)
        stored_state_tools = (
            "add_nodes_to_session, get_visualization_layout and "
            "apply_visualization_layout act on its stored state"
        )
        if stored and clients > 0:
            message = (
                f"Session '{session_id}' exists with {clients} connected "
                f"client(s). {stored_state_tools}, and results pushed with the "
                "visualization_session_id parameter reach the canvas."
            )
        elif stored and push_target:
            # Reachable through the legacy push channel only: a browser is
            # holding it open without reporting presence on the op stream, so
            # the count in this very payload is 0 and must not be contradicted.
            message = (
                f"Session '{session_id}' exists and a browser is holding its "
                "legacy push channel open, though none is reporting presence "
                f"on the op stream (connected_clients is 0). "
                f"{stored_state_tools}, and results pushed with the "
                "visualization_session_id parameter reach that browser."
            )
        elif stored:
            message = (
                f"Session '{session_id}' exists, with no client connected to "
                f"it. {stored_state_tools} and work now; anything aimed at a "
                "live canvas (clear_visualization, and the "
                "visualization_session_id parameter on the search/read tools) "
                "reaches nobody until a browser opens the session."
            )
        else:
            message = (
                f"Session '{session_id}' is open in a client but has no stored "
                "state yet — it materialises on the first change to it. Results "
                "pushed with the visualization_session_id parameter reach the "
                "canvas now, while add_nodes_to_session, "
                "get_visualization_layout and apply_visualization_layout report "
                "it as not found until it materialises."
            )
        return {
            "connected": True,
            "session_id": session_id,
            "has_stored_state": stored,
            "connected_clients": clients,
            "message": message,
            "visible_node_count": len(visible),
        }

    @register_tool
    def get_visualization_session_state(session_id: str) -> Dict[str, Any]:
        """
        Get the current visualization state of a session.

        Returns the node IDs currently displayed and selected in the canvas,
        plus which nodes/edges are session-locally dimmed (visible but
        de-emphasised — see ``dimmed_node_ids``/``dimmed_edge_ids``) and the
        session's global edge-intensity baseline. Use this to understand what
        the user is looking at, and how prominently, before deciding which
        nodes to add, which view to load, or whether a dim/restore action is
        still needed.

        The state is server-owned, so it reads back for any session that exists
        — including one created over MCP that no browser has opened yet, where
        the selection is simply empty.

        ``selected_node_ids`` holds node ids only. A selection claim can also be
        taken on an edge, but this field is narrowed to the session's nodes, so
        it is safe to pass into any argument that expects node ids.

        Dimming is session-local visualization state, not a graph edit: it
        never changes the underlying nodes or edges, only how this session
        currently renders them.

        Args:
            session_id: The session ID shown in the browser header, or the one
                returned by create_visualization_session (e.g. "8244-1742")

        Returns:
            Dict with visible_node_ids, selected_node_ids, node_count,
            dimmed_node_ids, dimmed_edge_ids and edge_intensity (0.0-1.0, the
            baseline opacity every non-dimmed edge renders at; 1.0 is full
            prominence).
        """
        if session_registry is None and session_manager is None:
            return {"error": "Visualization sessions are not available"}
        if not is_valid_session_id(session_id):
            return {"error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD"}
        denied = _authorize_session(
            GRAPH_ACTION_READ, "get_visualization_session_state"
        )
        if denied:
            return denied
        stored, _, push_target = _session_facts(session_id)
        if not stored and not push_target:
            return {
                "error": (
                    f"Session '{session_id}' not found. "
                    "Create one with create_visualization_session, or use the "
                    "session ID displayed in an open browser."
                )
            }
        visible, selected = _session_view_state(session_id)
        dimmed_node_ids: list = []
        dimmed_edge_ids: list = []
        edge_intensity = 1.0
        if session_manager is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                visible_set = set(visible)
                dimmed_node_ids = [
                    n
                    for n in session.state.get("dimmed_node_ids", [])
                    if n in visible_set
                ]
                dimmed_edge_ids = list(session.state.get("dimmed_edge_ids", []))
                edge_intensity = session.state.get("edge_intensity", 1.0)
        return {
            "session_id": session_id,
            "visible_node_ids": visible,
            "selected_node_ids": selected,
            "node_count": len(visible),
            "dimmed_node_ids": dimmed_node_ids,
            "dimmed_edge_ids": dimmed_edge_ids,
            "edge_intensity": edge_intensity,
        }

    # ==================== Visualization Layout (geometry) ====================
    #
    # These two tools let an assistant read node geometry and move nodes in an
    # open session. They implement the versioned geometry/movement contract in
    # docs/MCP_VISUALIZATION_LAYOUT_CONTRACT.md: coordinates are model space
    # (pixels at zoom 1, x/y = node top-left), node width/height are not
    # server-owned (an assumed_node_size is advertised for spacing), a write is
    # one atomic layout_applied op with optimistic-concurrency and batch caps,
    # and the animation fields are a forward-compatible hint carried on the
    # broadcast op for the canvas to honor.

    @register_tool
    def get_visualization_layout(session_id: str) -> Dict[str, Any]:
        """
        Read the geometry of every node in an open visualization session.

        Returns each node's model-space position *and what it is*, so an AI agent
        can compute a new arrangement (a left-to-right DAG, a grid, type or status
        swimlanes) and then call ``apply_visualization_layout`` to move them.

        Geometry contract (read this before computing positions):
        - Coordinates are **model space**: independent of the user's zoom and pan,
          in pixels at zoom 1. Origin is (0, 0); +x is right, +y is down.
        - ``x``/``y`` is the node's **top-left** corner (React Flow convention).
        - Node width/height are not server-owned, so they are not returned per
          node. Use ``assumed_node_size`` to leave collision-free spacing.
        - ``revision`` is a monotonic counter. Pass it back as
          ``expected_revision`` to ``apply_visualization_layout`` for optimistic
          concurrency (the write is rejected if someone else changed the session
          in between). A node with no recorded position yet has ``x``/``y`` null.
        - The connected users' viewports are not reported; prefer placing nodes
          relative to their related nodes over guessing where a viewport is,
          especially when several clients are connected.

        Semantics for arranging by meaning (never parse the node id for this):
        - ``type`` is the node's graph type, e.g. "Initiative". It is null when
          the node reference does not resolve to a node this caller may read.
        - ``status`` is whatever the deployment stores under the node's
          ``metadata["status"]``, trimmed, and is null when that value is blank
          or the deployment does not use the field. It is a convention, not a
          schema-enforced field, so treat a null as "unknown", not as "no
          status".
        - ``selected_node_ids`` is what the users currently have selected, the
          same value ``get_visualization_session_state`` reports, so an arrange
          that should respect the selection needs only this one call. It holds
          node ids only — a selection claim can also be taken on an edge, but
          this field is narrowed to this response's nodes, so every id in it can
          be passed straight back to ``apply_visualization_layout``. The visible
          set is not repeated here: it is this response's nodes with
          ``hidden`` false.
        - ``dimmed`` is session-local focus state (independent of ``hidden``):
          the node is still on the canvas but rendered at reduced prominence.
          See ``get_visualization_session_state`` for the session's global
          ``edge_intensity`` baseline and the dimmed edge ids.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")

        Returns:
            Dict with revision, node_count, nodes (id/x/y/hidden/dimmed/type/status),
            selected_node_ids, assumed_node_size
        """
        if session_manager is None:
            return {"error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {"error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD"}
        denied = _authorize_session(GRAPH_ACTION_READ, "get_visualization_layout")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                )
            }
        positions = session.state.get("positions", {})
        hidden = set(session.state.get("hidden_node_ids", []))
        dimmed = set(session.state.get("dimmed_node_ids", []))
        node_refs = session.state.get("node_refs", [])
        # Read scope, and this tool's own name as the target: same rule the
        # write path follows, so a target-aware hook is never asked about the
        # helper. It matters here because the projection's denial is swallowed
        # into {} below — a narrowing meant for some other target would silently
        # report every node as type/status None rather than erroring.
        semantics = (
            service.resolve_session_node_semantics(
                node_refs,
                action=GRAPH_ACTION_READ,
                target="get_visualization_layout",
            ).get("nodes")
            or {}
        )
        nodes = []
        for node_id in node_refs:
            pos = positions.get(node_id)
            meaning = semantics.get(node_id) or {}
            nodes.append(
                {
                    "id": node_id,
                    "x": pos["x"] if pos else None,
                    "y": pos["y"] if pos else None,
                    "hidden": node_id in hidden,
                    "dimmed": node_id in dimmed,
                    "type": meaning.get("type"),
                    "status": meaning.get("status"),
                }
            )
        return {
            "session_id": session_id,
            "revision": session.seq,
            "node_count": len(nodes),
            "nodes": nodes,
            "selected_node_ids": _claimed_node_ids(session_id, node_refs),
            "assumed_node_size": _ASSUMED_NODE_SIZE,
            "coordinate_space": "model-space, pixels at zoom 1, x/y = node top-left",
            "connected_clients": session_manager.connected_count(session_id),
        }

    @register_tool
    def apply_visualization_layout(
        session_id: str,
        positions: Optional[Dict[str, Any]] = None,
        deltas: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
        animate: bool = True,
        duration_ms: int = 400,
        easing: str = "ease-in-out",
    ) -> Dict[str, Any]:
        """
        Move nodes in a visualization session by setting their positions.

        The whole batch is applied as one atomic operation and mirrored live to
        every connected browser, so a bulk re-layout arrives as a single change
        rather than node-by-node jumps. The canvas tweens the batch from the
        nodes' current positions to the targets using the animation hint below,
        so an arrange reads as one coherent motion.

        Coordinates are model space (zoom/pan independent, pixels at zoom 1,
        ``x``/``y`` = node top-left), exactly as ``get_visualization_layout``
        reports them. Only the nodes you name move; a write is a partial update of
        the position map, not a replacement. A batch is capped at 500 moves and
        256 KiB of payload (``too_large`` above that), and each write also draws
        from a per-client rate budget sized to the number of moves — so a single
        very large arrange may return ``rate_limited`` before the hard cap. Either
        way, split a large session across successive writes, threading the
        returned ``revision`` into the next ``expected_revision``.
        Layout patterns (horizontal DAG, grid, swimlanes) and the full geometry
        contract are documented in
        ``docs/MCP_VISUALIZATION_LAYOUT_CONTRACT.md`` and ``backend/DEVELOPMENT.md``.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            positions: Absolute targets ``{node_id: {"x": <n>, "y": <n>}}`` in the
                model space described by ``get_visualization_layout``.
            deltas: Relative moves ``{node_id: {"dx": <n>, "dy": <n>}}`` from each
                node's current position (unknown ⇒ origin). Provide exactly one of
                positions/deltas. Deltas are resolved to absolute positions before
                broadcast, so every client applies identical coordinates.
            expected_revision: If given, the write is rejected unless it equals the
                session's current ``revision`` (optimistic concurrency). Read it
                from ``get_visualization_layout`` first. Omit for last-write-wins.
            animate: Whether the canvas should tween this move (default true). Send
                the hint you intend; do not try to detect reduced motion yourself —
                a viewer who asked for reduced motion snaps to the final positions
                regardless (a client-side decision).
            duration_ms: Tween duration in milliseconds (default 400).
            easing: Tween easing, e.g. "ease-in-out" (default), "linear",
                "ease-in", "ease-out".

        Returns:
            Dict with success, moved (node count), and the new revision. On a
            concurrency clash returns success=false with the current revision so
            the caller can re-read and retry. Retryable errors: revision_conflict,
            busy, rate_limited; change the request for too_large or a validation
            error.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "apply_visualization_layout")
        if denied:
            return denied
        animation = {
            "animate": bool(animate),
            "duration_ms": duration_ms,
            "easing": easing,
        }
        try:
            result = session_manager.apply_layout(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                positions=positions,
                deltas=deltas,
                expected_revision=expected_revision,
                animation=animation,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read the layout "
                    "and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many layout writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Too many nodes in one layout write; split into batches.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "moved": result["moved"],
            "revision": result["revision"],
        }

    @register_tool
    def add_nodes_to_session(
        session_id: str,
        node_ids: List[str],
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Put a known set of nodes on a session's canvas by their ids.

        Use this when you already know which nodes belong in the view — the ids
        from an earlier ``search_graph`` / ``get_related_nodes`` / traversal —
        instead of crafting a search whose results happen to be exactly that
        set. It is additive: nodes already in the session stay, and ids already
        present are not added twice (a call that adds nothing new leaves the
        revision untouched).

        Ids that do not resolve to a node you may add are skipped and returned
        in ``skipped``, so a stale id cannot put a phantom reference in the
        session. Only ids in **this server's own graph storage** are addable: a
        ``search_graph`` result can also contain federated nodes, which live in a
        remote graph, so an *unadopted* federated id is skipped. Once
        ``adopt_federated_node`` has run, that same id names a local reference
        and becomes addable. The nodes arrive with no position; arrange them with
        ``apply_visualization_layout``, threading the ``revision`` returned here
        into its ``expected_revision``.

        An id already in the session is left exactly as it is — including when
        it is currently hidden, which this tool does not undo. So a call can
        legitimately report success with an empty ``added`` and still show
        nothing new on the canvas.

        A batch is capped at 500 ids, and each call also draws from a per-client
        rate budget sized to the number of ids — so a batch well below the hard
        cap can still return ``rate_limited``. Split large sets across
        successive calls, threading the returned ``revision`` into the next
        ``expected_revision``.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            node_ids: Ids of the nodes to add.
            expected_revision: If given, the write is rejected unless it equals
                the session's current ``revision`` (optimistic concurrency).
                Omit for last-write-wins.

        Returns:
            Dict with success, added (ids actually added, deduplicated), skipped
            (ids that did not resolve, deduplicated), node_count (nodes the
            session references, hidden ones included — the same total
            ``get_visualization_session`` reports, not the visible count from
            ``get_visualization_session_state``) and the new revision. On a
            concurrency clash returns success=false with the current revision so
            the caller can re-read and retry. Retryable errors:
            revision_conflict, busy, rate_limited; change the request for
            too_large or a validation error.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "add_nodes_to_session")
        if denied:
            return denied
        if not isinstance(node_ids, list) or not node_ids:
            return {"success": False, "error": "'node_ids' must be a non-empty list"}
        # Checked before the resolve below, which costs one node lookup per id:
        # the write path enforces the same cap, but only after that work is
        # already done.
        if len(node_ids) > session_manager.max_ops_per_batch:
            return {
                "success": False,
                "error": "too_large",
                "message": "Too many nodes in one write; split into batches.",
            }

        # Resolve through the projection under the *mutate* decision, not a read
        # one: a hook may narrow the two to different graph scopes, and this call
        # writes the ids into server-owned session state. Filtering by what the
        # caller may read would let a read-only-visible node be written in, the
        # gap every sibling mutation closes by narrowing with its own decision.
        # An id that no longer exists drops out here too. Same target as the gate
        # above, so a target-aware hook is asked about this tool twice rather
        # than about a helper it has never heard of.
        resolved = service.resolve_session_node_semantics(
            node_ids, action=GRAPH_ACTION_MUTATE, target="add_nodes_to_session"
        )
        if not resolved.get("success"):
            return resolved
        known = resolved.get("nodes") or {}
        resolvable = [
            node_id
            for node_id in node_ids
            if isinstance(node_id, str) and node_id in known
        ]
        # Anything not resolvable is skipped, including an id that is not a
        # string at all — `known` is keyed by string id, so testing membership
        # for an unhashable value would raise instead. Deduplicated in order for
        # the same reason `added` is: a repeated id is one id, whichever list it
        # ends up in. Non-strings are compared by equality, since an unhashable
        # one cannot go in a set.
        skipped: List[Any] = []
        for node_id in node_ids:
            if isinstance(node_id, str) and node_id in known:
                continue
            if node_id not in skipped:
                skipped.append(node_id)
        if not resolvable:
            return {
                "success": False,
                "error": "no_resolvable_nodes",
                "message": (
                    "None of the given ids resolve to a node you may add. Only "
                    "ids in this server's own graph storage are addable; an "
                    "unadopted federated search result's ids are not — adopt "
                    "the node first, or use its local id."
                ),
                "skipped": skipped,
            }

        try:
            result = session_manager.add_node_refs(
                session_id,
                _MCP_SESSION_CLIENT_ID,
                resolvable,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read the session "
                    "and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Too many nodes in one write; split into batches.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "added": result["added"],
            "skipped": skipped,
            "node_count": result["node_count"],
            "revision": result["revision"],
        }

    # ==================== Visualization Session CRUD ====================
    #
    # These let an assistant manage session *resources* (create/list/get/rename/
    # delete), as opposed to the tools above that inspect or lay out an already
    # open session. They implement the versioned contract in
    # docs/MCP_SESSION_LIFECYCLE_CONTRACT.md: every operation is gated by the
    # service authorization hook (permissive/anonymous by default in the open
    # core; the hosted layer swaps the hook in to enforce tenancy), names are
    # non-unique with a server default, and deletion is a confirmed hard delete.

    def _authorize_session(action: str, target: str):
        """Return None when allowed, or the access-denied result dict when not."""
        return access.authorize_graph_access(
            service.authorization_hook, action=action, target=target
        )

    def _project_session(session, *, mutate_allowed: bool) -> Dict[str, Any]:
        """Full session-resource projection (contract §2) from a Session object."""
        return {
            "session_id": session.id,
            "name": session.name,
            "lifecycle_state": "active",
            "owner": None,  # reserved; bound by the hosted layer, null in open core
            "workspace": None,  # reserved; bound by the hosted layer
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "revision": session.seq,
            "node_count": len(session.state.get("node_refs", [])),
            "capabilities": ["read"]
            + (["rename", "delete", "layout"] if mutate_allowed else []),
            # Server-owned canonical link (contract §5); None when no public base
            # URL is configured. The assistant must never construct one itself.
            "session_url": build_session_url(session.id),
        }

    def _project_meta(meta: Dict[str, Any], *, mutate_allowed: bool) -> Dict[str, Any]:
        """Lightweight projection for the list index (no full state loaded).

        ``node_count`` is intentionally omitted here — the list is served from a
        cached meta index (session_store R13) to avoid a full-state disk scan per
        session; call ``get_visualization_session`` for a session's node count.
        """
        return {
            "session_id": meta["id"],
            "name": meta.get("name"),
            "lifecycle_state": "active",
            "owner": None,
            "workspace": None,
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "revision": meta.get("seq"),
            "capabilities": ["read"]
            + (["rename", "delete", "layout"] if mutate_allowed else []),
            "session_url": build_session_url(meta["id"]),
        }

    @register_tool
    def create_visualization_session(name: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a new, empty visualization session and return its identity.

        Use this to prepare a named session from scratch: create it, add nodes
        with the search/related tools (passing the returned session id as
        ``visualization_session_id``), inspect its geometry with
        ``get_visualization_layout``, arrange it with
        ``apply_visualization_layout``, then hand the user its link.

        Args:
            name: Optional display name. Names are not required to be unique; when
                omitted the server assigns a default.

        Returns:
            Dict with success and the session resource (session_id, name,
            lifecycle_state, timestamps, revision, node_count, capabilities and
            ``session_url``). ``session_url`` is the server-owned canonical direct
            link — give that to the user as-is; never build a link from a hostname
            yourself. It is null when no public base URL is configured for the
            deployment.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "create_visualization_session")
        if denied:
            return denied
        chosen = name.strip() if isinstance(name, str) and name.strip() else None
        try:
            session = session_manager.create_session(chosen or _DEFAULT_SESSION_NAME)
        except SessionLimitReached:
            return {
                "success": False,
                "error": "too_many_sessions",
                "message": "The session limit has been reached; delete unused sessions and retry.",
            }
        return {
            "success": True,
            "session": _project_session(session, mutate_allowed=True),
        }

    @register_tool
    def list_visualization_sessions() -> Dict[str, Any]:
        """
        List existing visualization sessions, most recently updated first.

        Returns a lightweight index; call ``get_visualization_session`` for a
        single session's full detail (including its node count).

        Returns:
            Dict with success, sessions (list of resource projections) and count.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        denied = _authorize_session(GRAPH_ACTION_READ, "list_visualization_sessions")
        if denied:
            return denied
        mutate_allowed = (
            _authorize_session(GRAPH_ACTION_MUTATE, "visualization_session") is None
        )
        metas = session_manager.list_sessions()
        return {
            "success": True,
            "count": len(metas),
            "sessions": [
                _project_meta(m, mutate_allowed=mutate_allowed) for m in metas
            ],
        }

    @register_tool
    def get_visualization_session(session_id: str) -> Dict[str, Any]:
        """
        Inspect one visualization session's resource metadata.

        Args:
            session_id: The session ID (e.g. "8244-1742-3391-0057").

        Returns:
            Dict with success and the session resource, or an error when the id is
            malformed or the session does not exist.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_READ, "get_visualization_session")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {"success": False, "error": f"Session '{session_id}' not found."}
        mutate_allowed = (
            _authorize_session(GRAPH_ACTION_MUTATE, "visualization_session") is None
        )
        return {
            "success": True,
            "session": _project_session(session, mutate_allowed=mutate_allowed),
        }

    @register_tool
    def rename_visualization_session(
        session_id: str, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Set or clear a visualization session's display name.

        Names are not required to be unique. Pass ``name=null`` to clear it.

        Args:
            session_id: The session ID (e.g. "8244-1742-3391-0057").
            name: The new display name, or null to clear it.

        Returns:
            Dict with success and the updated session resource.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "rename_visualization_session")
        if denied:
            return denied
        try:
            session = session_manager.rename_session_sync(
                session_id, name, client_id=_MCP_SESSION_CLIENT_ID
            )
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except SessionLimitReached:
            return {
                "success": False,
                "error": "too_many_sessions",
                "message": "The session limit has been reached; delete unused sessions and retry.",
            }
        except SessionNotFound:
            return {"success": False, "error": f"Session '{session_id}' not found."}
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session": _project_session(session, mutate_allowed=True),
        }

    @register_tool
    def delete_visualization_session(
        session_id: str, confirm: bool = False
    ) -> Dict[str, Any]:
        """
        Permanently delete a visualization session (hard delete, no recovery).

        Deletion is irreversible and requires explicit confirmation: call once to
        see the confirmation prompt, then again with ``confirm=true``. Connected
        browsers are notified that the session was deleted.

        Args:
            session_id: The session ID (e.g. "8244-1742-3391-0057").
            confirm: Must be true to actually delete. Defaults to false so a delete
                is never performed on a loose instruction.

        Returns:
            Dict with success and deleted=true, or a confirmation_required / error
            result.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "delete_visualization_session")
        if denied:
            return denied
        if not confirm:
            return {
                "success": False,
                "error": "confirmation_required",
                "message": (
                    f"Deleting session '{session_id}' is permanent and cannot be "
                    "undone. Re-call with confirm=true to proceed."
                ),
            }
        try:
            existed = session_manager.delete_session_sync(
                session_id, deleted_by=_MCP_SESSION_CLIENT_ID
            )
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        if not existed:
            return {"success": False, "error": f"Session '{session_id}' not found."}
        return {"success": True, "deleted": True, "session_id": session_id}

    # ==================== Sticky Note Annotations ====================
    #
    # These tools let an assistant read and edit sticky-note annotations in an
    # open visualization session — the same server-owned annotation document
    # (docs/ANNOTATION_CONTRACT.md) a connected browser renders and edits.
    # Positions/sizes are model-space, matching the layout tools above, and
    # writes share their optimistic-concurrency contract (`expected_revision`
    # / `revision_conflict`). Only the `note` annotation type is exposed here;
    # the rest of the v1 types (line, shape, ...) have their own
    # generic tool set below ("Generic Annotations"); `group` has its own
    # dedicated tool set too (create_group_annotation/update_group_members).

    def _find_note(session, annotation_id: str):
        for annotation in session.state.get("annotations", []):
            if annotation.get("id") == annotation_id:
                return annotation if is_note(annotation) else None
        return None

    def _find_any_annotation(session, annotation_id: str):
        for annotation in session.state.get("annotations", []):
            if annotation.get("id") == annotation_id:
                return annotation
        return None

    @register_tool
    def list_sticky_notes(session_id: str) -> Dict[str, Any]:
        """
        List every sticky note in a visualization session.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")

        Returns:
            Dict with session_id, revision, notes (id/text/x/y/w/h/color/font_size/
            z/locked/created_at/updated_at). ``revision`` can be threaded into
            ``create_sticky_note``/``update_sticky_note``/``delete_sticky_note``'s
            ``expected_revision`` for optimistic concurrency.
        """
        if session_manager is None:
            return {"error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {"error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD"}
        denied = _authorize_session(GRAPH_ACTION_READ, "list_sticky_notes")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                )
            }
        notes = [
            project_note(annotation)
            for annotation in session.state.get("annotations", [])
            if is_note(annotation)
        ]
        return {
            "session_id": session_id,
            "revision": session.seq,
            "notes": notes,
            "coordinate_space": "model-space, pixels at zoom 1, x/y = top-left",
        }

    @register_tool
    def create_sticky_note(
        session_id: str,
        x: float,
        y: float,
        text: str = "",
        color: Optional[str] = None,
        font_size: Optional[float] = None,
        w: Optional[float] = None,
        h: Optional[float] = None,
        rotation: Optional[float] = None,
        z: Optional[float] = None,
        locked: bool = False,
        annotation_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create a sticky note, or replace one by id (create/upsert).

        Coordinates are model space (zoom/pan independent, pixels at zoom 1,
        ``x``/``y`` = top-left), the same space ``list_sticky_notes`` reports.
        Pass ``annotation_id`` to replace an existing note by its stable id
        (an upsert — the write is idempotent for a retried call with the same
        id); omit it to have the server mint one, returned in the result.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            x: Model-space x of the note's top-left corner.
            y: Model-space y of the note's top-left corner.
            text: Note body text.
            color: Optional note color (any CSS color the canvas accepts).
            font_size: Optional font size in px.
            w: Optional width in model-space px (default 160).
            h: Optional height in model-space px (default 96).
            rotation: Optional rotation in degrees. Defaults to 0.
            z: Optional layer order (higher draws on top). Defaults to 0.
            locked: Whether the note starts locked against edits.
            annotation_id: Stable id to create or replace. Omit to let the
                server assign one.
            expected_revision: If given, the write is rejected unless it equals
                the session's current ``revision`` (optimistic concurrency).
                Read it from ``list_sticky_notes`` first. Omit for last-write-wins.

        Returns:
            Dict with success, the created/replaced note, and the new revision.
            On a concurrency clash returns success=false with the current
            revision so the caller can re-read and retry. Retryable errors:
            revision_conflict, busy, rate_limited; change the request for
            wrong_type or a validation error.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "create_sticky_note")
        if denied:
            return denied
        if annotation_id is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                existing_annotation = _find_any_annotation(session, annotation_id)
                if existing_annotation is not None and not is_note(existing_annotation):
                    return {
                        "success": False,
                        "error": "wrong_type",
                        "message": (
                            f"Annotation id {annotation_id!r} already exists as a "
                            "different annotation type; create_sticky_note only "
                            "creates or replaces notes."
                        ),
                    }
        annotation = build_note_annotation(
            x=x,
            y=y,
            text=text,
            color=color,
            font_size=font_size,
            w=w,
            h=h,
            rotation=rotation,
            z=z,
            locked=locked,
            annotation_id=annotation_id,
        )
        try:
            result = session_manager.upsert_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                annotation,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_sticky_notes and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationRecentlyDeleted:
            return {
                "success": False,
                "error": "annotation_recently_deleted",
                "message": (
                    f"Annotation id {annotation_id!r} was just deleted by another "
                    "collaborator; retry with a different id."
                ),
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Note payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "note": project_note(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def update_sticky_note(
        session_id: str,
        annotation_id: str,
        text: Optional[str] = None,
        color: Optional[str] = None,
        font_size: Optional[float] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
        w: Optional[float] = None,
        h: Optional[float] = None,
        rotation: Optional[float] = None,
        z: Optional[float] = None,
        locked: Optional[bool] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Update a sticky note's content, style, position, size, rotation, layer
        order and/or lock state.

        A partial update: only the arguments given change, everything else on
        the note (including fields not modeled by this tool) is left as-is.
        Position and size are model space, matching ``list_sticky_notes``.
        This is the note equivalent of the generic ``update_annotation`` /
        ``reorder_annotation`` / ``set_annotation_lock`` tools, which refuse
        note ids — a note's ``rotation``/``z``/``locked`` are set here
        instead. Like those generic tools, ``locked`` is the canvas UI's own
        edit-lock convention, not a server-enforced permission: this tool does
        not check the note's current ``locked`` value before applying a write.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            annotation_id: The note's stable id, from ``list_sticky_notes`` or a
                prior ``create_sticky_note`` result.
            text: New body text, if changing it.
            color: New color, if changing it.
            font_size: New font size in px, if changing it.
            x: New model-space x of the top-left corner, if moving it.
            y: New model-space y of the top-left corner, if moving it.
            w: New width in model-space px, if resizing it.
            h: New height in model-space px, if resizing it.
            rotation: New rotation in degrees, if changing it.
            z: New layer order (higher draws on top), if changing it.
            locked: New lock state, if changing it.
            expected_revision: If given, the write is rejected unless it equals
                the session's current ``revision`` (optimistic concurrency).
                Read it from ``list_sticky_notes`` first. Omit for last-write-wins.

        Returns:
            Dict with success, the updated note, and the new revision. On a
            concurrency clash returns success=false with the current revision
            so the caller can re-read and retry. Retryable errors:
            revision_conflict, busy, rate_limited; change the request for
            not_found or a validation error.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "update_sticky_note")
        if denied:
            return denied
        if all(
            v is None for v in (text, color, font_size, x, y, w, h, rotation, z, locked)
        ):
            return {
                "success": False,
                "error": "no_fields_to_update",
                "message": (
                    "Give at least one of "
                    "text/color/font_size/x/y/w/h/rotation/z/locked."
                ),
            }
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        existing = _find_note(session, annotation_id)
        if existing is None:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No sticky note with id {annotation_id!r} in this session.",
            }
        patch = build_note_patch(
            existing,
            text=text,
            color=color,
            font_size=font_size,
            x=x,
            y=y,
            w=w,
            h=h,
            rotation=rotation,
            z=z,
            locked=locked,
        )
        try:
            result = session_manager.update_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                patch,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_sticky_notes and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationNotFound:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No sticky note with id {annotation_id!r} in this session.",
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Note payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "note": project_note(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def delete_sticky_note(
        session_id: str,
        annotation_id: str,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Delete a sticky note by its stable id.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            annotation_id: The note's stable id, from ``list_sticky_notes`` or a
                prior ``create_sticky_note`` result.
            expected_revision: If given, the write is rejected unless it equals
                the session's current ``revision`` (optimistic concurrency).
                Read it from ``list_sticky_notes`` first. Omit for last-write-wins.

        Returns:
            Dict with success, deleted annotation_id, and the new revision. On a
            concurrency clash returns success=false with the current revision so
            the caller can re-read and retry. Retryable errors: revision_conflict,
            busy, rate_limited; change the request for not_found or a validation
            error.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "delete_sticky_note")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        if _find_note(session, annotation_id) is None:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No sticky note with id {annotation_id!r} in this session.",
            }
        try:
            result = session_manager.delete_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                annotation_id,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_sticky_notes and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationNotFound:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No sticky note with id {annotation_id!r} in this session.",
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation_id": annotation_id,
            "revision": result["revision"],
        }

    # ==================== Generic Annotations ====================
    #
    # These tools extend note-only MCP annotation access to the rest of the
    # v1 model: text, label, line/arrow, shape, icon, vote_dot, image.
    # `note` keeps its dedicated tool set above (list_sticky_notes / ...);
    # `group` (node-membership boxes) keeps its own dedicated tool set below
    # (create_group_annotation / update_group_members) — folding it into a
    # generic content patch would risk silently dropping or corrupting
    # member_node_ids via a shallow dict.update, the same reason
    # build_group_annotation only ever writes that key when a caller
    # explicitly passes it. Reading/writing a note or group id through these
    # generic tools is refused with "wrong_type"/"not_found", mirroring
    # create_sticky_note's existing cross-type guard, so a type is never
    # silently converted into another by any of the three tool sets.

    def _find_generic_annotation(session, annotation_id: str):
        for annotation in session.state.get("annotations", []):
            if annotation.get("id") == annotation_id:
                return annotation if is_generic_annotation(annotation) else None
        return None

    def _lock_detach_content(existing: Dict[str, Any]) -> Dict[str, Any]:
        """The `content` fields that drop an attached/anchored annotation's
        binding at the moment it is locked (dec-annotation-lock-semantics
        point 2). `locked` now freezes geometry outright (the two canvas
        effects in GraphCanvas.jsx that resolve a binding's geometry skip a
        locked annotation entirely), so a binding left in place would claim an
        attachment the annotation no longer honours — a locked attached label
        that silently drifts from what it labels once its target moves later,
        or a locked anchored arrow that snaps back onto a now-distant target
        the instant it is unlocked. The annotation's stored geometry is kept
        resolved continuously by the browser's own follow effects while it is
        unlocked, so there is nothing left to (re)compute here — only the
        binding reference itself needs to go. Unlocking does not restore it;
        the contract is deliberate that a user re-attaches by hand. Returns
        an empty dict when *existing* carries no binding to drop.

        Shared by `set_annotation_lock` (patching a stored annotation) and
        `create_annotation` (applied before the annotation is stored, so a
        caller cannot smuggle `locked=True` plus an attached `content` past
        the same rule through a fresh create or an upsert-replace — see
        docs/ANNOTATION_CONTRACT.md's "Locking and bindings"). Both call
        sites pass a dict with the payload fields already merged onto the
        top level (`_apply_content`'s shape), but what dict that is differs:
        `set_annotation_lock` always has the real stored annotation to read.
        `create_annotation`'s fresh-create case has no prior stored state,
        so the freshly built dict *is* the whole picture there — but its
        upsert-replace case must pass a merged view (the existing stored
        annotation overlaid by this call's own fields), not the freshly
        built dict alone, or a binding field this call's `content` omits
        would read as absent instead of falling back to the value the
        underlying shallow-merge write (session_store.py's
        `existing.update(annotation)`) would otherwise carry forward
        untouched.
        """
        ann_type = annotation_type_of(existing)
        content: Dict[str, Any] = {}
        if ann_type in ATTACHABLE_ANNOTATION_TYPES and existing.get("attachment"):
            content["attachment"] = None
        if ann_type == "line":
            if existing.get("startAnchor"):
                content["startAnchor"] = None
            if existing.get("endAnchor"):
                content["endAnchor"] = None
            for key in ("start", "end"):
                endpoint = existing.get(key)
                if isinstance(endpoint, dict) and endpoint.get("attachment"):
                    content[key] = None
        return content

    @register_tool
    def list_annotations(
        session_id: str, types: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        List annotations in a visualization session, across every v1 type.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            types: Optional list of annotation types to include (e.g.
                ["line", "label"]; "arrow" is accepted as an alias for
                "line"). Omit to list every type, including notes and groups.

        Returns:
            Dict with session_id, revision, annotations (id/type/x/y/w/h/
            rotation/style/z/locked/content/created_at/updated_at/
            created_by/updated_by — ``content`` holds the type-specific
            payload fields, e.g. a line's ``from``/``to``, a label's
            ``text``). ``revision`` can be threaded into the write tools'
            ``expected_revision`` for optimistic concurrency.
        """
        if session_manager is None:
            return {"error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {"error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD"}
        denied = _authorize_session(GRAPH_ACTION_READ, "list_annotations")
        if denied:
            return denied
        wanted: Optional[set] = None
        if types is not None:
            wanted = set()
            for raw_type in types:
                resolved = resolve_annotation_type_alias(raw_type)
                if resolved not in ALL_ANNOTATION_TYPES:
                    return {
                        "error": (
                            f"Unknown annotation type {raw_type!r}; expected one "
                            f"of {sorted(ALL_ANNOTATION_TYPES)} (or 'arrow' as an "
                            "alias for 'line')."
                        )
                    }
                wanted.add(resolved)
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                )
            }
        annotations = [
            project_annotation(annotation)
            for annotation in session.state.get("annotations", [])
            if wanted is None or annotation_type_of(annotation) in wanted
        ]
        return {
            "session_id": session_id,
            "revision": session.seq,
            "annotations": annotations,
            "coordinate_space": "model-space, pixels at zoom 1, x/y = top-left",
        }

    @register_tool
    def create_annotation(
        session_id: str,
        type: str,
        x: float,
        y: float,
        w: Optional[float] = None,
        h: Optional[float] = None,
        rotation: Optional[float] = None,
        content: Optional[Dict[str, Any]] = None,
        style: Optional[Dict[str, Any]] = None,
        z: Optional[float] = None,
        locked: bool = False,
        annotation_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create an annotation, or replace one by id (create/upsert).

        Covers every v1 annotation type except `note`, `group` and `image`:
        `text`, `label`, `line` (`arrow` accepted as an alias),
        `shape`, `icon`, `vote_dot`, `freehand`. Use `create_sticky_note` for notes,
        `create_group_annotation` for groups, and `create_image_annotation`
        for images (an image's pixel content must be ingested server-side, so
        it cannot be created from a bare envelope here). An image annotation
        that already exists is updated, moved, reordered, locked, duplicated
        and deleted through these generic tools like any other type.

        Coordinates are model space (zoom/pan independent, pixels at zoom 1,
        `x`/`y` = top-left or anchor point), the same space `list_annotations`
        reports. Pass `annotation_id` to replace an existing annotation of
        the *same* type by its stable id (an upsert — idempotent for a
        retried call with the same id); replacing an id that already holds a
        different type is refused rather than silently converted. Omit
        `annotation_id` to have the server mint one, returned in the result.

        `content` carries the type-specific payload verbatim (the shape
        differs per type — see docs/ANNOTATION_CONTRACT.md), for example:
          - text/label: {"text": "..."}
          - line: {"to": {"x": .., "y": ..}, "endArrow": true}
          - shape: {"shape": "rectangle", "text": "optional caption"}
          - icon: {"icon": "flag"}
          - vote_dot: {"value": 3}

        `locked=True` combined with an attached/anchored binding (an
        attachable type's `attachment`, or a `line`'s `start`/`end`
        attachment) never stores both together — the binding is dropped in
        the same write, the same rule `set_annotation_lock` enforces when
        locking an already-stored annotation (dec-annotation-lock-semantics
        point 2; see docs/ANNOTATION_CONTRACT.md's "Locking and bindings").
        This applies to a fresh create and to an upsert-replace alike,
        including replacing an existing unlocked, attached annotation with
        `locked=True` and the binding either resent verbatim in `content` or
        left out of `content` entirely — the previously stored binding is
        looked up and dropped either way, not only a resent one.

        `text` and `shape` also read typography out of `style` (not
        `content`): `style.fontSize` (px), `style.font` (one of the curated
        family names GENERIC_FONT_FAMILIES in
        packages/ui-graph-canvas/src/utils/annotations.js lists — currently
        "serif", "monospace", "cursive"; omit for the app's own default font),
        and `style.textAlign` (one of the nine box positions "top-left"
        through "bottom-right", e.g. {"style": {"fontSize": 20, "font":
        "serif", "textAlign": "middle-center"}}). All three are optional and
        each falls back independently to what the canvas already rendered
        before this existed, so omitting them changes nothing.

        `shape` reads its fill and border out of `style` too —
        `style.fill`/`style.border`, each either a CSS colour string or the
        literal string "transparent" (independent of each other, e.g.
        {"style": {"fill": "transparent", "border": "#94a3b8"}} for a
        transparent-bodied box with a coloured outline — what the retired
        `frame` type used to be, before it was folded into `shape`). Omitting
        either leaves it at its default (a solid grey fill, no border), the
        same look a plain `shape` always had.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            type: One of text/label/line/shape/icon/vote_dot/freehand
                ("arrow" accepted as an alias for "line"; "image" is
                rejected — use create_image_annotation).
            x: Model-space x of the annotation's anchor/top-left corner.
            y: Model-space y of the annotation's anchor/top-left corner.
            w: Optional width in model-space px (no type-specific default;
                shape usually needs one, line/icon usually don't).
            h: Optional height in model-space px.
            rotation: Optional rotation in degrees.
            content: Optional type-specific payload fields (see above).
            style: Optional style dict (color/opacity; for
                text/shape also fontSize/font/textAlign, and for shape also
                fill/border — see above).
            z: Optional layer order (higher draws on top). Defaults to 0.
            locked: Whether the annotation starts locked against edits.
            annotation_id: Stable id to create or replace. Omit to let the
                server assign one.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision` (optimistic
                concurrency). Read it from `list_annotations` first. Omit
                for last-write-wins.

        Returns:
            Dict with success, the created/replaced annotation (same shape
            as `list_annotations`), and the new revision. Retryable errors:
            revision_conflict, busy, rate_limited; change the request for
            invalid_type, invalid_content, wrong_type, or too_large.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "create_annotation")
        if denied:
            return denied
        normalized_type = normalize_generic_type(type)
        if normalized_type is None:
            return {
                "success": False,
                "error": "invalid_type",
                "message": (
                    "type must be one of "
                    f"{sorted(GENERIC_ANNOTATION_TYPES - {IMAGE_TYPE})} "
                    "('arrow' accepted as an alias for 'line'); use "
                    "create_sticky_note for notes, create_group_annotation for "
                    "groups, and create_image_annotation for images."
                ),
            }
        if normalized_type == IMAGE_TYPE:
            return {
                "success": False,
                "error": "invalid_type",
                "message": (
                    "image annotations are created with create_image_annotation, "
                    "which ingests the picture server-side (format validation, "
                    "downscale, embed) — this tool would otherwise store an "
                    "unvalidated image reference. Every other operation "
                    "(update/move/reorder/lock/duplicate/delete) works on an "
                    "image annotation through the generic tools."
                ),
            }
        existing_annotation = None
        if annotation_id is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                existing_annotation = _find_any_annotation(session, annotation_id)
                if existing_annotation is not None:
                    if not is_generic_annotation(existing_annotation):
                        return {
                            "success": False,
                            "error": "wrong_type",
                            "message": (
                                f"Annotation id {annotation_id!r} already exists as "
                                "a note or group; create_annotation only creates or "
                                "replaces the generic types it manages."
                            ),
                        }
                    existing_type = annotation_type_of(existing_annotation)
                    if existing_type != normalized_type:
                        return {
                            "success": False,
                            "error": "wrong_type",
                            "message": (
                                f"Annotation id {annotation_id!r} already exists as "
                                f"type {existing_type!r}; create_annotation will not "
                                "silently convert it to a different type. Delete it "
                                "first or use a new annotation_id."
                            ),
                        }
        try:
            annotation = build_annotation(
                type=normalized_type,
                x=x,
                y=y,
                w=w,
                h=h,
                rotation=rotation,
                content=content,
                style=style,
                z=z,
                locked=locked,
                annotation_id=annotation_id,
            )
        except ValueError as exc:
            return {"success": False, "error": "invalid_content", "message": str(exc)}
        # dec-annotation-lock-semantics point 2: a fresh create or an
        # upsert-replace (annotation_id matching an existing annotation) that
        # sets locked=True must not store a binding alongside it either —
        # session_manager.upsert_annotation's actual write
        # (session_store.py's `existing.update(annotation)`) is a shallow
        # per-key merge onto the previously stored record, not a full
        # replace, so any binding field this call's `content` omits survives
        # untouched from the old stored annotation. Detecting the binding to
        # drop on `annotation` alone (the freshly built dict) is therefore
        # only correct for a fresh create, where there is no prior stored
        # state — a caller flipping `locked` to True on an existing
        # attached annotation while omitting `content` (the natural
        # "just lock it" call) would otherwise leave the old binding in
        # place under the shallow merge, reproducing the bypass this whole
        # rule exists to prevent. For an upsert-replace we instead detect
        # against a merged view — the existing stored annotation overlaid by
        # whatever this call is about to write — so an omitted binding field
        # falls back to the stored value instead of reading as absent, and
        # any field this call does supply still takes precedence.
        if annotation.get("locked"):
            if existing_annotation is not None:
                merged_view = dict(existing_annotation)
                merged_view.update(annotation)
                annotation.update(_lock_detach_content(merged_view))
            else:
                annotation.update(_lock_detach_content(annotation))
        try:
            result = session_manager.upsert_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                annotation,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationRecentlyDeleted:
            return {
                "success": False,
                "error": "annotation_recently_deleted",
                "message": (
                    f"Annotation id {annotation_id!r} was just deleted by another "
                    "collaborator; retry with a different id."
                ),
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except ImageBudgetExceeded as exc:
            return {"success": False, "error": "too_large", "message": str(exc)}
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Annotation payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation": project_annotation(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def create_image_annotation(
        session_id: str,
        x: float,
        y: float,
        image_data: Optional[str] = None,
        image_url: Optional[str] = None,
        w: Optional[float] = None,
        h: Optional[float] = None,
        rotation: Optional[float] = None,
        alt: str = "",
        style: Optional[Dict[str, Any]] = None,
        z: Optional[float] = None,
        locked: bool = False,
        annotation_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create an `image` annotation by embedding an image, or replace one by id.

        Ingests the image server-side and stores the result as an embedded
        data URI — never a remote link — so the annotation still renders if
        the source later disappears. Give exactly one of `image_data` (a
        `data:image/...;base64,...` string, or bare base64) or `image_url`
        (an http(s) URL fetched once, server-side). Only PNG, JPEG and WebP
        are accepted, validated from the decoded bytes rather than a
        declared content-type; the image is downscaled to a longest side of
        2560px if needed and re-encoded as WebP, preserving PNG/WebP
        transparency.

        This is a separate tool from `create_annotation` because an embedded
        image is orders of magnitude larger than the generic op-batch cap
        that tool's writes share (see its docstring) — this tool enforces
        its own, larger, image-specific budgets instead: a per-image cap
        after optimization, a per-session cap on total embedded image bytes,
        and a cap on the full session document size. Once created, an image
        annotation is an ordinary generic annotation: `update_annotation`,
        `delete_annotation`, `reorder_annotation` and `set_annotation_lock`
        all act on it like any other type (moving, resizing, rotating,
        relayering and locking touch only the envelope, not the embedded
        bytes). `duplicate_annotation` works too, except on an annotation
        whose stored URL is not an embedded one — a copy lands on a new id,
        so it counts as a new reference to unvalidated content and is
        refused. Replacing the *picture* means calling this tool
        again with the same `annotation_id`; `create_annotation` and
        `update_annotation` cannot set image content, so no path stores a
        picture that has not been ingested here.

        Coordinates are model space (zoom/pan independent, pixels at zoom 1,
        `x`/`y` = top-left), the same space `list_annotations` reports.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            x: Model-space x of the image's top-left corner.
            y: Model-space y of the image's top-left corner.
            image_data: The image's bytes, as a `data:` URL or bare base64.
                Give this or `image_url`, not both.
            image_url: An http(s) URL to fetch the image from, server-side,
                exactly once. Give this or `image_data`, not both.
            w: Optional width in model-space px. Defaults to the image's
                (possibly downscaled) pixel width.
            h: Optional height in model-space px. Defaults to the image's
                (possibly downscaled) pixel height.
            rotation: Optional rotation in degrees.
            alt: Optional alt text for the image.
            style: Optional style dict (opacity, border, ...).
            z: Optional layer order (higher draws on top). Defaults to 0.
            locked: Whether the annotation starts locked against edits.
            annotation_id: Stable id to create or replace. Omit to let the
                server assign one.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision` (optimistic
                concurrency). Read it from `list_annotations` first. Omit
                for last-write-wins.

        Returns:
            Dict with success, the created/replaced annotation (same shape
            as `list_annotations`), and the new revision. Retryable errors:
            revision_conflict, busy, rate_limited; change the request for
            invalid_source, invalid_image, unsupported_type, fetch_failed,
            invalid_content, wrong_type or too_large.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "create_image_annotation")
        if denied:
            return denied
        if bool(image_data) == bool(image_url):
            return {
                "success": False,
                "error": "invalid_source",
                "message": "Give exactly one of image_data or image_url.",
            }

        if annotation_id is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                existing_annotation = _find_any_annotation(session, annotation_id)
                if existing_annotation is not None:
                    if not is_generic_annotation(existing_annotation):
                        return {
                            "success": False,
                            "error": "wrong_type",
                            "message": (
                                f"Annotation id {annotation_id!r} already exists as "
                                "a note or group; create_image_annotation only "
                                "creates or replaces image annotations."
                            ),
                        }
                    existing_type = annotation_type_of(existing_annotation)
                    if existing_type != "image":
                        return {
                            "success": False,
                            "error": "wrong_type",
                            "message": (
                                f"Annotation id {annotation_id!r} already exists as "
                                f"type {existing_type!r}; create_image_annotation "
                                "will not silently convert it to image. Delete it "
                                "first or use a new annotation_id."
                            ),
                        }

        try:
            raw = (
                fetch_image_bytes(image_url)
                if image_url is not None
                else decode_image_data(image_data)
            )
        except SourceImageTooLarge as exc:
            return {"success": False, "error": "too_large", "message": str(exc)}
        except ImageFetchError as exc:
            return {"success": False, "error": "fetch_failed", "message": str(exc)}
        except InvalidImageData as exc:
            return {"success": False, "error": "invalid_image", "message": str(exc)}

        try:
            optimized = optimize_image(raw)
        except UnsupportedImageType as exc:
            return {"success": False, "error": "unsupported_type", "message": str(exc)}
        except OptimizedImageTooLarge as exc:
            return {"success": False, "error": "too_large", "message": str(exc)}
        except InvalidImageData as exc:
            return {"success": False, "error": "invalid_image", "message": str(exc)}

        content = {
            "image": {
                "url": optimized.data_url,
                "width": optimized.width,
                "height": optimized.height,
            },
            "alt": alt or "",
        }
        try:
            annotation = build_annotation(
                type="image",
                x=x,
                y=y,
                w=w if w is not None else optimized.width,
                h=h if h is not None else optimized.height,
                rotation=rotation,
                content=content,
                style=style,
                z=z,
                locked=locked,
                annotation_id=annotation_id,
            )
        except ValueError as exc:
            return {"success": False, "error": "invalid_content", "message": str(exc)}

        try:
            result = session_manager.upsert_image_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                annotation,
                optimized_image_bytes=len(optimized.data),
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationRecentlyDeleted:
            return {
                "success": False,
                "error": "annotation_recently_deleted",
                "message": (
                    f"Annotation id {annotation_id!r} was just deleted by another "
                    "collaborator; retry with a different id."
                ),
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except ImageBudgetExceeded as exc:
            return {"success": False, "error": "too_large", "message": str(exc)}
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            # A same-id collision with a different type slipped past the
            # pre-check above (a concurrent write landed in the window between
            # that read and this write — the pre-check is a fast-path UX
            # nicety, not the enforcement point); SessionStore.apply_state_op
            # is the actual authority and raises OpError here instead of
            # silently retyping the annotation. Same race, same handling, as
            # the REST endpoint's ingest_session_image.
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation": project_annotation(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def update_annotation(
        session_id: str,
        annotation_id: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        w: Optional[float] = None,
        h: Optional[float] = None,
        rotation: Optional[float] = None,
        content: Optional[Dict[str, Any]] = None,
        style: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Update an annotation's content, style, and/or geometry.

        A partial update: only the arguments given change, everything else
        on the annotation is left as-is. Position/size are model space,
        matching `list_annotations`. Only acts on the generic types
        `create_annotation` manages (not `note`/`group` — see its tool
        docstring); layer order and lock state have their own tools
        (`reorder_annotation`, `set_annotation_lock`).

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            annotation_id: The annotation's stable id, from `list_annotations`
                or a prior `create_annotation` result.
            x: New model-space x of the anchor/top-left corner, if moving it.
            y: New model-space y of the anchor/top-left corner, if moving it.
            w: New width in model-space px, if resizing it.
            h: New height in model-space px, if resizing it.
            rotation: New rotation in degrees, if changing it.
            content: Type-specific payload fields to overwrite (see
                `create_annotation`'s docstring for the shape per type).
                `image` is rejected here: an image annotation's picture is
                replaced by calling `create_image_annotation` again with the
                same annotation_id, so the new bytes go through ingest.
            style: New style dict, if changing it (replaces the whole dict —
                for text/shape this includes fontSize/font/textAlign, so
                changing only one of them still means resending every style
                field you want kept, per create_annotation's docstring).
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision` (optimistic
                concurrency). Read it from `list_annotations` first. Omit
                for last-write-wins.

        Returns:
            Dict with success, the updated annotation, and the new revision.
            Retryable errors: revision_conflict, busy, rate_limited; change
            the request for not_found, invalid_content, or no_fields_to_update.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "update_annotation")
        if denied:
            return denied
        if all(v is None for v in (x, y, w, h, rotation, content, style)):
            return {
                "success": False,
                "error": "no_fields_to_update",
                "message": "Give at least one of x/y/w/h/rotation/content/style.",
            }
        if isinstance(content, dict) and "image" in content:
            return {
                "success": False,
                "error": "invalid_content",
                "message": (
                    "an image annotation's picture is replaced with "
                    "create_image_annotation (same annotation_id), which ingests "
                    "the new picture server-side; this tool cannot set "
                    "content.image directly. Alt text and every other field are "
                    "editable here."
                ),
            }
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        existing = _find_generic_annotation(session, annotation_id)
        if existing is None:
            return {
                "success": False,
                "error": "not_found",
                "message": (
                    f"No annotation with id {annotation_id!r} in this session's "
                    "generic annotation set (note/group ids are managed by their "
                    "own tools)."
                ),
            }
        try:
            patch = build_annotation_patch(
                existing,
                x=x,
                y=y,
                w=w,
                h=h,
                rotation=rotation,
                content=content,
                style=style,
            )
        except ValueError as exc:
            return {"success": False, "error": "invalid_content", "message": str(exc)}
        try:
            result = session_manager.update_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                patch,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationNotFound:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session.",
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Annotation payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation": project_annotation(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def reorder_annotation(
        session_id: str,
        annotation_id: str,
        z: float,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Change an annotation's layer order (`z`; higher draws on top).

        Only acts on the generic types `create_annotation` manages (not
        `note`/`group`).

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            annotation_id: The annotation's stable id.
            z: The new layer order value.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision`. Omit for
                last-write-wins.

        Returns:
            Dict with success, the updated annotation, and the new revision.
            Retryable errors: revision_conflict, busy, rate_limited; change
            the request for not_found.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "reorder_annotation")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        existing = _find_generic_annotation(session, annotation_id)
        if existing is None:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session's generic annotation set.",
            }
        patch = build_annotation_patch(existing, z=z)
        try:
            result = session_manager.update_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                patch,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationNotFound:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session.",
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Annotation payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation": project_annotation(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def set_annotation_lock(
        session_id: str,
        annotation_id: str,
        locked: bool,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Lock or unlock an annotation against edits (`locked=True`/`False`).

        This is the canvas UI's own edit-lock convention, not a
        server-enforced permission: `update_annotation`/`reorder_annotation`/
        `delete_annotation` do not check the flag themselves, so an agent can
        still edit or unlock a locked annotation deliberately. Only acts on
        the generic types `create_annotation` manages (not `note`/`group`).

        Locking freezes ALL geometry change, including a binding's own
        follow behaviour — so locking an attached (`text`/`label`/`icon`/
        `vote_dot`) or anchored (`line`) annotation drops that binding in the
        same write: its current, already-resolved position is kept, but the
        attachment/anchor reference itself is cleared. Unlocking does not
        restore it; re-attach manually if that is what you want.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            annotation_id: The annotation's stable id.
            locked: True to lock, False to unlock.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision`. Omit for
                last-write-wins.

        Returns:
            Dict with success, the updated annotation, and the new revision.
            Retryable errors: revision_conflict, busy, rate_limited; change
            the request for not_found.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "set_annotation_lock")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        existing = _find_generic_annotation(session, annotation_id)
        if existing is None:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session's generic annotation set.",
            }
        # Locking drops an attached/anchored annotation's binding in the same
        # write (dec-annotation-lock-semantics point 2) — unlocking does not
        # bring it back, per that decision.
        detach_content = _lock_detach_content(existing) if locked else None
        patch = build_annotation_patch(existing, locked=locked, content=detach_content)
        try:
            result = session_manager.update_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                patch,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationNotFound:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session.",
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Annotation payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation": project_annotation(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def duplicate_annotation(
        session_id: str,
        annotation_id: str,
        new_annotation_id: Optional[str] = None,
        dx: float = 0,
        dy: float = 0,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Duplicate an annotation at an optional offset.

        Copies every field of the existing annotation — including its
        type-specific content and style — onto a new id, so the caller does
        not need to know the type's payload shape to duplicate it (mirrors
        the canvas's `duplicate` operation, docs/ANNOTATION_CONTRACT.md).
        Only acts on the generic types `create_annotation` manages (not
        `note`/`group`).

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            annotation_id: The stable id of the annotation to duplicate.
            new_annotation_id: Stable id for the copy. Omit to let the
                server assign one. Rejected if it already names another
                annotation.
            dx: Model-space x offset applied to the copy. Default 0.
            dy: Model-space y offset applied to the copy. Default 0.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision`. Omit for
                last-write-wins.

        Returns:
            Dict with success, the new annotation, and the new revision.
            Retryable errors: revision_conflict, busy, rate_limited; change
            the request for not_found or id_exists.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "duplicate_annotation")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        existing = _find_generic_annotation(session, annotation_id)
        if existing is None:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session's generic annotation set.",
            }
        if new_annotation_id is not None:
            collision = _find_any_annotation(session, new_annotation_id)
            if collision is not None:
                return {
                    "success": False,
                    "error": "id_exists",
                    "message": (
                        f"Annotation id {new_annotation_id!r} already exists; "
                        "choose a different new_annotation_id or omit it to let "
                        "the server assign one."
                    ),
                }
        copy = dict(existing)
        for key in ("id", "created_at", "updated_at", "created_by", "updated_by"):
            copy.pop(key, None)
        geometry = dict(copy.get("geometry") or {})
        geometry["x"] = geometry.get("x", 0) + dx
        geometry["y"] = geometry.get("y", 0) + dy
        copy["geometry"] = geometry
        position = dict(
            copy.get("position")
            or {"x": geometry.get("x", 0), "y": geometry.get("y", 0)}
        )
        position["x"] = position.get("x", 0) + dx
        position["y"] = position.get("y", 0) + dy
        copy["position"] = position
        copy.update(translate_line_endpoints(existing, dx, dy))
        copy.update(translate_freehand_points(existing, dx, dy))
        if new_annotation_id is not None:
            copy["id"] = new_annotation_id
        try:
            result = session_manager.upsert_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                copy,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationRecentlyDeleted:
            return {
                "success": False,
                "error": "annotation_recently_deleted",
                "message": (
                    f"Annotation id {new_annotation_id!r} was just deleted by "
                    "another collaborator; retry with a different id."
                ),
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except ImageBudgetExceeded as exc:
            return {"success": False, "error": "too_large", "message": str(exc)}
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Annotation payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation": project_annotation(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def delete_annotation(
        session_id: str,
        annotation_id: str,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Delete an annotation by its stable id.

        Only acts on the generic types `create_annotation` manages (not
        `note`/`group` — use `delete_sticky_note` for notes and
        `delete_group_annotation` for group boxes).

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            annotation_id: The annotation's stable id.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision`. Omit for
                last-write-wins.

        Returns:
            Dict with success, deleted annotation_id, and the new revision.
            Retryable errors: revision_conflict, busy, rate_limited; change
            the request for not_found.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "delete_annotation")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        if _find_generic_annotation(session, annotation_id) is None:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session's generic annotation set.",
            }
        try:
            result = session_manager.delete_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                annotation_id,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationNotFound:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {annotation_id!r} in this session.",
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "annotation_id": annotation_id,
            "revision": result["revision"],
        }

    @register_tool
    def create_group_annotation(
        session_id: str,
        x: float,
        y: float,
        w: Optional[float] = None,
        h: Optional[float] = None,
        label: str = "",
        description: str = "",
        color: Optional[str] = None,
        member_node_ids: Optional[List[str]] = None,
        z: Optional[float] = None,
        locked: bool = False,
        annotation_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create a `group` (node-membership box) annotation, or replace one by id.

        A group is a visual box that tracks a set of graph node ids
        (`member_node_ids`) — not attachment, and not a canonical graph
        relationship (docs/ANNOTATION_CONTRACT.md's "Attachment and detach
        behavior"). Use `update_group_members` to add or remove members after
        creation.

        Coordinates are model space (zoom/pan independent, pixels at zoom 1,
        `x`/`y` = top-left), the same space `list_annotations` reports. Pass
        `annotation_id` to replace an existing group by its stable id (an
        upsert — idempotent for a retried call with the same id); replacing
        an id that already holds a different annotation type is refused
        rather than silently converted. Omit `annotation_id` to have the
        server mint one, returned in the result.

        Unlike the other fields, omitting `member_node_ids` on an upsert
        leaves the group's current membership untouched rather than clearing
        it — resending a group's label or color should not also have to
        resend every member id, and doing so would fight
        `update_group_members`'s own writes. Pass an explicit list
        (including `[]`) to set membership from this call instead.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            x: Model-space x of the group box's top-left corner.
            y: Model-space y of the group box's top-left corner.
            w: Optional width in model-space px (default 320).
            h: Optional height in model-space px (default 200).
            label: Optional group label shown on the box.
            description: Optional longer description.
            color: Optional box color (any CSS color the canvas accepts).
            member_node_ids: Optional list of graph node ids to start the
                group with. Omit to leave current membership alone on an
                upsert, or create an empty group. Use `update_group_members`
                afterward for ongoing add/remove.
            z: Optional layer order. Defaults to 0. Stored and reported
                back, but not drawn for a group: the canvas paints groups as
                backdrops behind their members in node-array order, so a
                group's `z` does not change what covers what. Set it if you
                want the value preserved; do not expect it to reorder
                anything.
            locked: Whether the group starts locked against edits. The canvas
                honours it: a locked group refuses recolour, rename, resize,
                drag and delete, and offers only unlock. A group's menu has
                no hide action at all, locked or not.
            annotation_id: Stable id to create or replace. Omit to let the
                server assign one.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision` (optimistic
                concurrency). Read it from `list_annotations` first. Omit
                for last-write-wins.

        Returns:
            Dict with success, the created/replaced group (same projected
            shape as `list_annotations`, with `label`/`description`/`color`/
            `member_node_ids` under `content`), and the new revision.
            Retryable errors: revision_conflict, busy, rate_limited; change
            the request for invalid_content, wrong_type, or too_large.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "create_group_annotation")
        if denied:
            return denied
        if annotation_id is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                existing_annotation = _find_any_annotation(session, annotation_id)
                if existing_annotation is not None and not is_group(
                    existing_annotation
                ):
                    return {
                        "success": False,
                        "error": "wrong_type",
                        "message": (
                            f"Annotation id {annotation_id!r} already exists as a "
                            "different annotation type; create_group_annotation "
                            "only creates or replaces groups."
                        ),
                    }
        try:
            annotation = build_group_annotation(
                x=x,
                y=y,
                w=w,
                h=h,
                label=label,
                description=description,
                color=color,
                member_node_ids=member_node_ids,
                z=z,
                locked=locked,
                annotation_id=annotation_id,
            )
        except ValueError as exc:
            return {"success": False, "error": "invalid_content", "message": str(exc)}
        try:
            result = session_manager.upsert_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                annotation,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationRecentlyDeleted:
            return {
                "success": False,
                "error": "annotation_recently_deleted",
                "message": (
                    f"Annotation id {annotation_id!r} was just deleted by another "
                    "collaborator; retry with a different id."
                ),
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Group payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "group": project_annotation(result["annotation"]),
            "revision": result["revision"],
        }

    @register_tool
    def update_group_members(
        session_id: str,
        group_id: str,
        add_member_node_ids: Optional[List[str]] = None,
        remove_member_node_ids: Optional[List[str]] = None,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Add and/or remove graph node ids from an existing group's membership.

        Reads the group's current `member_node_ids`, applies the given
        additions and removals (dedupes; a duplicate add is a no-op, an id
        not present is dropped by a remove without error), and writes the
        result as one `group_membership_changed` op — so the caller does not
        have to fetch the full current list first just to add or remove one
        id. Two calls issued back to back each compute their delta from the
        list the previous call actually wrote, so neither has to know the
        other's outcome in advance — but, like every other MCP annotation
        write, this is last-write-wins under a genuine race between two
        concurrent calls (no CRDT — see `session_manager.py`'s module
        docstring): pass `expected_revision` if you need the write rejected
        instead of silently applied over a concurrent change. This op has no
        undo entry (docs/ANNOTATION_CONTRACT.md's cross-type row, and
        `session_activity.UNDOABLE_OPS`'s docstring — membership changes are
        not currently undoable through `undo_last_action`, unlike most other
        annotation writes).

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            group_id: The group annotation's stable id (from
                `create_group_annotation` or `list_annotations`).
            add_member_node_ids: Optional list of graph node ids to add.
            remove_member_node_ids: Optional list of graph node ids to remove.
                At least one of `add_member_node_ids`/`remove_member_node_ids`
                is required.
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision` (optimistic
                concurrency). Read it from `list_annotations` first. Omit
                for last-write-wins.

        Returns:
            Dict with success, the group (same projected shape as
            `list_annotations`), the resulting `member_node_ids`, and the new
            revision. Retryable errors: revision_conflict, busy, rate_limited;
            change the request for invalid_content, not_found, or too_large.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "update_group_members")
        if denied:
            return denied
        if add_member_node_ids is None and remove_member_node_ids is None:
            return {
                "success": False,
                "error": "no_fields_to_update",
                "message": (
                    "Provide add_member_node_ids and/or remove_member_node_ids."
                ),
            }
        for field_name, value in (
            ("add_member_node_ids", add_member_node_ids),
            ("remove_member_node_ids", remove_member_node_ids),
        ):
            if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(v, str) for v in value)
            ):
                return {
                    "success": False,
                    "error": "invalid_content",
                    "message": f"{field_name} must be a list of strings",
                }
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        existing = _find_any_annotation(session, group_id)
        if existing is None or not is_group(existing):
            return {
                "success": False,
                "error": "not_found",
                "message": f"No group annotation with id {group_id!r} in this session.",
            }
        current = [
            m for m in (existing.get("member_node_ids") or []) if isinstance(m, str)
        ]
        seen = set(current)
        for node_id in add_member_node_ids or []:
            if node_id not in seen:
                current.append(node_id)
                seen.add(node_id)
        if remove_member_node_ids:
            removing = set(remove_member_node_ids)
            current = [m for m in current if m not in removing]
        try:
            result = session_manager.set_group_members(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                group_id,
                current,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except OpBatchTooLarge:
            return {
                "success": False,
                "error": "too_large",
                "message": "Membership payload too large for one write.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        updated = result.get("annotation")
        return {
            "success": True,
            "session_id": session_id,
            "group": project_annotation(updated) if updated else None,
            "member_node_ids": current,
            "revision": result["revision"],
        }

    @register_tool
    def delete_group_annotation(
        session_id: str,
        group_id: str,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Delete a `group` (node-membership box) annotation by its stable id.

        This removes only the group box itself. `member_node_ids` names
        graph nodes by id, not annotations the group owns — the annotation
        model never writes graph nodes or edges
        (docs/ANNOTATION_CONTRACT.md's "Scope" and "Persistence" sections),
        so there is nothing else to cascade-delete. This matches the GUI's
        own group-delete behavior (`GroupNode.jsx`'s "Delete Group" action,
        `removeGroupKeepChildren`): it un-parents and keeps every member
        node, never deleting or hiding them, and only removes the group
        container. A membership change is not itself undoable through
        `undo_last_action` (`update_group_members`'s docstring), but
        deleting the group annotation is, like any other annotation type
        (`session_activity.UNDOABLE_OPS`).

        Only acts on `group`-typed annotations — a note or generic-type id
        is refused as `not_found`, matching `update_group_members`'s and
        `delete_annotation`'s cross-type boundary; use `delete_annotation`
        for generic types and `delete_sticky_note` for notes.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")
            group_id: The group annotation's stable id (from
                `create_group_annotation` or `list_annotations`).
            expected_revision: If given, the write is rejected unless it
                equals the session's current `revision`. Omit for
                last-write-wins.

        Returns:
            Dict with success, deleted group_id, and the new revision.
            Retryable errors: revision_conflict, busy, rate_limited; change
            the request for not_found.
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}
        if not is_valid_session_id(session_id):
            return {
                "success": False,
                "error": "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD",
            }
        denied = _authorize_session(GRAPH_ACTION_MUTATE, "delete_group_annotation")
        if denied:
            return denied
        session = session_manager.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        existing = _find_any_annotation(session, group_id)
        if existing is None or not is_group(existing):
            return {
                "success": False,
                "error": "not_found",
                "message": f"No group annotation with id {group_id!r} in this session.",
            }
        try:
            result = session_manager.delete_annotation(
                session_id,
                _MCP_LAYOUT_CLIENT_ID,
                group_id,
                expected_revision=expected_revision,
            )
        except RevisionConflict as exc:
            return {
                "success": False,
                "error": "revision_conflict",
                "message": (
                    "The session changed since you read it; re-read "
                    "list_annotations and retry with the current revision."
                ),
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }
        except AnnotationNotFound:
            return {
                "success": False,
                "error": "not_found",
                "message": f"No annotation with id {group_id!r} in this session.",
            }
        except LayoutBusy:
            return {
                "success": False,
                "error": "busy",
                "message": "Another change is being applied to this session; retry.",
            }
        except RateLimited:
            return {
                "success": False,
                "error": "rate_limited",
                "message": "Too many session writes; slow down and retry.",
            }
        except SessionNotFound:
            return {
                "success": False,
                "error": (
                    f"Session '{session_id}' not found. "
                    "This tool acts on a session's stored state, which exists "
                    "once create_visualization_session created it or a browser "
                    "made its first change to it."
                ),
            }
        except OpError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "session_id": session_id,
            "group_id": group_id,
            "revision": result["revision"],
        }

    return tools_map


def _push_to_session(
    session_registry,
    session_id: Optional[str],
    tool_name: str,
    result: Dict[str, Any],
    session_manager=None,
) -> None:
    """Push *result* to a browser session if *session_id* is set.

    When the result has nodes but no explicit *action*, defaults to
    "add_to_visualization" so external AI tools add to the canvas rather
    than silently replacing it.

    The command goes to the legacy single-consumer registry (current frontend)
    and, when a *session_manager* is supplied, is also broadcast to the new
    shared-session hub so every connected collaborator receives it (design 3.8).
    """
    if not session_id:
        return
    command_result = dict(result)
    if "action" not in command_result and command_result.get("nodes"):
        command_result["action"] = "add_to_visualization"
    # A unique id lets the browser dedupe the legacy stream and the hub
    # broadcast delivering the same push during the handover between them
    # (design §8.1 R5) without mistaking a later, genuinely repeated command
    # for a duplicate of this one.
    command = {
        "type": "tool_result",
        "tool": tool_name,
        "result": command_result,
        "command_id": secrets.token_hex(8),
    }
    if session_registry and session_registry.is_valid_session_id(session_id):
        session_registry.push_command_sync(session_id, command)
    if session_manager is not None:
        try:
            session_manager.push_command(session_id, command)
        except Exception:
            # Best-effort mirror to the hub; never break the legacy push path.
            pass
