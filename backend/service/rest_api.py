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

import asyncio
import json
import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Body, Request, Path
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from backend.config.config_loader import RestInterfaceConfig, get_rest_interfaces
from backend.core.image_ingest import (
    DEFAULT_MAX_SOURCE_IMAGE_BYTES,
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
    annotation_type_of,
    build_annotation,
    is_generic_annotation,
)
from backend.core.storage_search import MATCH_MODE_SUBSTRING, validate_match_mode
from backend.runtime.authorization import use_request_authorization

from .service import GraphService

logger = logging.getLogger(__name__)


def _lookup_rate_key(http_request: Request) -> str:
    """Return the real client IP to key the lookup rate limit on.

    Directly reached (``trusted_proxy_hops == 0``): the socket peer. Behind
    ``N`` trusted proxies: the ``N``-th entry from the right of
    ``X-Forwarded-For`` — the address the outermost trusted proxy recorded.
    Client-supplied entries sit further left and are ignored, so the key
    cannot be spoofed to mint a fresh per-source budget.
    """
    direct = http_request.client.host if http_request.client else "unknown"
    config = getattr(http_request.app.state, "config", None)
    trusted_hops = getattr(config, "trusted_proxy_hops", 0) or 0
    if trusted_hops <= 0:
        return direct
    forwarded = http_request.headers.get("x-forwarded-for")
    if not forwarded:
        return direct
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not parts:
        return direct
    idx = max(0, len(parts) - trusted_hops)
    return parts[idx]


# ==================== Request/Response Models ====================


class SearchRequest(BaseModel):
    """Request model for search operations."""

    query: str = Field(..., description="Search text")
    node_types: Optional[List[str]] = Field(None, description="Filter by node types")
    limit: int = Field(50, ge=1, le=500, description="Max results")
    federation_depth: Optional[int] = Field(
        None, ge=1, le=9, description="Optional federated search depth"
    )
    tags_any: Optional[List[str]] = Field(
        None, description="Keep nodes carrying at least one of these tags (OR)"
    )
    tags_all: Optional[List[str]] = Field(
        None, description="Keep nodes carrying every one of these tags (AND)"
    )
    tags_none: Optional[List[str]] = Field(
        None, description="Drop nodes carrying any of these tags (exclude)"
    )
    metadata_filters: Optional[List[Dict[str, Any]]] = Field(
        None,
        description=(
            "Generic metadata filters, each "
            '{"key": str, "values": [...], "match": "any"|"all"|"none"}'
        ),
    )
    include_archived: bool = Field(
        False,
        description="When false (default) archived nodes and edges are excluded",
    )
    semantic: bool = Field(
        False,
        description=(
            "When true, rank results by embedding meaning instead of lexical "
            "substring matching. Lexical search still auto-falls back to semantic "
            "ranking when it returns zero results."
        ),
    )
    match_mode: str = Field(
        MATCH_MODE_SUBSTRING,
        description=(
            "Lexical match mode: 'substring' (default) requires the whole query "
            "verbatim; 'any_term' matches nodes containing any of the query's "
            "distinct whitespace-separated terms, and a repeated term counts "
            "once. Ignored when semantic is true."
        ),
    )

    @field_validator("match_mode")
    @classmethod
    def _validate_match_mode(cls, value: str) -> str:
        """Reject an unsupported mode as a 422 rather than a 500 from the core."""
        return validate_match_mode(value)


class RelatedNodesRequest(BaseModel):
    """Request model for related nodes query."""

    node_id: str = Field(..., description="Starting node ID")
    relationship_types: Optional[List[str]] = Field(
        None, description="Filter by relationship types"
    )
    depth: int = Field(1, ge=1, le=5, description="Traversal depth")
    include_archived: bool = Field(
        False,
        description="When false (default) archived edges and neighbour nodes are excluded",
    )


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
    edges: List[Dict[str, Any]] = Field(
        default_factory=list, description="Edges to add"
    )
    # Event context (optional, for webhooks/loop prevention)
    event_origin: Optional[str] = Field(
        None, description="Source of mutation (web-ui, mcp, system, agent:<id>)"
    )
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class UpdateNodeRequest(BaseModel):
    """Request model for updating a node."""

    updates: Dict[str, Any] = Field(..., description="Fields to update")
    metadata_merge: bool = Field(
        False,
        description=(
            "When True, merge the `metadata` object field-by-field onto the "
            "node's existing metadata (a null value removes a key) instead of "
            "replacing the whole object. Default False keeps replace semantics."
        ),
    )
    expected_updated_at: Optional[str] = Field(
        None,
        description=(
            "Optimistic-concurrency guard: the `updated_at` the caller last read. "
            "The update is rejected with 409 if the node changed since then."
        ),
    )
    # Event context (optional, for webhooks/loop prevention)
    event_origin: Optional[str] = Field(
        None, description="Source of mutation (web-ui, mcp, system, agent:<id>)"
    )
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class DeleteNodesRequest(BaseModel):
    """Request model for deleting nodes."""

    node_ids: List[str] = Field(
        ..., max_length=10, description="Node IDs to delete (max 10)"
    )
    confirmed: bool = Field(False, description="Confirmation flag")
    # Event context (optional, for webhooks/loop prevention)
    event_origin: Optional[str] = Field(
        None, description="Source of mutation (web-ui, mcp, system, agent:<id>)"
    )
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class ArchiveNodesRequest(BaseModel):
    """Request model for archiving/unarchiving nodes."""

    node_ids: List[str] = Field(..., description="Node IDs to archive/unarchive")
    archived: bool = Field(
        True, description="True to archive (hide), False to unarchive (restore)"
    )
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class ArchiveEdgesRequest(BaseModel):
    """Request model for archiving/unarchiving edges."""

    edge_ids: List[str] = Field(..., description="Edge IDs to archive/unarchive")
    archived: bool = Field(
        True, description="True to archive (hide), False to unarchive (restore)"
    )
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class AddEdgeRequest(BaseModel):
    """Request model for adding a single edge."""

    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    type: Optional[str] = Field(
        None, description="Relationship type (optional, defaults to RELATES_TO)"
    )
    label: Optional[str] = Field(None, description="Free-text label (optional)")
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class UpdateEdgeRequest(BaseModel):
    """Request model for updating an edge."""

    updates: Dict[str, Any] = Field(
        ..., description="Fields to update (type, label, metadata)"
    )
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class DeleteEdgeRequest(BaseModel):
    """Request model for deleting a single edge."""

    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class AdoptFederatedNodeRequest(BaseModel):
    """Request model for adopting a federated cached node into local graph."""

    federated_node_id: str = Field(..., description="Federated cached node ID")
    local_name: Optional[str] = Field(None, description="Optional local name override")
    relationship_type: str = Field("ADOPTED_FROM", description="Lineage relation type")
    create_new_copy: bool = Field(
        False, description="Create a new local copy even if already adopted"
    )
    event_origin: Optional[str] = Field(None, description="Source of mutation")
    event_session_id: Optional[str] = Field(
        None, description="Session ID for loop prevention"
    )
    event_correlation_id: Optional[str] = Field(
        None, description="Correlation ID for chaining events"
    )


class SaveViewRequest(BaseModel):
    """Request model for saving a view."""

    name: str = Field(..., min_length=1, max_length=200, description="View name")


class CreateSessionRequest(BaseModel):
    """Request model for creating a shared session."""

    name: Optional[str] = Field(
        None, max_length=200, description="Optional session name"
    )


class RenameSessionRequest(BaseModel):
    """Request model for renaming a shared session."""

    name: Optional[str] = Field(
        None, max_length=200, description="New session name (or null to clear)"
    )
    client_id: Optional[str] = Field(
        None,
        max_length=100,
        description="Renaming client id (attributes the op; defaults server-side)",
    )


class SessionOpsRequest(BaseModel):
    """Request model for a batch of session ops."""

    client_id: str = Field(
        ..., min_length=1, max_length=100, description="Originating client id"
    )
    base_seq: Optional[int] = Field(
        None, description="Client's last-known seq (informational)"
    )
    ops: List[Dict[str, Any]] = Field(..., description="Ordered ops to apply")


class UndoSessionActionRequest(BaseModel):
    """Request model for undoing a session actor's latest eligible action."""

    client_id: str = Field(
        ..., min_length=1, max_length=100, description="Requesting actor's client id"
    )
    expected_revision: Optional[int] = Field(
        None, description="Client's last-known seq; conflicts if the session moved on"
    )


class IngestSessionImageRequest(BaseModel):
    """Request model for the human clipboard-paste / file-upload image GUI.

    Mirrors the MCP ``create_image_annotation`` tool's inputs (same underlying
    ``image_ingest``/``upsert_image_annotation`` pipeline — see
    ``_register_session_endpoints``'s ``ingest_session_image`` handler), so a
    human pasting or dropping an image on the canvas goes through the same
    validated, budget-enforced ingest as an MCP agent.
    """

    client_id: str = Field(
        ..., min_length=1, max_length=100, description="Pasting browser's client id"
    )
    x: float
    y: float
    image_data: Optional[str] = Field(
        None, description="Image bytes as a data: URL or bare base64"
    )
    image_url: Optional[str] = Field(
        None, description="http(s) URL to fetch server-side, exactly once"
    )
    w: Optional[float] = None
    h: Optional[float] = None
    rotation: Optional[float] = None
    alt: Optional[str] = None
    style: Optional[Dict[str, Any]] = None
    z: Optional[float] = None
    locked: bool = False
    annotation_id: Optional[str] = None
    expected_revision: Optional[int] = None


def _resolve_stream_event(
    event: Dict[str, Any], session_manager, session_id: str
) -> Dict[str, Any]:
    """Translate a slow-consumer resync sentinel into a fresh full snapshot.

    ``InProcessEventBus.publish`` drops a subscriber's backlog and enqueues a
    ``{"type": "resync"}`` sentinel when that subscriber's queue overflows
    (session_hub.py). Forwarding the sentinel verbatim left the client with no
    way to recover (design §8.1 R2); translating it into a real ``catch_up``
    snapshot here reuses the client's existing "second snapshot means resync"
    handling (`sessionSyncClient.js`), so no wire format or client change is
    needed.
    """
    if event.get("type") == "resync":
        return session_manager.catch_up(session_id, None)
    return event


# Stable marker distinct from any real browser `graph_client_id` (see
# frontend/web/src/services/api.js's getClientId), attributing a human GUI
# image-ingest op's SSE broadcast the same way _MCP_LAYOUT_CLIENT_ID in
# mcp_tools.py attributes an MCP agent's writes. sessionSyncClient.js drops the
# SSE echo of an op carrying the sender's own client_id (standard "don't
# re-apply your own optimistic update" behaviour) — but this op is a server
# round-trip (validate/optimize/embed), so the pasting browser must see the
# server's actual outcome over the normal SSE channel, not a client-side
# guess. Sending the op back under the browser's own client_id would make
# that echo indistinguishable from a self-authored op and it would be
# silently dropped, so a distinct marker is required, not optional.
_HUMAN_IMAGE_INGEST_CLIENT_ID = "human-image-ingest"

# Sanity ceiling for the raw HTTP body of one ``POST .../annotations/image``
# request, checked from ``Content-Length`` (and, as a backstop, the actual
# buffered body) before any parsing happens — mirrors why
# ``apply_session_ops`` needs the same kind of pre-parse check just above: a
# typed Pydantic body parameter lets FastAPI/Starlette read and fully parse
# the request before this handler, or image_ingest's own size checks, ever
# run, so an oversized payload would be fully buffered regardless of what
# decode_image_data later rejects. The legitimate ceiling is a base64
# ``image_data`` payload of the largest accepted source image
# (``DEFAULT_MAX_SOURCE_IMAGE_BYTES``, 20MB): base64's 4/3 expansion plus the
# surrounding JSON puts a real request at roughly 27MB, so this cap uses a
# clean 2x multiple of the source limit for headroom — comfortably above
# that so this early, coarse check cannot itself reject a legitimate upload.
# ``decode_image_data`` still enforces the tight, exact bound once the body
# is parsed.
_MAX_IMAGE_INGEST_BODY_BYTES = 2 * DEFAULT_MAX_SOURCE_IMAGE_BYTES


def _fetch_and_optimize_image(image_data: Optional[str], image_url: Optional[str]):
    """Run the blocking fetch/decode/optimize steps of image ingest.

    Both a URL fetch (network I/O, up to the ingest module's own timeout) and
    Pillow's decode/re-encode (CPU-bound) are synchronous; the caller runs this
    via ``asyncio.to_thread`` so a slow or malicious ``image_url`` cannot stall
    the event loop for every other request. Mirrors the MCP
    ``create_image_annotation`` tool's ingest call exactly, so REST and MCP
    share one ingest path (backend/service/mcp_tools.py).
    """
    raw = (
        fetch_image_bytes(image_url)
        if image_url is not None
        else decode_image_data(image_data)
    )
    return optimize_image(raw)


def _raise_for_access_denied(result: Dict[str, Any]) -> None:
    if result.get("error_code") == "access_denied":
        raise HTTPException(
            status_code=403, detail=result.get("message") or result.get("error")
        )


async def _read_body_within_cap(
    http_request: Request, max_bytes: int, oversized_detail: str
) -> bytes:
    """Read a request body, rejecting early once it is known to exceed ``max_bytes``.

    Shared by ``apply_session_ops`` and ``ingest_session_image``, both of
    which take their body as a raw ``Request`` instead of a typed Pydantic
    parameter specifically so this check can run *before* the request is
    buffered — a typed body parameter would let FastAPI/Starlette read and
    parse the whole thing first, defeating the point of a pre-parse cap.

    Checks the declared ``Content-Length`` header first, so a wildly
    oversized request is rejected without ever being read into memory; then
    re-checks the actually-buffered length as a backstop for a request whose
    header is missing, absent, or understates the truth (e.g. chunked
    transfer-encoding, or a client that simply lies).
    """
    content_length = http_request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise HTTPException(status_code=413, detail=oversized_detail)
    body = await http_request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail=oversized_detail)
    return body


def _parse_body_or_422(model_cls, body: bytes):
    """Parse ``body`` as ``model_cls``, matching FastAPI's default 422 shape.

    Automatic body validation no longer runs once the route takes a raw
    ``Request`` (see ``_read_body_within_cap``), so this reproduces it: a
    list of error dicts, the same shape ``RequestValidationError`` produces.
    ``jsonable_encoder`` handles error entries whose ``"input"`` is raw bytes
    (e.g. an invalid-JSON error embeds the undecoded body).
    """
    try:
        return model_cls.model_validate_json(body)
    except PydanticValidationError as exc:
        raise HTTPException(status_code=422, detail=jsonable_encoder(exc.errors()))


# ==================== Route Registration Helpers ====================


def _register_search_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.post("/search")
    async def search_graph(
        request: SearchRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Search for nodes in the graph based on text query."""
        with use_request_authorization(headers=http_request.headers):
            result = service.search_graph(
                query=request.query,
                node_types=request.node_types,
                limit=request.limit,
                federation_depth=request.federation_depth,
                tags_any=request.tags_any,
                tags_all=request.tags_all,
                tags_none=request.tags_none,
                metadata_filters=request.metadata_filters,
                include_archived=request.include_archived,
                semantic=request.semantic,
                match_mode=request.match_mode,
            )
        _raise_for_access_denied(result)
        return result

    @router.get("/nodes/{node_id}")
    async def get_node_details(node_id: str, request: Request) -> Dict[str, Any]:
        """Get complete information about a specific node."""
        with use_request_authorization(headers=request.headers):
            result = service.get_node_details(node_id)
        _raise_for_access_denied(result)
        if not result.get("success", True):
            status_code = 404 if result.get("error") else 400
            raise HTTPException(
                status_code=status_code,
                detail=result.get("error") or result.get("message"),
            )
        return result

    @router.post("/nodes/{node_id}/related")
    async def get_related_nodes(
        node_id: str,
        request: Request,
        relationship_types: Optional[List[str]] = Body(None),
        depth: int = Body(1, ge=1, le=5),
        include_archived: bool = Body(False),
    ) -> Dict[str, Any]:
        """Get nodes connected to the given node."""
        with use_request_authorization(headers=request.headers):
            result = service.get_related_nodes(
                node_id=node_id,
                relationship_types=relationship_types,
                depth=depth,
                include_archived=include_archived,
            )
        _raise_for_access_denied(result)
        return result


def _register_similarity_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.post("/similar")
    async def find_similar_nodes(request: SimilarNodesRequest) -> Dict[str, Any]:
        """Find similar nodes based on name (for duplicate detection)."""
        return service.find_similar_nodes(
            name=request.name,
            node_type=request.node_type,
            threshold=request.threshold,
            limit=request.limit,
        )

    @router.post("/similar/batch")
    async def find_similar_nodes_batch(
        request: SimilarNodesBatchRequest,
    ) -> Dict[str, Any]:
        """Find similar nodes for multiple names at once."""
        return service.find_similar_nodes_batch(
            names=request.names,
            node_type=request.node_type,
            threshold=request.threshold,
            limit=request.limit,
        )

    @router.post("/federation/adopt")
    async def adopt_federated_node(
        request: AdoptFederatedNodeRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Adopt (clone) a federated cached node into local graph."""
        with use_request_authorization(headers=http_request.headers):
            result = service.adopt_federated_node(
                federated_node_id=request.federated_node_id,
                local_name=request.local_name,
                relationship_type=request.relationship_type,
                create_new_copy=request.create_new_copy,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(
                status_code=400, detail=result.get("message", "Adoption failed")
            )
        return result


def _register_node_crud_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.post("/nodes")
    async def add_nodes(
        request: AddNodesRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Add new nodes and edges to the graph."""
        with use_request_authorization(headers=http_request.headers):
            result = service.add_nodes(
                nodes=request.nodes,
                edges=request.edges,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result

    @router.patch("/nodes/{node_id}")
    async def update_node(
        node_id: str, request: UpdateNodeRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Update an existing node."""
        with use_request_authorization(headers=http_request.headers):
            result = service.update_node(
                node_id,
                request.updates,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
                metadata_merge=request.metadata_merge,
                expected_updated_at=request.expected_updated_at,
            )
        _raise_for_access_denied(result)
        if result.get("conflict"):
            raise HTTPException(status_code=409, detail=result.get("error"))
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.delete("/nodes")
    async def delete_nodes(
        request: DeleteNodesRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Delete nodes from the graph (max 10 at a time)."""
        with use_request_authorization(headers=http_request.headers):
            result = service.delete_nodes(
                node_ids=request.node_ids,
                confirmed=request.confirmed,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result

    @router.post("/nodes/archive")
    async def archive_nodes(
        request: ArchiveNodesRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Archive or unarchive nodes (hide-by-default vs. permanent delete)."""
        with use_request_authorization(headers=http_request.headers):
            archive = (
                service.archive_nodes if request.archived else service.unarchive_nodes
            )
            result = archive(
                node_ids=request.node_ids,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result


def _register_edge_crud_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.post("/edges")
    async def add_edge(
        request: AddEdgeRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Add a single edge between existing nodes. Type is optional (defaults to RELATES_TO)."""
        with use_request_authorization(headers=http_request.headers):
            result = service.add_edge(
                source=request.source,
                target=request.target,
                type=request.type,
                label=request.label,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result

    @router.patch("/edges/{edge_id}")
    async def update_edge(
        edge_id: str, request: UpdateEdgeRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Update an existing edge (type, label, metadata)."""
        with use_request_authorization(headers=http_request.headers):
            result = service.update_edge(
                edge_id,
                request.updates,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.delete("/edges/{edge_id}")
    async def delete_edge(
        edge_id: str,
        http_request: Request,
        request: Optional[DeleteEdgeRequest] = Body(None),
    ) -> Dict[str, Any]:
        """Delete a single edge."""
        with use_request_authorization(headers=http_request.headers):
            result = service.delete_edge(
                edge_id,
                event_origin=request.event_origin if request else None,
                event_session_id=request.event_session_id if request else None,
                event_correlation_id=request.event_correlation_id if request else None,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.post("/edges/archive")
    async def archive_edges(
        request: ArchiveEdgesRequest, http_request: Request
    ) -> Dict[str, Any]:
        """Archive or unarchive edges (hide-by-default vs. permanent delete)."""
        with use_request_authorization(headers=http_request.headers):
            archive = (
                service.archive_edges if request.archived else service.unarchive_edges
            )
            result = archive(
                edge_ids=request.edge_ids,
                event_origin=request.event_origin,
                event_session_id=request.event_session_id,
                event_correlation_id=request.event_correlation_id,
            )
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result


def _register_history_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.get("/history")
    async def get_graph_history(
        request: Request,
        limit: int = Query(50, ge=1, le=500, description="Max entries to return"),
        offset: int = Query(0, ge=0, description="Number of entries to skip"),
    ) -> Dict[str, Any]:
        """Get recent graph mutation history (newest first)."""
        with use_request_authorization(headers=request.headers):
            result = service.get_graph_history(limit=limit, offset=offset)
        _raise_for_access_denied(result)
        return result

    @router.get("/nodes/{node_id}/history")
    async def get_node_history(
        node_id: str,
        request: Request,
        limit: int = Query(50, ge=1, le=500, description="Max entries to return"),
        offset: int = Query(0, ge=0, description="Number of entries to skip"),
    ) -> Dict[str, Any]:
        """Get mutation history for a single node (newest first)."""
        with use_request_authorization(headers=request.headers):
            result = service.get_node_history(node_id, limit=limit, offset=offset)
        _raise_for_access_denied(result)
        return result

    @router.get("/edges/{edge_id}/history")
    async def get_edge_history(
        edge_id: str,
        request: Request,
        limit: int = Query(50, ge=1, le=500, description="Max entries to return"),
        offset: int = Query(0, ge=0, description="Number of entries to skip"),
    ) -> Dict[str, Any]:
        """Get mutation history for a single edge (newest first)."""
        with use_request_authorization(headers=request.headers):
            result = service.get_edge_history(edge_id, limit=limit, offset=offset)
        _raise_for_access_denied(result)
        return result


def _register_metadata_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.get("/stats")
    async def get_graph_stats(request: Request) -> Dict[str, Any]:
        """Get statistics for the graph."""
        with use_request_authorization(headers=request.headers):
            result = service.get_graph_stats()
        _raise_for_access_denied(result)
        return result

    @router.get("/meta/node-types")
    async def list_node_types() -> Dict[str, Any]:
        """List all allowed node types according to the schema config."""
        return service.list_node_types()

    @router.get("/meta/relationship-types")
    async def list_relationship_types() -> Dict[str, Any]:
        """List all allowed relationship types according to schema config."""
        return service.list_relationship_types()

    @router.get("/meta/relationship-applicability/audit")
    async def audit_relationship_applicability() -> Dict[str, Any]:
        """Report existing edges that violate relationship applicability rules."""
        return service.audit_relationship_applicability()

    @router.get("/meta/subtypes")
    async def get_subtypes(
        node_type: Optional[str] = Query(None, description="Filter by node type"),
    ) -> Dict[str, Any]:
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

    @router.get("/capabilities")
    async def get_capabilities() -> Dict[str, Any]:
        """Get the public capability manifest for client discovery."""
        return service.get_capabilities()

    @router.get("/runtime")
    async def get_runtime_info() -> Dict[str, Any]:
        """Get the public runtime metadata for deployment introspection."""
        return service.get_runtime_info()

    @router.get("/tenant-context")
    async def get_tenant_context() -> Dict[str, Any]:
        """Get the tenant/deployment context metadata."""
        return service.get_tenant_context()

    @router.get("/config-context")
    async def get_config_context() -> Dict[str, Any]:
        """Get the effective public config scope and non-sensitive source metadata."""
        return service.get_config_context()

    @router.get("/request-actor")
    async def get_request_actor(request: Request) -> Dict[str, Any]:
        """Get the public request actor context derived from safe env/request inputs."""
        return service.get_request_actor_info(headers=request.headers)

    @router.get("/request-scope")
    async def get_request_scope(request: Request) -> Dict[str, Any]:
        """Get the public workspace/graph scope context derived from safe env/request inputs."""
        return service.get_request_scope_info(headers=request.headers)

    @router.get("/request-selection")
    async def get_request_selection(request: Request) -> Dict[str, Any]:
        """Get the public graph/workspace selection summary derived from safe env/request inputs."""
        return service.get_request_graph_selection_info(headers=request.headers)


def _register_views_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.post("/views/save")
    async def save_view(request: SaveViewRequest) -> Dict[str, Any]:
        """Signal intent to save the current view state."""
        return service.save_view(request.name)

    @router.get("/views/{name}")
    async def get_saved_view(name: str, request: Request) -> Dict[str, Any]:
        """Get a saved view by name and load its content."""
        with use_request_authorization(headers=request.headers):
            result = service.get_saved_view(name)
        _raise_for_access_denied(result)
        if not result.get("success", True):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.get("/views")
    async def list_saved_views(request: Request) -> Dict[str, Any]:
        """List all saved views."""
        with use_request_authorization(headers=request.headers):
            result = service.list_saved_views()
        _raise_for_access_denied(result)
        return result


def _register_session_endpoints(
    router: APIRouter, service: GraphService, session_manager
) -> None:
    """Register the server-side shared-session REST + SSE endpoints.

    Only wired when a ``session_manager`` is supplied. These live alongside the
    legacy ``/sessions/{id}/state|stream`` MCP-push channel, which is unchanged.
    """
    from backend.core.session_manager import (
        AnnotationRecentlyDeleted,
        ClaimConflict,
        ImageBudgetExceeded,
        LayoutBusy,
        NoUndoableAction,
        OpBatchTooLarge,
        RateLimited,
        RevisionConflict,
        SessionLimitReached,
        SessionNotFound,
        UndoConflict,
    )
    from backend.core.session_store import OpError, is_valid_session_id

    def _rate_limit_lookup(http_request: Request) -> None:
        """Throttle auth-bypassed session-id lookups by client address.

        Guards the enumeration oracle these endpoints expose (200/404 for
        get, session creation for the stream handshake) against brute force.
        """
        try:
            session_manager.check_lookup_rate(_lookup_rate_key(http_request))
        except RateLimited:
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    def _session_payload(session, *, resolve: bool, manager) -> Dict[str, Any]:
        payload = session.to_dict()
        payload["roster"] = manager.roster(session.id)
        if resolve:
            resolved = service.resolve_session_nodes(session.state.get("node_refs", []))
            payload["resolved"] = {
                "nodes": resolved.get("nodes", []),
                "edges": resolved.get("edges", []),
            }
        return payload

    @router.post("/sessions")
    async def create_session(request: CreateSessionRequest) -> Dict[str, Any]:
        try:
            session = session_manager.create_session(request.name)
        except SessionLimitReached:
            raise HTTPException(status_code=503, detail="too many sessions")
        return _session_payload(session, resolve=False, manager=session_manager)

    @router.get("/sessions")
    async def list_sessions() -> Dict[str, Any]:
        return {"sessions": session_manager.list_sessions()}

    @router.get("/sessions/{session_id}")
    async def get_session(
        http_request: Request,
        session_id: str,
        resolve: bool = Query(
            False, description="Resolve node references to node objects"
        ),
    ) -> Dict[str, Any]:
        if not is_valid_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session_id format")
        _rate_limit_lookup(http_request)
        session = session_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return _session_payload(session, resolve=resolve, manager=session_manager)

    @router.patch("/sessions/{session_id}")
    async def rename_session(
        session_id: str, request: RenameSessionRequest
    ) -> Dict[str, Any]:
        if not is_valid_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session_id format")
        try:
            # get-or-create (R7): a rename for an id that only exists in the
            # browser's URL/recents (never saved server-side) must materialise
            # it rather than 404 — otherwise the name is lost the moment the
            # session later saves with a null server name.
            await session_manager.rename_session(
                session_id, request.name, request.client_id
            )
        except SessionLimitReached:
            raise HTTPException(status_code=503, detail="too many sessions")
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")
        except RateLimited:
            # Routing the rename through apply_ops (R8) means it now shares
            # the op token bucket, same as /ops — mirror that endpoint's 429
            # instead of letting bucket exhaustion surface as an unhandled 500.
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        session = session_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return _session_payload(session, resolve=False, manager=session_manager)

    @router.delete("/sessions/{session_id}")
    async def delete_session(
        session_id: str,
        client_id: Optional[str] = Query(
            None, description="Deleting client id (for the notice)"
        ),
    ) -> Dict[str, Any]:
        if not is_valid_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session_id format")
        existed = await session_manager.delete_session(session_id, deleted_by=client_id)
        if not existed:
            raise HTTPException(status_code=404, detail="session not found")
        return {"deleted": True, "id": session_id}

    @router.post("/sessions/{session_id}/ops")
    async def apply_session_ops(
        session_id: str, http_request: Request
    ) -> Dict[str, Any]:
        # Reject an oversized batch from the Content-Length header alone, before
        # buffering the body — the per-op accounting in apply_ops only catches
        # this after FastAPI has already read and parsed the whole body. This
        # pre-parse check cannot yet tell an ordinary batch from one carrying a
        # validated embedded image (that requires decoding the JSON), so it
        # admits the larger `max_request_body_bytes` ceiling; apply_ops applies
        # the tighter, per-case cap once it has parsed the ops.
        max_body_bytes = session_manager.max_request_body_bytes
        body = await _read_body_within_cap(
            http_request, max_body_bytes, "op batch too large"
        )
        request = _parse_body_or_422(SessionOpsRequest, body)
        try:
            return await session_manager.apply_ops(
                session_id, request.client_id, request.base_seq, request.ops
            )
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")
        except RateLimited:
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        except OpBatchTooLarge:
            raise HTTPException(status_code=413, detail="op batch too large")
        except ClaimConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except OpError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/sessions/{session_id}/annotations/image")
    async def ingest_session_image(
        session_id: str, http_request: Request
    ) -> Dict[str, Any]:
        """Human GUI clipboard-paste / file-upload image ingest.

        Shares the exact validate/optimize/embed pipeline the MCP
        ``create_image_annotation`` tool uses (``image_ingest.py`` +
        ``SessionManager.upsert_image_annotation`` — see that tool in
        mcp_tools.py for the sibling error-mapping), so a pasted/uploaded image
        goes through the same server-side ingest, budgets and SSRF protections
        as an MCP agent's. It never persists the raw ``image_url``/``image_data``
        the caller sent, only the optimized embedded copy.

        The annotation is attributed to `_HUMAN_IMAGE_INGEST_CLIENT_ID` rather
        than `request.client_id` (see that constant's docstring for why the
        echo would otherwise be dropped) so the pasting browser's own SSE
        subscription (`GET .../stream`) still receives it — this response is
        no longer the *only* path to seeing it, though: the pasting browser
        (frontend/web/src/App.jsx's handleImageIngest) applies this response
        to its canvas immediately, and treats the later SSE echo as a
        delivery confirmation rather than the sole signal that the upload
        succeeded. Other collaborators watching the same session still learn
        of it only via that echo.

        Takes the body as raw ``Request`` rather than a typed Pydantic
        parameter — same reason as ``apply_session_ops`` above — so
        ``_read_body_within_cap``'s Content-Length pre-check runs before
        FastAPI/Starlette buffers and parses the whole request; a typed body
        parameter would have already done both by the time any size check
        could run.
        """
        if not is_valid_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session_id format")

        body = await _read_body_within_cap(
            http_request, _MAX_IMAGE_INGEST_BODY_BYTES, "image upload too large"
        )
        request = _parse_body_or_422(IngestSessionImageRequest, body)

        if bool(request.image_data) == bool(request.image_url):
            raise HTTPException(
                status_code=400,
                detail="Give exactly one of image_data or image_url.",
            )

        if request.annotation_id is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                existing = next(
                    (
                        a
                        for a in session.state.get("annotations", [])
                        if a.get("id") == request.annotation_id
                    ),
                    None,
                )
                if existing is not None and (
                    not is_generic_annotation(existing)
                    or annotation_type_of(existing) != "image"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Annotation id {request.annotation_id!r} already "
                            "exists as a different type; image upload will not "
                            "silently convert it."
                        ),
                    )
                # This replaces an existing annotation directly (not through
                # apply_ops, so ClaimMap enforcement there never sees it) — the
                # pasting browser's own client_id is known here (unlike the op
                # sent to upsert_image_annotation below, which is always
                # attributed to the shared _HUMAN_IMAGE_INGEST_CLIENT_ID for SSE
                # echo purposes — see that constant's docstring), so check it
                # against the same live-claim snapshot apply_ops uses, before
                # paying for the fetch/optimize below.
                if existing is not None:
                    holder = session_manager.claims.snapshot(session_id).get(
                        request.annotation_id
                    )
                    if holder is not None and holder != request.client_id:
                        raise HTTPException(
                            status_code=409,
                            detail=str(ClaimConflict(request.annotation_id, holder)),
                        )

        try:
            optimized = await asyncio.to_thread(
                _fetch_and_optimize_image, request.image_data, request.image_url
            )
        except SourceImageTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except ImageFetchError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except InvalidImageData as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except UnsupportedImageType as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        except OptimizedImageTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc))

        content = {
            "image": {
                "url": optimized.data_url,
                "width": optimized.width,
                "height": optimized.height,
            },
            "alt": request.alt or "",
        }
        try:
            annotation = build_annotation(
                type="image",
                x=request.x,
                y=request.y,
                w=request.w if request.w is not None else optimized.width,
                h=request.h if request.h is not None else optimized.height,
                rotation=request.rotation,
                content=content,
                style=request.style,
                z=request.z,
                locked=request.locked,
                annotation_id=request.annotation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Preserve the pasting browser's real identity as the annotation's
        # human-visible creator; only the op's attribution (SSE broadcast +
        # activity actor, passed as the second arg below) uses the dedicated
        # marker. `SessionStore.apply_state_op` only defaults `created_by` from
        # the op's client_id when the annotation doesn't already carry one, so
        # setting it here keeps both facts distinct and correct.
        annotation["created_by"] = request.client_id

        try:
            result = session_manager.upsert_image_annotation(
                session_id,
                _HUMAN_IMAGE_INGEST_CLIENT_ID,
                annotation,
                optimized_image_bytes=len(optimized.data),
                expected_revision=request.expected_revision,
            )
        except RevisionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail=f"expected revision {exc.expected}, session is at {exc.actual}",
            )
        except AnnotationRecentlyDeleted:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Annotation id {request.annotation_id!r} was just deleted "
                    "by another collaborator; retry with a different id."
                ),
            )
        except LayoutBusy:
            raise HTTPException(status_code=409, detail="session busy, retry")
        except RateLimited:
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        except ImageBudgetExceeded as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")
        except OpError as exc:
            # A same-id collision with a different type slipped past the
            # pre-check above (a concurrent write landed in the window between
            # that read and this write — the pre-check is a fast-path UX
            # nicety, not the enforcement point); SessionStore.apply_state_op
            # is the actual authority and raises OpError here instead of
            # silently retyping the annotation.
            raise HTTPException(status_code=409, detail=str(exc))

        return {
            "annotation": result.get("annotation"),
            "revision": result.get("revision"),
        }

    @router.get("/sessions/{session_id}/activity")
    async def get_session_activity(
        session_id: str,
        actor: Optional[str] = Query(
            None, description="Restrict to activity by this client id"
        ),
        limit: int = Query(50, ge=1, le=500, description="Max records to return"),
    ) -> Dict[str, Any]:
        if not is_valid_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session_id format")
        try:
            records = session_manager.list_activity(
                session_id, actor=actor, limit=limit
            )
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "activity": records}

    @router.post("/sessions/{session_id}/undo")
    async def undo_session_action(
        session_id: str, request: UndoSessionActionRequest
    ) -> Dict[str, Any]:
        if not is_valid_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session_id format")
        try:
            return session_manager.undo_last_action(
                session_id,
                request.client_id,
                expected_revision=request.expected_revision,
            )
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")
        except NoUndoableAction:
            raise HTTPException(status_code=404, detail="no undoable action")
        except UndoConflict as exc:
            raise HTTPException(status_code=409, detail=exc.reason)
        except ClaimConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except RevisionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail=f"expected revision {exc.expected}, session is at {exc.actual}",
            )
        except LayoutBusy:
            raise HTTPException(status_code=409, detail="session busy, retry")
        except RateLimited:
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        except OpError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/sessions/{session_id}/stream")
    async def stream_session(
        session_id: str,
        request: Request,
        client_id: str = Query(..., min_length=1, max_length=100),
        name: Optional[str] = Query(None, max_length=100),
        since_seq: Optional[int] = Query(None),
    ):
        if not is_valid_session_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session_id format")
        _rate_limit_lookup(request)
        try:
            session_manager.get_or_create(session_id)
        except SessionLimitReached:
            raise HTTPException(status_code=503, detail="too many sessions")

        subscription, _member = session_manager.connect(session_id, client_id, name)

        async def event_generator():
            try:
                catch_up = session_manager.catch_up(session_id, since_seq)
                yield f"data: {json.dumps(catch_up)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(subscription.get(), timeout=25.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    try:
                        event = _resolve_stream_event(
                            event, session_manager, session_id
                        )
                    except SessionNotFound:
                        # The session was deleted in the narrow window between
                        # this subscriber's queue overflowing (which may have
                        # drained an already-queued session_deleted event
                        # too, per session_hub.py's drain-on-overflow) and the
                        # resync translation running catch_up against a store
                        # entry that no longer exists. Give the client the
                        # notice it would otherwise have missed.
                        yield f"data: {json.dumps({'type': 'session_deleted', 'deleted_by': None})}\n\n"
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                session_manager.disconnect(session_id, client_id, subscription)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


def _register_export_endpoints(router: APIRouter, service: GraphService) -> None:
    @router.get("/export")
    async def export_graph(request: Request) -> Dict[str, Any]:
        """Export the entire graph (all nodes and edges)."""
        with use_request_authorization(headers=request.headers):
            result = service.export_graph()
        _raise_for_access_denied(result)
        return result


def _register_custom_interface_endpoints(
    router: APIRouter,
    service: GraphService,
    interfaces: List[RestInterfaceConfig],
) -> None:
    """Register config-driven dedicated REST interfaces per node/edge type.

    Each enabled interface gets its own ``GET /{path}`` route that bypasses the
    generic node/edge interface and returns only entities of the configured
    type, narrowed by the configured tag/subtype filters. The routes delegate to
    ``GraphService.list_typed_nodes`` / ``list_typed_edges``, which apply the
    same read authorization and graph-scope narrowing as the generic interface,
    so a dedicated endpoint never exposes more than a generic search would.
    """
    seen_paths: set = set()
    for interface in interfaces:
        if not interface.enabled:
            continue
        if interface.path in seen_paths:
            logger.warning(
                "Duplicate rest_interfaces path '%s' — skipping the later entry",
                interface.path,
            )
            continue

        if interface.entity == "node":
            if not interface.node_type:
                logger.warning(
                    "rest_interfaces entry for path '%s' has entity 'node' but no "
                    "node_type — skipping",
                    interface.path,
                )
                continue
        else:  # edge
            if not interface.edge_type:
                logger.warning(
                    "rest_interfaces entry for path '%s' has entity 'edge' but no "
                    "edge_type — skipping",
                    interface.path,
                )
                continue

        seen_paths.add(interface.path)
        _register_single_custom_interface(router, service, interface)


def _register_single_custom_interface(
    router: APIRouter,
    service: GraphService,
    interface: RestInterfaceConfig,
) -> None:
    """Register one dedicated interface route.

    A dedicated factory (rather than an inline closure in the loop) binds
    ``interface`` per route, avoiding the late-binding pitfall where every
    handler would otherwise close over the loop's final value.
    """
    filters = interface.filters

    if interface.entity == "node":

        @router.get(f"/{interface.path}", name=f"custom_interface_{interface.path}")
        async def custom_node_interface(
            request: Request, include_archived: bool = False
        ) -> Dict[str, Any]:
            with use_request_authorization(headers=request.headers):
                result = service.list_typed_nodes(
                    node_type=interface.node_type,
                    tags_all=filters.tags_all,
                    tags_any=filters.tags_any,
                    subtypes_any=filters.subtypes_any,
                    limit=interface.limit,
                    include_archived=include_archived,
                )
            _raise_for_access_denied(result)
            return result

    else:

        @router.get(f"/{interface.path}", name=f"custom_interface_{interface.path}")
        async def custom_edge_interface(
            request: Request, include_archived: bool = False
        ) -> Dict[str, Any]:
            with use_request_authorization(headers=request.headers):
                result = service.list_typed_edges(
                    edge_type=interface.edge_type,
                    tags_all=filters.tags_all,
                    tags_any=filters.tags_any,
                    limit=interface.limit,
                    include_archived=include_archived,
                )
            _raise_for_access_denied(result)
            return result


# ==================== Router Factory ====================


def create_rest_router(
    service: GraphService,
    prefix: str = "",
    session_manager=None,
    rest_interfaces: Optional[List[RestInterfaceConfig]] = None,
) -> APIRouter:
    """
    Create a FastAPI router with all graph operation endpoints.

    Args:
        service: GraphService instance to use for operations
        prefix: Optional URL prefix for all routes
        session_manager: Optional SessionManager enabling shared-session
            endpoints (/sessions CRUD + ops + stream). When None, those routes
            are not registered.
        rest_interfaces: Optional explicit list of config-driven dedicated REST
            interfaces. When None, they are read from the loaded schema config
            (``config_loader.get_rest_interfaces()``).

    Returns:
        Configured APIRouter
    """
    router = APIRouter(prefix=prefix, tags=["graph"])

    _register_search_endpoints(router, service)
    _register_similarity_endpoints(router, service)
    _register_node_crud_endpoints(router, service)
    _register_edge_crud_endpoints(router, service)
    _register_history_endpoints(router, service)
    _register_metadata_endpoints(router, service)
    _register_views_endpoints(router, service)
    _register_export_endpoints(router, service)
    if session_manager is not None:
        _register_session_endpoints(router, service, session_manager)

    @router.get("/collect/{short_name}")
    async def get_collect_config(
        short_name: str = Path(
            ..., pattern=r"^[a-z0-9][a-z0-9-]{0,98}[a-z0-9]$|^[a-z0-9]$"
        ),
    ) -> Dict[str, Any]:
        """Get Active Knowledge Collection public config by short name.

        The AI prompt is intentionally excluded to prevent exposure of
        operator-configured instructions. The prompt is resolved server-side
        when the chat endpoint receives collection_short_name.
        """
        try:
            result = service.search_graph(
                query="", node_types=["ActiveKnowledgeCollection"], limit=500
            )
            nodes = result.get("nodes", [])
            for node in nodes:
                metadata = node.get("metadata") or {}
                if metadata.get("short_name") == short_name:
                    return {
                        "found": True,
                        "name": node.get("name", ""),
                        "short_name": short_name,
                        "introduction_text": metadata.get("introduction_text", ""),
                        "node_type_permissions": metadata.get(
                            "node_type_permissions", {}
                        ),
                    }
            raise HTTPException(
                status_code=404,
                detail=f"No Active Knowledge Collection found with short_name '{short_name}'",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Register config-driven dedicated interfaces last, after every fixed route
    # (including /collect/{short_name}), so a fixed route always wins if an
    # operator picks a colliding path.
    if rest_interfaces is None:
        rest_interfaces = get_rest_interfaces()
    _register_custom_interface_endpoints(router, service, rest_interfaces)

    return router
