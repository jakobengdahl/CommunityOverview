"""
Metamodell för Community Knowledge Graph
Definierar alla node-typer, edge-typer och valideringsregler
"""

from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
import uuid


class NodeType(str, Enum):
    """Tillåtna node-typer enligt metamodellen"""
    ACTOR = "Actor"
    COMMUNITY = "Community"
    INITIATIVE = "Initiative"
    CAPABILITY = "Capability"
    RESOURCE = "Resource"
    LEGISLATION = "Legislation"
    THEME = "Theme"
    VISUALIZATION_VIEW = "VisualizationView"


class RelationshipType(str, Enum):
    """Tillåtna relationship-typer"""
    BELONGS_TO = "BELONGS_TO"
    IMPLEMENTS = "IMPLEMENTS"
    PRODUCES = "PRODUCES"
    GOVERNED_BY = "GOVERNED_BY"
    RELATES_TO = "RELATES_TO"
    PART_OF = "PART_OF"


# Färgkodning för visualisering
NODE_COLORS = {
    NodeType.ACTOR: "#3B82F6",  # blue
    NodeType.COMMUNITY: "#A855F7",  # purple
    NodeType.INITIATIVE: "#10B981",  # green
    NodeType.CAPABILITY: "#F97316",  # orange
    NodeType.RESOURCE: "#FBBF24",  # yellow
    NodeType.LEGISLATION: "#EF4444",  # red
    NodeType.THEME: "#14B8A6",  # teal
    NodeType.VISUALIZATION_VIEW: "#6B7280",  # gray
}


class Node(BaseModel):
    """Bas-modell för en nod i grafen"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: NodeType
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    summary: str = Field(default="", max_length=100)  # För visualisering
    communities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None  # För future vector search
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    def to_dict(self) -> dict:
        """Konvertera till dict för JSON-lagring"""
        data = self.model_dump()
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'Node':
        """Skapa från dict (JSON)"""
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if isinstance(data.get('updated_at'), str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        return cls(**data)

    def get_color(self) -> str:
        """Returnera färg för visualisering"""
        return NODE_COLORS.get(self.type, "#9CA3AF")


class Edge(BaseModel):
    """Modell för en edge (relationship) mellan noder"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # Node ID
    target: str  # Node ID
    type: RelationshipType
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    def to_dict(self) -> dict:
        data = self.model_dump()
        data['created_at'] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'Edge':
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        return cls(**data)


class SimilarNode(BaseModel):
    """Modell för resultat från similarity search"""
    node: Node
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    match_reason: str = ""


class GraphStats(BaseModel):
    """Statistik för grafen"""
    total_nodes: int
    total_edges: int
    nodes_by_type: Dict[str, int]
    nodes_by_community: Dict[str, int]
    last_updated: datetime

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class ProposedNodesResult(BaseModel):
    """Resultat från propose_nodes_from_text"""
    proposed_nodes: List[Node]
    proposed_edges: List[Edge]
    similar_existing: List[SimilarNode]
    communities: List[str]  # Communities som föreslås kopplas


class AddNodesResult(BaseModel):
    """Resultat från add_nodes operation"""
    added_node_ids: List[str]
    added_edge_ids: List[str]
    success: bool
    message: str = ""


class DeleteNodesResult(BaseModel):
    """Resultat från delete_nodes operation"""
    deleted_node_ids: List[str]
    affected_edge_ids: List[str]  # Edges som också togs bort
    success: bool
    message: str = ""
