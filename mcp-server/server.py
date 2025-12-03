"""
MCP Server för Community Knowledge Graph
Exponerar tools för graf-operationer via MCP
"""

from typing import List, Optional, Dict, Any
from mcp.server.fastmcp import FastMCP
from graph_storage import GraphStorage
from models import (
    Node, Edge, NodeType, RelationshipType,
    SimilarNode, AddNodesResult, DeleteNodesResult
)

# Initiera MCP server
mcp = FastMCP("community-knowledge-graph")

# Initiera graf-lagring
graph = GraphStorage("graph.json")


@mcp.tool()
def search_graph(
    query: str,
    node_types: Optional[List[str]] = None,
    communities: Optional[List[str]] = None,
    limit: int = 50
) -> Dict[str, Any]:
    """
    Söker efter noder i grafen baserat på text-query

    Args:
        query: Söktext (matchar mot name, description, summary)
        node_types: Lista av node-typer att filtrera på (Actor, Initiative, etc.)
        communities: Lista av communities att filtrera på
        limit: Max antal resultat (default 50)

    Returns:
        Dict med matching nodes
    """
    # Konvertera node_types till NodeType enum
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


@mcp.tool()
def get_node_details(node_id: str) -> Dict[str, Any]:
    """
    Hämtar fullständig information om en specifik nod

    Args:
        node_id: ID för noden

    Returns:
        Dict med node-data eller error
    """
    node = graph.get_node(node_id)

    if not node:
        return {
            "success": False,
            "error": f"Nod med ID {node_id} hittades inte"
        }

    return {
        "success": True,
        "node": node.model_dump()
    }


@mcp.tool()
def get_related_nodes(
    node_id: str,
    relationship_types: Optional[List[str]] = None,
    depth: int = 1
) -> Dict[str, Any]:
    """
    Hämtar noder kopplade till given nod

    Args:
        node_id: ID för start-noden
        relationship_types: Lista av relationship-typer att filtrera på
        depth: Hur många hopp från start-noden (default 1)

    Returns:
        Dict med nodes och edges
    """
    # Konvertera relationship_types till enum
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


@mcp.tool()
def find_similar_nodes(
    name: str,
    node_type: Optional[str] = None,
    threshold: float = 0.7,
    limit: int = 5
) -> Dict[str, Any]:
    """
    Hittar liknande noder baserat på namn (för dublettkontroll)

    Args:
        name: Namnet att söka efter liknande noder för
        node_type: Optional node-typ att filtrera på
        threshold: Similarity threshold 0.0-1.0 (default 0.7)
        limit: Max antal resultat (default 5)

    Returns:
        Dict med liknande noder och similarity scores
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


@mcp.tool()
def add_nodes(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Lägger till nya noder och edges till grafen

    Args:
        nodes: Lista av nod-objekt att lägga till
        edges: Lista av edge-objekt att lägga till

    Returns:
        Dict med resultat (added_node_ids, added_edge_ids, success, message)
    """
    # Konvertera dicts till Node och Edge objekt
    try:
        node_objects = [Node(**n) for n in nodes]
        edge_objects = [Edge(**e) for e in edges]
    except Exception as e:
        return {
            "success": False,
            "message": f"Fel vid validering av input: {str(e)}",
            "added_node_ids": [],
            "added_edge_ids": []
        }

    result = graph.add_nodes(node_objects, edge_objects)
    return result.model_dump()


@mcp.tool()
def update_node(node_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Uppdaterar en befintlig nod

    Args:
        node_id: ID för noden att uppdatera
        updates: Dict med fält att uppdatera (name, description, summary, communities, metadata)

    Returns:
        Dict med uppdaterad node eller error
    """
    updated_node = graph.update_node(node_id, updates)

    if not updated_node:
        return {
            "success": False,
            "error": f"Nod med ID {node_id} hittades inte"
        }

    return {
        "success": True,
        "node": updated_node.model_dump()
    }


@mcp.tool()
def delete_nodes(
    node_ids: List[str],
    confirmed: bool = False
) -> Dict[str, Any]:
    """
    Tar bort noder från grafen (max 10 åt gången)

    SÄKERHET: Kräver confirmed=True och max 10 noder per anrop

    Args:
        node_ids: Lista av node IDs att ta bort
        confirmed: Måste vara True för att genomföra deletion

    Returns:
        Dict med resultat (deleted_node_ids, affected_edge_ids, success, message)
    """
    result = graph.delete_nodes(node_ids, confirmed)
    return result.model_dump()


@mcp.tool()
def get_graph_stats(communities: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Hämtar statistik för grafen

    Args:
        communities: Optional lista av communities att filtrera på

    Returns:
        Dict med statistik (total_nodes, total_edges, nodes_by_type, nodes_by_community)
    """
    stats = graph.get_stats(communities)
    return stats.model_dump()


@mcp.tool()
def list_node_types() -> Dict[str, Any]:
    """
    Listar alla tillåtna node-typer enligt metamodellen

    Returns:
        Dict med node-typer och deras färgkodning
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


@mcp.tool()
def list_relationship_types() -> Dict[str, Any]:
    """
    Listar alla tillåtna relationship-typer

    Returns:
        Dict med relationship-typer
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


def _get_node_type_description(node_type: NodeType) -> str:
    """Helper för beskrivningar av node-typer"""
    descriptions = {
        NodeType.ACTOR: "Myndigheter, organisationer",
        NodeType.COMMUNITY: "Communities (eSam, Myndigheter, etc.)",
        NodeType.INITIATIVE: "Projekt, gruppverksamheter",
        NodeType.CAPABILITY: "Förmågor (upphandling, IT-utveckling, etc.)",
        NodeType.RESOURCE: "Resultat (rapporter, mjukvara, etc.)",
        NodeType.LEGISLATION: "Lagar och direktiv (NIS2, GDPR, etc.)",
        NodeType.THEME: "Teman (AI, datastrategier, etc.)",
        NodeType.VISUALIZATION_VIEW: "Färdiga vyer för navigation"
    }
    return descriptions.get(node_type, "")


def _get_relationship_description(rel_type: RelationshipType) -> str:
    """Helper för beskrivningar av relationship-typer"""
    descriptions = {
        RelationshipType.BELONGS_TO: "Tillhör (actor tillhör community, initiative tillhör actor)",
        RelationshipType.IMPLEMENTS: "Implementerar (initiative implementerar legislation)",
        RelationshipType.PRODUCES: "Producerar (initiative producerar resource/capability)",
        RelationshipType.GOVERNED_BY: "Styrs av (initiativ styrs av legislation)",
        RelationshipType.RELATES_TO: "Relaterar till (generell koppling)",
        RelationshipType.PART_OF: "Del av (komponent är del av större helhet)"
    }
    return descriptions.get(rel_type, "")


# Instruktioner för LLM när den använder MCP
SYSTEM_PROMPT = """
Du är en hjälpsam assistent för Community Knowledge Graph-systemet.

METAMODELL:
- Actor (blue): Myndigheter, organisationer
- Community (purple): eSam, Myndigheter, Officiell Statistik
- Initiative (green): Projekt, gruppverksamheter
- Capability (orange): Förmågor
- Resource (yellow): Rapporter, mjukvara
- Legislation (red): NIS2, GDPR
- Theme (teal): AI, datastrategier
- VisualizationView (gray): Färdiga vyer

SÄKERHETSREGLER:
1. Varna ALLTID om användaren försöker lagra personuppgifter
2. Vid deletion: Max 10 noder, kräv dubbelkonfirmation
3. Filtrera alltid baserat på användarens aktiva communities

ARBETSFLÖDE VID TILLÄGG AV NOD:
1. Kör find_similar_nodes() för att hitta dubletter
2. Presentera förslag + liknande befintliga noder
3. Vänta på användargodkännande
4. Kör add_nodes() endast efter godkännande

ARBETSFLÖDE VID DOKUMENTUPPLADDNING:
1. Extrahera text från dokument
2. Identifiera potentiella noder enligt metamodell
3. Kör find_similar_nodes() för varje
4. Presentera förslag + dubletter
5. Låt användare välja vad som ska läggas till
6. Länka automatiskt till användarens aktiva communities

Var alltid tydlig med vad du gör och be om bekräftelse vid viktiga operationer.
"""


if __name__ == "__main__":
    # Starta MCP server
    print("Startar Community Knowledge Graph MCP Server...")
    print(f"Laddade graf med {len(graph.nodes)} noder och {len(graph.edges)} edges")
    print(SYSTEM_PROMPT)
    mcp.run()
