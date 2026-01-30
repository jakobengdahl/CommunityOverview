"""
Metamodel for Community Knowledge Graph
Defines all node types, edge types, and validation rules

This module is part of graph_core - the core graph storage layer.
It contains data models without any MCP, API, or external service dependencies.

Node and relationship types are loaded from the schema configuration file.
See backend/config_loader.py for configuration loading.
"""

from enum import Enum
from typing import Optional, List, Dict, Any, Union
from datetime import datetime
from pydantic import BaseModel, Field, validator
import uuid


# Lazy import to avoid circular dependencies
_config_loader = None


def _get_config_loader():
    """Lazy load config_loader to avoid circular imports."""
    global _config_loader
    if _config_loader is None:
        from backend import config_loader
        _config_loader = config_loader
    return _config_loader


def get_node_type_names() -> List[str]:
    """Get list of valid node type names from config."""
    return _get_config_loader().get_node_type_names()


def get_relationship_type_names() -> List[str]:
    """Get list of valid relationship type names from config."""
    return _get_config_loader().get_relationship_type_names()


def is_valid_node_type(type_name: str) -> bool:
    """Check if a node type name is valid according to config."""
    return type_name in get_node_type_names()


def is_valid_relationship_type(type_name: str) -> bool:
    """Check if a relationship type name is valid according to config."""
    return type_name in get_relationship_type_names()


def get_node_color(node_type: str) -> str:
    """Get the color for a node type from config."""
    return _get_config_loader().get_node_color(node_type)


# Legacy Enum classes for backward compatibility
# New code should use the string-based types with validation functions above
class NodeType(str, Enum):
    """
    Legacy node types enum for backward compatibility.

    Note: New code should use string types and validate with is_valid_node_type().
    This enum is kept for compatibility with existing code.
    """
    ACTOR = "Actor"
    COMMUNITY = "Community"
    INITIATIVE = "Initiative"
    CAPABILITY = "Capability"
    RESOURCE = "Resource"
    LEGISLATION = "Legislation"
    THEME = "Theme"
    SAVED_VIEW = "SavedView"
    # Legacy support (to be removed)
    VISUALIZATION_VIEW = "VisualizationView"

    @classmethod
    def from_string(cls, value: str) -> Union['NodeType', str]:
        """
        Convert a string to NodeType if it's a known type, otherwise return the string.
        This allows handling of dynamic types defined in config.
        """
        try:
            return cls(value)
        except ValueError:
            # Not a known enum value, but might be valid in config
            if is_valid_node_type(value):
                return value
            raise ValueError(f"Invalid node type: {value}")


class RelationshipType(str, Enum):
    """
    Legacy relationship types enum for backward compatibility.

    Note: New code should use string types and validate with is_valid_relationship_type().
    """
    BELONGS_TO = "BELONGS_TO"
    IMPLEMENTS = "IMPLEMENTS"
    PRODUCES = "PRODUCES"
    GOVERNED_BY = "GOVERNED_BY"
    RELATES_TO = "RELATES_TO"
    PART_OF = "PART_OF"

    @classmethod
    def from_string(cls, value: str) -> Union['RelationshipType', str]:
        """
        Convert a string to RelationshipType if known, otherwise return the string.
        """
        try:
            return cls(value)
        except ValueError:
            if is_valid_relationship_type(value):
                return value
            raise ValueError(f"Invalid relationship type: {value}")


# Dynamic color lookup function
def NODE_COLORS_LOOKUP(node_type: Union[NodeType, str]) -> str:
    """Get color for a node type (works with both enum and string types)."""
    type_str = node_type.value if isinstance(node_type, NodeType) else str(node_type)
    return get_node_color(type_str)


# Legacy color dict for backward compatibility (updated at import time)
NODE_COLORS = {
    NodeType.ACTOR: "#3B82F6",  # blue
    NodeType.COMMUNITY: "#A855F7",  # purple
    NodeType.INITIATIVE: "#10B981",  # green
    NodeType.CAPABILITY: "#F97316",  # orange
    NodeType.RESOURCE: "#FBBF24",  # yellow
    NodeType.LEGISLATION: "#EF4444",  # red
    NodeType.THEME: "#14B8A6",  # teal
    NodeType.SAVED_VIEW: "#6B7280",  # gray
    NodeType.VISUALIZATION_VIEW: "#6B7280",  # gray (legacy)
}


class Node(BaseModel):
    """Base model for a node in the graph"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: Union[NodeType, str]  # Accept both enum and string types
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

    @validator('type', pre=True)
    def validate_type(cls, v):
        """Validate and normalize node type."""
        if isinstance(v, NodeType):
            return v
        if isinstance(v, str):
            # Try to convert to enum for backward compatibility
            try:
                return NodeType(v)
            except ValueError:
                # Check if it's a valid config-defined type
                if is_valid_node_type(v):
                    return v
                raise ValueError(f"Invalid node type: {v}")
        raise ValueError(f"Node type must be string or NodeType, got {type(v)}")

    @property
    def type_str(self) -> str:
        """Get the type as a string (works with both enum and string types)."""
        return self.type.value if isinstance(self.type, NodeType) else str(self.type)

    def to_dict(self) -> dict:
        """Convert to dict for JSON storage"""
        data = self.model_dump()
        # Ensure type is stored as string
        data['type'] = self.type_str
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
        """Return color for visualization from config"""
        return get_node_color(self.type_str)


class Edge(BaseModel):
    """Model for an edge (relationship) between nodes"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # Node ID
    target: str  # Node ID
    type: Union[RelationshipType, str]  # Accept both enum and string types
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    @validator('type', pre=True)
    def validate_type(cls, v):
        """Validate and normalize relationship type."""
        if isinstance(v, RelationshipType):
            return v
        if isinstance(v, str):
            # Try to convert to enum for backward compatibility
            try:
                return RelationshipType(v)
            except ValueError:
                # Check if it's a valid config-defined type
                if is_valid_relationship_type(v):
                    return v
                raise ValueError(f"Invalid relationship type: {v}")
        raise ValueError(f"Relationship type must be string or RelationshipType, got {type(v)}")

    @property
    def type_str(self) -> str:
        """Get the type as a string (works with both enum and string types)."""
        return self.type.value if isinstance(self.type, RelationshipType) else str(self.type)

    def to_dict(self) -> dict:
        data = self.model_dump()
        # Ensure type is stored as string
        data['type'] = self.type_str
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
