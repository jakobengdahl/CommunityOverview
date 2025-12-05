"""
MCP Server for Community Knowledge Graph
Exposes tools for graph operations via MCP
"""

from typing import List, Optional, Dict, Any
import os
import tempfile
import shutil
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from graph_storage import GraphStorage
from models import (
    Node, Edge, NodeType, RelationshipType,
    SimilarNode, AddNodesResult, DeleteNodesResult
)
from document_processor import DocumentProcessor

# Initialize MCP server
mcp = FastMCP("community-knowledge-graph")

# Initialize graph storage
graph = GraphStorage("graph.json")


# --- Chat Endpoint (Backend Integration) ---

from chat_logic import ChatProcessor

# Map tools to functions for the chat processor
TOOLS_MAP = {}

def register_tool(name, func):
    TOOLS_MAP[name] = func
    return func

# Wrapper to register tools both in MCP and locally for ChatProcessor
def tool_wrapper(func):
    mcp.tool()(func)
    register_tool(func.__name__, func)
    return func

# Initialize ChatProcessor (lazy load to avoid circular deps if any)
chat_processor = None

def get_chat_processor():
    global chat_processor
    if not chat_processor:
        chat_processor = ChatProcessor(TOOLS_MAP)
    return chat_processor

@mcp.custom_route("/chat", methods=["POST"])
async def chat_endpoint(request: Request):
    """
    Endpoint for the frontend to chat with Claude via the backend.
    """
    try:
        body = await request.json()
        messages = body.get("messages", [])

        if not messages:
            return JSONResponse({"error": "No messages provided"}, status_code=400)

        processor = get_chat_processor()
        result = processor.process_message(messages)

        return JSONResponse(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/upload", methods=["POST"])
async def upload_endpoint(request: Request):
    """
    Endpoint to upload files and extract text
    """
    try:
        form = await request.form()
        file = form.get("file")

        if not file:
            return JSONResponse({"error": "No file provided"}, status_code=400)

        filename = file.filename

        # Save temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name

        try:
            # Extract text
            text = DocumentProcessor.extract_text(tmp_path)

            return JSONResponse({
                "success": True,
                "filename": filename,
                "text": text,
                "message": f"Successfully extracted {len(text)} characters"
            })

        finally:
            # Cleanup
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/execute_tool", methods=["POST"])
async def execute_tool_endpoint(request: Request):
    """
    Endpoint for the frontend to execute tools directly
    """
    try:
        body = await request.json()
        tool_name = body.get("tool_name")
        arguments = body.get("arguments", {})

        if not tool_name:
            return JSONResponse({"error": "No tool_name provided"}, status_code=400)

        if tool_name not in TOOLS_MAP:
            return JSONResponse({"error": f"Tool {tool_name} not found"}, status_code=404)

        func = TOOLS_MAP[tool_name]
        result = func(**arguments)

        return JSONResponse(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


# --- MCP Tools ---

@tool_wrapper
def search_graph(
    query: str,
    node_types: Optional[List[str]] = None,
    communities: Optional[List[str]] = None,
    limit: int = 50
) -> Dict[str, Any]:
    """
    Search for nodes in the graph based on text query

    Args:
        query: Search text (matches against name, description, summary)
        node_types: List of node types to filter on (Actor, Initiative, etc.)
        communities: List of communities to filter on
        limit: Max number of results (default 50)

    Returns:
        Dict with matching nodes
    """
    # Convert node_types to NodeType enum
    type_filters = None
    if node_types:
        type_filters = [NodeType(t) for t in node_types]

    results = graph.search_nodes(
        query=query,
        node_types=type_filters,
        communities=communities,
        limit=limit
    )

    return {
        "nodes": [node.model_dump() for node in results],
        "total": len(results),
        "query": query,
        "filters": {
            "node_types": node_types,
            "communities": communities
        }
    }


@tool_wrapper
def get_node_details(node_id: str) -> Dict[str, Any]:
    """
    Get complete information about a specific node

    Args:
        node_id: ID of the node

    Returns:
        Dict with node data or error
    """
    node = graph.get_node(node_id)

    if not node:
        return {
            "success": False,
            "error": f"Node with ID {node_id} not found"
        }

    return {
        "success": True,
        "node": node.model_dump()
    }


@tool_wrapper
def get_related_nodes(
    node_id: str,
    relationship_types: Optional[List[str]] = None,
    depth: int = 1
) -> Dict[str, Any]:
    """
    Get nodes connected to the given node

    Args:
        node_id: ID of the starting node
        relationship_types: List of relationship types to filter on
        depth: How many hops from the starting node (default 1)

    Returns:
        Dict with nodes and edges
    """
    # Convert relationship_types to enum
    rel_filters = None
    if relationship_types:
        rel_filters = [RelationshipType(r) for r in relationship_types]

    result = graph.get_related_nodes(
        node_id=node_id,
        relationship_types=rel_filters,
        depth=depth
    )

    return {
        "nodes": [node.model_dump() for node in result['nodes']],
        "edges": [edge.model_dump() for edge in result['edges']],
        "total_nodes": len(result['nodes']),
        "total_edges": len(result['edges']),
        "depth": depth
    }


@tool_wrapper
def find_similar_nodes(
    name: str,
    node_type: Optional[str] = None,
    threshold: float = 0.7,
    limit: int = 5
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
    type_filter = NodeType(node_type) if node_type else None

    similar = graph.find_similar_nodes(
        name=name,
        node_type=type_filter,
        threshold=threshold,
        limit=limit
    )

    return {
        "similar_nodes": [
            {
                "node": s.node.model_dump(),
                "similarity_score": s.similarity_score,
                "match_reason": s.match_reason
            }
            for s in similar
        ],
        "total": len(similar),
        "search_name": name
    }


@tool_wrapper
def add_nodes(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Add new nodes and edges to the graph

    Args:
        nodes: List of node objects to add
        edges: List of edge objects to add

    Returns:
        Dict with result (added_node_ids, added_edge_ids, success, message)
    """
    # Convert dicts to Node and Edge objects
    try:
        node_objects = [Node(**n) for n in nodes]
        edge_objects = [Edge(**e) for e in edges]
    except Exception as e:
        return {
            "success": False,
            "message": f"Error validating input: {str(e)}",
            "added_node_ids": [],
            "added_edge_ids": []
        }

    result = graph.add_nodes(node_objects, edge_objects)
    return result.model_dump()


@tool_wrapper
def update_node(node_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update an existing node

    Args:
        node_id: ID of the node to update
        updates: Dict with fields to update (name, description, summary, communities, metadata)

    Returns:
        Dict with updated node or error
    """
    updated_node = graph.update_node(node_id, updates)

    if not updated_node:
        return {
            "success": False,
            "error": f"Node with ID {node_id} not found"
        }

    return {
        "success": True,
        "node": updated_node.model_dump()
    }


@tool_wrapper
def delete_nodes(
    node_ids: List[str],
    confirmed: bool = False
) -> Dict[str, Any]:
    """
    Delete nodes from the graph (max 10 at a time)

    SECURITY: Requires confirmed=True and max 10 nodes per call

    Args:
        node_ids: List of node IDs to delete
        confirmed: Must be True to execute deletion

    Returns:
        Dict with result (deleted_node_ids, affected_edge_ids, success, message)
    """
    result = graph.delete_nodes(node_ids, confirmed)
    return result.model_dump()


@tool_wrapper
def get_graph_stats(communities: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Get statistics for the graph

    Args:
        communities: Optional list of communities to filter on

    Returns:
        Dict with statistics (total_nodes, total_edges, nodes_by_type, nodes_by_community)
    """
    stats = graph.get_stats(communities)
    return stats.model_dump()


@tool_wrapper
def list_node_types() -> Dict[str, Any]:
    """
    List all allowed node types according to the metamodel

    Returns:
        Dict with node types and their color coding
    """
    from models import NODE_COLORS

    return {
        "node_types": [
            {
                "type": nt.value,
                "color": NODE_COLORS[nt],
                "description": _get_node_type_description(nt)
            }
            for nt in NodeType
        ]
    }


@tool_wrapper
def list_relationship_types() -> Dict[str, Any]:
    """
    List all allowed relationship types

    Returns:
        Dict with relationship types
    """
    return {
        "relationship_types": [
            {
                "type": rt.value,
                "description": _get_relationship_description(rt)
            }
            for rt in RelationshipType
        ]
    }


@tool_wrapper
def save_visualization_metadata(name: str) -> Dict[str, Any]:
    """
    Signal intent to save the current visualization view.

    This tool does NOT save the view data itself (positions, etc.) because
    the backend does not know the client state. Instead, it acts as a signal
    for the frontend to capture the state and save it.

    Args:
        name: Name of the view to save

    Returns:
        A signal object that the frontend will intercept.
    """
    return {
        "action": "save_visualization",
        "name": name,
        "message": f"Ready to save view '{name}'. Client will capture state."
    }


@tool_wrapper
def get_visualization(name: str) -> Dict[str, Any]:
    """
    Get a saved visualization view by name.

    Args:
        name: Name of the view

    Returns:
        The view data (nodes, positions, etc.)
    """
    # Search for a node of type VISUALIZATION_VIEW with the given name
    results = graph.search_nodes(query=name, node_types=[NodeType.VISUALIZATION_VIEW], limit=1)

    if not results:
        return {
            "success": False,
            "error": f"View '{name}' not found."
        }

    view_node = results[0]
    return {
        "success": True,
        "view": view_node.model_dump(),
        "action": "load_visualization"  # Signal for frontend
    }


def _get_node_type_description(node_type: NodeType) -> str:
    """Helper for node type descriptions"""
    descriptions = {
        NodeType.ACTOR: "Government agencies, organizations",
        NodeType.COMMUNITY: "Communities (eSam, Myndigheter, etc.)",
        NodeType.INITIATIVE: "Projects, collaborative activities",
        NodeType.CAPABILITY: "Capabilities (procurement, IT development, etc.)",
        NodeType.RESOURCE: "Outputs (reports, software, etc.)",
        NodeType.LEGISLATION: "Laws and directives (NIS2, GDPR, etc.)",
        NodeType.THEME: "Themes (AI, data strategies, etc.)",
        NodeType.VISUALIZATION_VIEW: "Predefined views for navigation"
    }
    return descriptions.get(node_type, "")


def _get_relationship_description(rel_type: RelationshipType) -> str:
    """Helper for relationship type descriptions"""
    descriptions = {
        RelationshipType.BELONGS_TO: "Belongs to (actor belongs to community, initiative belongs to actor)",
        RelationshipType.IMPLEMENTS: "Implements (initiative implements legislation)",
        RelationshipType.PRODUCES: "Produces (initiative produces resource/capability)",
        RelationshipType.GOVERNED_BY: "Governed by (initiative governed by legislation)",
        RelationshipType.RELATES_TO: "Relates to (general connection)",
        RelationshipType.PART_OF: "Part of (component is part of larger whole)"
    }
    return descriptions.get(rel_type, "")


# Instructions for LLM when using MCP
SYSTEM_PROMPT = """
You are a helpful assistant for the Community Knowledge Graph system.

METAMODEL:
- Actor (blue): Government agencies, organizations
- Community (purple): eSam, Myndigheter, Officiell Statistik
- Initiative (green): Projects, collaborative activities
- Capability (orange): Capabilities
- Resource (yellow): Reports, software
- Legislation (red): NIS2, GDPR
- Theme (teal): AI, data strategies
- VisualizationView (gray): Predefined views

SECURITY RULES:
1. ALWAYS warn if the user tries to store personal data
2. For deletion: Max 10 nodes, require double confirmation
3. Always filter based on the user's active communities

WORKFLOW FOR ADDING NODES:
1. Run find_similar_nodes() to find duplicates
2. Present proposal + similar existing nodes
3. Wait for user approval
4. Run add_nodes() only after approval

WORKFLOW FOR DOCUMENT UPLOAD:
1. Extract text from document
2. Identify potential nodes according to metamodel
3. Run find_similar_nodes() for each
4. Present proposal + duplicates
5. Let user choose what to add
6. Automatically link to user's active communities

Always be clear about what you're doing and ask for confirmation for important operations.
"""


if __name__ == "__main__":
    # Start MCP server
    print("Starting Community Knowledge Graph MCP Server...")
    print(f"Loaded graph with {len(graph.nodes)} nodes and {len(graph.edges)} edges")
    print(SYSTEM_PROMPT)
    mcp.run()
