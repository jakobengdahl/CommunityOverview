"""
Metamodel for Community Knowledge Graph
Defines all node types, edge types, and validation rules
"""

from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
import uuid


class NodeType(str, Enum):
    """Allowed node types according to the metamodel"""
    ACTOR = "Actor"
    COMMUNITY = "Community"
    INITIATIVE = "Initiative"
    CAPABILITY = "Capability"
    RESOURCE = "Resource"
    LEGISLATION = "Legislation"
    THEME = "Theme"
    VISUALIZATION_VIEW = "VisualizationView"


class RelationshipType(str, Enum):
    """Allowed relationship types"""
    BELONGS_TO = "BELONGS_TO"
    IMPLEMENTS = "IMPLEMENTS"
    PRODUCES = "PRODUCES"
    GOVERNED_BY = "GOVERNED_BY"
    RELATES_TO = "RELATES_TO"
    PART_OF = "PART_OF"


# Color coding for visualization
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
    """Base model for a node in the graph"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: NodeType
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    summary: str = Field(default="", max_length=100)  # For visualization
    communities: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)  # Searchable tags for categorization
    metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None  # For future vector search
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    def to_dict(self) -> dict:
        """Convert to dict for JSON storage"""
        data = self.model_dump()
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'Node':
        """Create from dict (JSON)"""
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if isinstance(data.get('updated_at'), str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        return cls(**data)

    def get_color(self) -> str:
        """Return color for visualization"""
        return NODE_COLORS.get(self.type, "#9CA3AF")


class Edge(BaseModel):
    """Model for an edge (relationship) between nodes"""
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
    """Model for similarity search results"""
    node: Node
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    match_reason: str = ""


class GraphStats(BaseModel):
    """Statistics for the graph"""
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
    """Result from propose_nodes_from_text"""
    proposed_nodes: List[Node]
    proposed_edges: List[Edge]
    similar_existing: List[SimilarNode]
    communities: List[str]  # Communities to be linked


class AddNodesResult(BaseModel):
    """Result from add_nodes operation"""
    added_node_ids: List[str]
    added_edge_ids: List[str]
    success: bool
    message: str = ""


class DeleteNodesResult(BaseModel):
    """Result from delete_nodes operation"""
    deleted_node_ids: List[str]
    affected_edge_ids: List[str]  # Edges that were also removed
    success: bool
    message: str = ""
