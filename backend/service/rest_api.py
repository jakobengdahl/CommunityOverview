"""
REST API router for graph operations.

Provides FastAPI routes that expose GraphService methods via HTTP endpoints.
This module handles HTTP-specific concerns like request/response formatting,
error handling, and route definitions.

Usage:
    from fastapi import FastAPI
    from backend.service import GraphService, create_rest_router
    from backend.core import GraphStorage

    app = FastAPI()
    storage = GraphStorage("graph.json")
    service = GraphService(storage)
    router = create_rest_router(service)
    app.include_router(router, prefix="/api")
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel, Field

from .service import GraphService


# ==================== Request/Response Models ====================

class SearchRequest(BaseModel):
    """Request model for search operations."""
    query: str = Field(..., description="Search text")
    node_types: Optional[List[str]] = Field(None, description="Filter by node types")
    limit: int = Field(50, ge=1, le=500, description="Max results")


class RelatedNodesRequest(BaseModel):
    """Request model for related nodes query."""
    node_id: str = Field(..., description="Starting node ID")
    relationship_types: Optional[List[str]] = Field(None, description="Filter by relationship types")
    depth: int = Field(1, ge=1, le=5, description="Traversal depth")


class SimilarNodesRequest(BaseModel):
    """Request model for similarity search."""
    name: str = Field(..., description="Name to search for")
    node_type: Optional[str] = Field(None, description="Filter by node type")
    threshold: float = Field(0.7, ge=0.0, le=1.0, description="Similarity threshold")
    limit: int = Field(5, ge=1, le=50, description="Max results")


class SimilarNodesBatchRequest(BaseModel):
    """Request model for batch similarity search."""
    names: List[str] = Field(..., description="Names to search for")
    node_type: Optional[str] = Field(None, description="Filter by node type")
    threshold: float = Field(0.7, ge=0.0, le=1.0, description="Similarity threshold")
    limit: int = Field(5, ge=1, le=50, description="Max results per name")


class AddNodesRequest(BaseModel):
    """Request model for adding nodes."""
    nodes: List[Dict[str, Any]] = Field(..., description="Nodes to add")
    edges: List[Dict[str, Any]] = Field(default_factory=list, description="Edges to add")
    # Event context (optional, for webhooks/loop prevention)
    event_origin: Optional[str] = Field(None, description="Source of mutation (web-ui, mcp, system, agent:<id>)")
    event_session_id: Optional[str] = Field(None, description="Session ID for loop prevention")
    event_correlation_id: Optional[str] = Field(None, description="Correlation ID for chaining events")


class UpdateNodeRequest(BaseModel):
    """Request model for updating a node."""
    updates: Dict[str, Any] = Field(..., description="Fields to update")
    # Event context (optional, for webhooks/loop prevention)
    event_origin: Optional[str] = Field(None, description="Source of mutation (web-ui, mcp, system, agent:<id>)")
    event_session_id: Optional[str] = Field(None, description="Session ID for loop prevention")
    event_correlation_id: Optional[str] = Field(None, description="Correlation ID for chaining events")


class DeleteNodesRequest(BaseModel):
    """Request model for deleting nodes."""
    node_ids: List[str] = Field(..., max_length=10, description="Node IDs to delete (max 10)")
    confirmed: bool = Field(False, description="Confirmation flag")
    # Event context (optional, for webhooks/loop prevention)
    event_origin: Optional[str] = Field(None, description="Source of mutation (web-ui, mcp, system, agent:<id>)")
    event_session_id: Optional[str] = Field(None, description="Session ID for loop prevention")
    event_correlation_id: Optional[str] = Field(None, description="Correlation ID for chaining events")


class AddEdgeRequest(BaseModel):
    """Request model for adding a single edge."""
    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    type: Optional[str] = Field(None, description="Relationship type (optional, defaults to RELATES_TO)")
    label: Optional[str] = Field(None, description="Free-text label (optional)")
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(None, description="Session ID for loop prevention")
    event_correlation_id: Optional[str] = Field(None, description="Correlation ID for chaining events")


class UpdateEdgeRequest(BaseModel):
    """Request model for updating an edge."""
    updates: Dict[str, Any] = Field(..., description="Fields to update (type, label, metadata)")
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(None, description="Session ID for loop prevention")
    event_correlation_id: Optional[str] = Field(None, description="Correlation ID for chaining events")


class DeleteEdgeRequest(BaseModel):
    """Request model for deleting a single edge."""
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(None, description="Session ID for loop prevention")
    event_correlation_id: Optional[str] = Field(None, description="Correlation ID for chaining events")


class SaveViewRequest(BaseModel):
    """Request model for saving a view."""
    name: str = Field(..., min_length=1, max_length=200, description="View name")


# ==================== Router Factory ====================

def create_rest_router(service: GraphService, prefix: str = "") -> APIRouter:
    """
    Create a FastAPI router with all graph operation endpoints.

    Args:
        service: GraphService instance to use for operations
        prefix: Optional URL prefix for all routes

    Returns:
        Configured APIRouter
    """
    router = APIRouter(prefix=prefix, tags=["graph"])

    # ==================== Search Endpoints ====================

    @router.post("/search")
    async def search_graph(request: SearchRequest) -> Dict[str, Any]:
        """Search for nodes in the graph based on text query."""
        return service.search_graph(
            query=request.query,
            node_types=request.node_types,
            limit=request.limit
        )

    @router.get("/nodes/{node_id}")
    async def get_node_details(node_id: str) -> Dict[str, Any]:
        """Get complete information about a specific node."""
        result = service.get_node_details(node_id)
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.post("/nodes/{node_id}/related")
    async def get_related_nodes(
        node_id: str,
        relationship_types: Optional[List[str]] = Body(None),
        depth: int = Body(1, ge=1, le=5)
    ) -> Dict[str, Any]:
        """Get nodes connected to the given node."""
        return service.get_related_nodes(
            node_id=node_id,
            relationship_types=relationship_types,
            depth=depth
        )

    # ==================== Similarity Endpoints ====================

    @router.post("/similar")
    async def find_similar_nodes(request: SimilarNodesRequest) -> Dict[str, Any]:
        """Find similar nodes based on name (for duplicate detection)."""
        return service.find_similar_nodes(
            name=request.name,
            node_type=request.node_type,
            threshold=request.threshold,
            limit=request.limit
        )

    @router.post("/similar/batch")
    async def find_similar_nodes_batch(request: SimilarNodesBatchRequest) -> Dict[str, Any]:
        """Find similar nodes for multiple names at once."""
        return service.find_similar_nodes_batch(
            names=request.names,
            node_type=request.node_type,
            threshold=request.threshold,
            limit=request.limit
        )

    # ==================== CRUD Endpoints ====================

    @router.post("/nodes")
    async def add_nodes(request: AddNodesRequest) -> Dict[str, Any]:
        """Add new nodes and edges to the graph."""
        result = service.add_nodes(
            nodes=request.nodes,
            edges=request.edges,
            event_origin=request.event_origin,
            event_session_id=request.event_session_id,
            event_correlation_id=request.event_correlation_id,
        )
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result

    @router.patch("/nodes/{node_id}")
    async def update_node(node_id: str, request: UpdateNodeRequest) -> Dict[str, Any]:
        """Update an existing node."""
        result = service.update_node(
            node_id,
            request.updates,
            event_origin=request.event_origin,
            event_session_id=request.event_session_id,
            event_correlation_id=request.event_correlation_id,
        )
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.delete("/nodes")
    async def delete_nodes(request: DeleteNodesRequest) -> Dict[str, Any]:
        """Delete nodes from the graph (max 10 at a time)."""
        result = service.delete_nodes(
            node_ids=request.node_ids,
            confirmed=request.confirmed,
            event_origin=request.event_origin,
            event_session_id=request.event_session_id,
            event_correlation_id=request.event_correlation_id,
        )
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result

    # ==================== Edge CRUD Endpoints ====================

    @router.post("/edges")
    async def add_edge(request: AddEdgeRequest) -> Dict[str, Any]:
        """Add a single edge between existing nodes. Type is optional (defaults to RELATES_TO)."""
        result = service.add_edge(
            source=request.source,
            target=request.target,
            type=request.type,
            label=request.label,
            event_origin=request.event_origin,
            event_session_id=request.event_session_id,
            event_correlation_id=request.event_correlation_id,
        )
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result

    @router.patch("/edges/{edge_id}")
    async def update_edge(edge_id: str, request: UpdateEdgeRequest) -> Dict[str, Any]:
        """Update an existing edge (type, label, metadata)."""
        result = service.update_edge(
            edge_id,
            request.updates,
            event_origin=request.event_origin,
            event_session_id=request.event_session_id,
            event_correlation_id=request.event_correlation_id,
        )
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.delete("/edges/{edge_id}")
    async def delete_edge(edge_id: str) -> Dict[str, Any]:
        """Delete a single edge."""
        result = service.delete_edge(edge_id, event_origin="web-ui")
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    # ==================== Statistics & Metadata Endpoints ====================

    @router.get("/stats")
    async def get_graph_stats() -> Dict[str, Any]:
        """Get statistics for the graph."""
        return service.get_graph_stats()

    @router.get("/meta/node-types")
    async def list_node_types() -> Dict[str, Any]:
        """List all allowed node types according to the schema config."""
        return service.list_node_types()

    @router.get("/meta/relationship-types")
    async def list_relationship_types() -> Dict[str, Any]:
        """List all allowed relationship types according to schema config."""
        return service.list_relationship_types()

    @router.get("/meta/subtypes")
    async def get_subtypes(node_type: Optional[str] = Query(None, description="Filter by node type")) -> Dict[str, Any]:
        """Get existing subtypes used in the graph, grouped by node type."""
        return service.get_subtypes(node_type)

    @router.get("/schema")
    async def get_schema() -> Dict[str, Any]:
        """Get the complete schema configuration (node types, relationship types)."""
        return service.get_schema()

    @router.get("/presentation")
    async def get_presentation() -> Dict[str, Any]:
        """Get the presentation configuration (colors, prompts, introduction text)."""
        return service.get_presentation()

    # ==================== Saved Views Endpoints ====================

    @router.post("/views/save")
    async def save_view(request: SaveViewRequest) -> Dict[str, Any]:
        """Signal intent to save the current view state."""
        return service.save_view(request.name)

    @router.get("/views/{name}")
    async def get_saved_view(name: str) -> Dict[str, Any]:
        """Get a saved view by name and load its content."""
        result = service.get_saved_view(name)
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.get("/views")
    async def list_saved_views() -> Dict[str, Any]:
        """List all saved views."""
        return service.list_saved_views()

    # ==================== Export Endpoint ====================

    @router.get("/export")
    async def export_graph() -> Dict[str, Any]:
        """Export the entire graph (all nodes and edges)."""
        return service.export_graph()

    return router
