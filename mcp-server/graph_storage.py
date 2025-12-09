"""
Graph storage with NetworkX and JSON persistence
Handles all CRUD operations on the graph
"""

import json
import os
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime
import networkx as nx
from pathlib import Path
import Levenshtein

from models import (
    Node, Edge, NodeType, RelationshipType,
    SimilarNode, GraphStats, AddNodesResult, DeleteNodesResult
)
from vector_store import VectorStore


class GraphStorage:
    """Manages graph storage with NetworkX + JSON persistence"""

    def __init__(self, json_path: str = "graph.json"):
        self.json_path = Path(json_path)
        self.vector_store = VectorStore(storage_path="embeddings.pkl")
        self.graph = nx.MultiDiGraph()  # MultiDiGraph allows multiple edges between same nodes
        self.nodes: Dict[str, Node] = {}  # node_id -> Node
        self.edges: Dict[str, Edge] = {}  # edge_id -> Edge
        self.load()

    def load(self) -> None:
        """Load graph from JSON file"""
        if not self.json_path.exists():
            print(f"No graph file found at {self.json_path}, creating new empty graph")
            self.save()
            return

        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Load nodes
            for node_data in data.get('nodes', []):
                node = Node.from_dict(node_data)
                self.nodes[node.id] = node
                self.graph.add_node(node.id, data=node)

            # Load edges
            for edge_data in data.get('edges', []):
                edge = Edge.from_dict(edge_data)
                self.edges[edge.id] = edge
                self.graph.add_edge(
                    edge.source,
                    edge.target,
                    key=edge.id,
                    data=edge
                )

            print(f"Loaded {len(self.nodes)} nodes and {len(self.edges)} edges from {self.json_path}")

        except Exception as e:
            print(f"Error loading graph: {e}")
            raise

    def save(self) -> None:
        """Save graph to JSON file"""
        data = {
            'nodes': [node.to_dict() for node in self.nodes.values()],
            'edges': [edge.to_dict() for edge in self.edges.values()],
            'metadata': {
                'version': '1.0',
                'last_updated': datetime.utcnow().isoformat()
            }
        }

        # Create directory if it doesn't exist
        self.json_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"Saved {len(self.nodes)} nodes and {len(self.edges)} edges to {self.json_path}")

    def search_nodes(
        self,
        query: str,
        node_types: Optional[List[NodeType]] = None,
        communities: Optional[List[str]] = None,
        limit: int = 50
    ) -> List[Node]:
        """
        Search nodes based on text query
        Matches against name, description, summary
        """
        query_lower = query.lower()
        results = []

        for node in self.nodes.values():
            # Filter by node type
            if node_types and node.type not in node_types:
                continue

            # Filter by communities
            if communities:
                if not any(comm in node.communities for comm in communities):
                    continue

            # Text matching
            searchable_text = f"{node.name} {node.description} {node.summary}".lower()
            if query_lower in searchable_text:
                results.append(node)

            if len(results) >= limit:
                break

        return results

    def get_node(self, node_id: str) -> Optional[Node]:
        """Get a specific node"""
        return self.nodes.get(node_id)

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[RelationshipType]] = None,
        depth: int = 1
    ) -> Dict[str, any]:
        """
        Get nodes connected to the given node
        Returns both nodes and edges
        """
        if node_id not in self.nodes:
            return {'nodes': [], 'edges': []}

        visited_nodes = set([node_id])
        visited_edges = set()
        current_layer = {node_id}

        for _ in range(depth):
            next_layer = set()

            for curr_id in current_layer:
                # Outgoing edges
                for _, target, edge_id, edge_data in self.graph.out_edges(curr_id, keys=True, data=True):
                    edge = edge_data['data']
                    if relationship_types and edge.type not in relationship_types:
                        continue
                    visited_edges.add(edge_id)
                    if target not in visited_nodes:
                        visited_nodes.add(target)
                        next_layer.add(target)

                # Incoming edges
                for source, _, edge_id, edge_data in self.graph.in_edges(curr_id, keys=True, data=True):
                    edge = edge_data['data']
                    if relationship_types and edge.type not in relationship_types:
                        continue
                    visited_edges.add(edge_id)
                    if source not in visited_nodes:
                        visited_nodes.add(source)
                        next_layer.add(source)

            current_layer = next_layer

        return {
            'nodes': [self.nodes[nid] for nid in visited_nodes if nid in self.nodes],
            'edges': [self.edges[eid] for eid in visited_edges if eid in self.edges]
        }

    def find_similar_nodes(
        self,
        name: str,
        node_type: Optional[NodeType] = None,
        threshold: float = 0.7,
        limit: int = 5
    ) -> List[SimilarNode]:
        """
        Find similar nodes based on Levenshtein distance AND vector embeddings
        Used for duplicate detection
        """
        results = []
        seen_node_ids = set()

        # 1. Levenshtein Search (Exact string matching)
        name_lower = name.lower()

        for node in self.nodes.values():
            # Filter by type if specified
            if node_type and node.type != node_type:
                continue

            # Calculate similarity with Levenshtein
            node_name_lower = node.name.lower()
            distance = Levenshtein.distance(name_lower, node_name_lower)
            max_len = max(len(name_lower), len(node_name_lower))

            if max_len == 0:
                similarity = 1.0
            else:
                similarity = 1.0 - (distance / max_len)

            if similarity >= threshold:
                results.append(SimilarNode(
                    node=node,
                    similarity_score=round(similarity, 2),
                    match_reason=f"Name similarity: {int(similarity * 100)}%"
                ))
                seen_node_ids.add(node.id)

        # 2. Vector Search (Semantic similarity)
        # Lower threshold for vector search to catch semantic matches that might have different names
        vector_threshold = max(0.4, threshold - 0.2)
        vector_results = self.vector_store.search(
            query_text=name,
            limit=limit,
            threshold=vector_threshold
        )

        for node_id, score in vector_results:
            if node_id in seen_node_ids:
                continue

            node = self.nodes.get(node_id)
            if not node:
                continue

            if node_type and node.type != node_type:
                continue

            results.append(SimilarNode(
                node=node,
                similarity_score=round(score, 2),
                match_reason=f"Semantic similarity: {int(score * 100)}%"
            ))
            seen_node_ids.add(node_id)

        # Sort by similarity score
        results.sort(key=lambda x: x.similarity_score, reverse=True)
        return results[:limit]

    def add_nodes(
        self,
        nodes: List[Node],
        edges: List[Edge]
    ) -> AddNodesResult:
        """
        Add nodes and edges
        Validates and saves to JSON
        """
        added_node_ids = []
        added_edge_ids = []

        try:
            # Add nodes
            nodes_to_embed = []
            for node in nodes:
                if node.id in self.nodes:
                    return AddNodesResult(
                        added_node_ids=[],
                        added_edge_ids=[],
                        success=False,
                        message=f"Node with ID {node.id} already exists"
                    )

                self.nodes[node.id] = node
                self.graph.add_node(node.id, data=node)
                added_node_ids.append(node.id)
                nodes_to_embed.append(node)

            # Generate embeddings for new nodes
            if nodes_to_embed:
                self.vector_store.update_nodes_embeddings(nodes_to_embed)

            # Create name-to-ID mapping for newly added nodes and existing nodes
            name_to_id = {}
            for node_id, node in self.nodes.items():
                name_to_id[node.name] = node_id

            # Add edges
            for edge in edges:
                # Resolve source and target - they might be names or IDs
                source_id = edge.source
                target_id = edge.target

                # If source is not a valid ID, try to resolve it as a name
                if source_id not in self.nodes:
                    if source_id in name_to_id:
                        source_id = name_to_id[source_id]
                    else:
                        raise ValueError(f"Source node '{edge.source}' does not exist (not found by ID or name)")

                # If target is not a valid ID, try to resolve it as a name
                if target_id not in self.nodes:
                    if target_id in name_to_id:
                        target_id = name_to_id[target_id]
                    else:
                        raise ValueError(f"Target node '{edge.target}' does not exist (not found by ID or name)")

                # Update edge with resolved IDs
                edge.source = source_id
                edge.target = target_id

                if edge.id in self.edges:
                    return AddNodesResult(
                        added_node_ids=[],
                        added_edge_ids=[],
                        success=False,
                        message=f"Edge with ID {edge.id} already exists"
                    )

                self.edges[edge.id] = edge
                self.graph.add_edge(
                    edge.source,
                    edge.target,
                    key=edge.id,
                    data=edge
                )
                added_edge_ids.append(edge.id)

            # Save to JSON
            self.save()

            return AddNodesResult(
                added_node_ids=added_node_ids,
                added_edge_ids=added_edge_ids,
                success=True,
                message=f"Added {len(added_node_ids)} nodes and {len(added_edge_ids)} edges"
            )

        except Exception as e:
            return AddNodesResult(
                added_node_ids=[],
                added_edge_ids=[],
                success=False,
                message=f"Error during add: {str(e)}"
            )

    def update_node(self, node_id: str, updates: Dict) -> Optional[Node]:
        """Update an existing node"""
        if node_id not in self.nodes:
            return None

        node = self.nodes[node_id]

        # Update allowed fields
        allowed_fields = {'name', 'description', 'summary', 'communities', 'metadata'}
        for key, value in updates.items():
            if key in allowed_fields:
                setattr(node, key, value)

        node.updated_at = datetime.utcnow()

        # Update in graph
        self.graph.nodes[node_id]['data'] = node

        # Update embedding if text fields changed
        if any(k in updates for k in ['name', 'description', 'summary']):
            self.vector_store.update_node_embedding(node)

        # Save
        self.save()
        return node

    def delete_nodes(
        self,
        node_ids: List[str],
        confirmed: bool = False
    ) -> DeleteNodesResult:
        """
        Delete nodes (max 10 at a time for safety)
        Requires confirmed=True
        """
        if len(node_ids) > 10:
            return DeleteNodesResult(
                deleted_node_ids=[],
                affected_edge_ids=[],
                success=False,
                message="Max 10 nodes can be deleted at a time. Contact admin for bulk deletion."
            )

        if not confirmed:
            return DeleteNodesResult(
                deleted_node_ids=[],
                affected_edge_ids=[],
                success=False,
                message="Deletion requires confirmed=True parameter"
            )

        deleted_node_ids = []
        affected_edge_ids = []

        try:
            for node_id in node_ids:
                if node_id not in self.nodes:
                    continue

                # Find all edges connected to this node
                edges_to_remove = []
                for edge_id, edge in self.edges.items():
                    if edge.source == node_id or edge.target == node_id:
                        edges_to_remove.append(edge_id)
                        affected_edge_ids.append(edge_id)

                # Remove edges
                for edge_id in edges_to_remove:
                    edge = self.edges[edge_id]
                    self.graph.remove_edge(edge.source, edge.target, key=edge_id)
                    del self.edges[edge_id]

                # Remove node
                self.graph.remove_node(node_id)
                del self.nodes[node_id]
                deleted_node_ids.append(node_id)

            # Remove embeddings
            self.vector_store.remove_nodes_embeddings(deleted_node_ids)

            # Save
            self.save()

            # TODO: Log deletion for audit

            return DeleteNodesResult(
                deleted_node_ids=deleted_node_ids,
                affected_edge_ids=affected_edge_ids,
                success=True,
                message=f"Deleted {len(deleted_node_ids)} nodes and {len(affected_edge_ids)} edges"
            )

        except Exception as e:
            return DeleteNodesResult(
                deleted_node_ids=[],
                affected_edge_ids=[],
                success=False,
                message=f"Error during deletion: {str(e)}"
            )

    def get_stats(self, communities: Optional[List[str]] = None) -> GraphStats:
        """Get statistics for the graph"""
        # Filter nodes based on communities
        relevant_nodes = self.nodes.values()
        if communities:
            relevant_nodes = [
                n for n in self.nodes.values()
                if any(comm in n.communities for comm in communities)
            ]

        # Count nodes per type
        nodes_by_type = {}
        for node in relevant_nodes:
            type_name = node.type.value
            nodes_by_type[type_name] = nodes_by_type.get(type_name, 0) + 1

        # Count nodes per community
        nodes_by_community = {}
        for node in relevant_nodes:
            for comm in node.communities:
                nodes_by_community[comm] = nodes_by_community.get(comm, 0) + 1

        return GraphStats(
            total_nodes=len(relevant_nodes),
            total_edges=len(self.edges),
            nodes_by_type=nodes_by_type,
            nodes_by_community=nodes_by_community,
            last_updated=datetime.utcnow()
        )
