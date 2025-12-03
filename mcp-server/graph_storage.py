"""
Graf-lagring med NetworkX och JSON-persistens
Hanterar alla CRUD-operationer på grafen
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


class GraphStorage:
    """Hanterar graf-lagring med NetworkX + JSON persistens"""

    def __init__(self, json_path: str = "graph.json"):
        self.json_path = Path(json_path)
        self.graph = nx.MultiDiGraph()  # MultiDiGraph tillåter flera edges mellan samma noder
        self.nodes: Dict[str, Node] = {}  # node_id -> Node
        self.edges: Dict[str, Edge] = {}  # edge_id -> Edge
        self.load()

    def load(self) -> None:
        """Ladda graf från JSON-fil"""
        if not self.json_path.exists():
            print(f"Ingen graf-fil hittades på {self.json_path}, skapar ny tom graf")
            self.save()
            return

        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Ladda noder
            for node_data in data.get('nodes', []):
                node = Node.from_dict(node_data)
                self.nodes[node.id] = node
                self.graph.add_node(node.id, data=node)

            # Ladda edges
            for edge_data in data.get('edges', []):
                edge = Edge.from_dict(edge_data)
                self.edges[edge.id] = edge
                self.graph.add_edge(
                    edge.source,
                    edge.target,
                    key=edge.id,
                    data=edge
                )

            print(f"Laddade {len(self.nodes)} noder och {len(self.edges)} edges från {self.json_path}")

        except Exception as e:
            print(f"Fel vid laddning av graf: {e}")
            raise

    def save(self) -> None:
        """Spara graf till JSON-fil"""
        data = {
            'nodes': [node.to_dict() for node in self.nodes.values()],
            'edges': [edge.to_dict() for edge in self.edges.values()],
            'metadata': {
                'version': '1.0',
                'last_updated': datetime.utcnow().isoformat()
            }
        }

        # Skapa directory om det inte finns
        self.json_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"Sparade {len(self.nodes)} noder och {len(self.edges)} edges till {self.json_path}")

    def search_nodes(
        self,
        query: str,
        node_types: Optional[List[NodeType]] = None,
        communities: Optional[List[str]] = None,
        limit: int = 50
    ) -> List[Node]:
        """
        Söker noder baserat på text-query
        Matchar mot name, description, summary
        """
        query_lower = query.lower()
        results = []

        for node in self.nodes.values():
            # Filtrera på node type
            if node_types and node.type not in node_types:
                continue

            # Filtrera på communities
            if communities:
                if not any(comm in node.communities for comm in communities):
                    continue

            # Text-matching
            searchable_text = f"{node.name} {node.description} {node.summary}".lower()
            if query_lower in searchable_text:
                results.append(node)

            if len(results) >= limit:
                break

        return results

    def get_node(self, node_id: str) -> Optional[Node]:
        """Hämta en specifik nod"""
        return self.nodes.get(node_id)

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[RelationshipType]] = None,
        depth: int = 1
    ) -> Dict[str, any]:
        """
        Hämta noder kopplade till given nod
        Returnerar både noder och edges
        """
        if node_id not in self.nodes:
            return {'nodes': [], 'edges': []}

        visited_nodes = set([node_id])
        visited_edges = set()
        current_layer = {node_id}

        for _ in range(depth):
            next_layer = set()

            for curr_id in current_layer:
                # Utgående edges
                for _, target, edge_id, edge_data in self.graph.out_edges(curr_id, keys=True, data=True):
                    edge = edge_data['data']
                    if relationship_types and edge.type not in relationship_types:
                        continue
                    visited_edges.add(edge_id)
                    if target not in visited_nodes:
                        visited_nodes.add(target)
                        next_layer.add(target)

                # Ingående edges
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
        Hitta liknande noder baserat på Levenshtein distance
        Används för dublettkontroll
        """
        results = []
        name_lower = name.lower()

        for node in self.nodes.values():
            # Filtrera på typ om specificerat
            if node_type and node.type != node_type:
                continue

            # Beräkna similarity med Levenshtein
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
                    match_reason=f"Namnsimilaritet: {int(similarity * 100)}%"
                ))

        # Sortera efter similarity score
        results.sort(key=lambda x: x.similarity_score, reverse=True)
        return results[:limit]

    def add_nodes(
        self,
        nodes: List[Node],
        edges: List[Edge]
    ) -> AddNodesResult:
        """
        Lägger till noder och edges
        Validerar och sparar till JSON
        """
        added_node_ids = []
        added_edge_ids = []

        try:
            # Lägg till noder
            for node in nodes:
                if node.id in self.nodes:
                    return AddNodesResult(
                        added_node_ids=[],
                        added_edge_ids=[],
                        success=False,
                        message=f"Nod med ID {node.id} finns redan"
                    )

                self.nodes[node.id] = node
                self.graph.add_node(node.id, data=node)
                added_node_ids.append(node.id)

            # Lägg till edges
            for edge in edges:
                # Validera att source och target finns
                if edge.source not in self.nodes:
                    raise ValueError(f"Source node {edge.source} finns inte")
                if edge.target not in self.nodes:
                    raise ValueError(f"Target node {edge.target} finns inte")

                if edge.id in self.edges:
                    return AddNodesResult(
                        added_node_ids=[],
                        added_edge_ids=[],
                        success=False,
                        message=f"Edge med ID {edge.id} finns redan"
                    )

                self.edges[edge.id] = edge
                self.graph.add_edge(
                    edge.source,
                    edge.target,
                    key=edge.id,
                    data=edge
                )
                added_edge_ids.append(edge.id)

            # Spara till JSON
            self.save()

            return AddNodesResult(
                added_node_ids=added_node_ids,
                added_edge_ids=added_edge_ids,
                success=True,
                message=f"Lade till {len(added_node_ids)} noder och {len(added_edge_ids)} edges"
            )

        except Exception as e:
            return AddNodesResult(
                added_node_ids=[],
                added_edge_ids=[],
                success=False,
                message=f"Fel vid tillägg: {str(e)}"
            )

    def update_node(self, node_id: str, updates: Dict) -> Optional[Node]:
        """Uppdatera en befintlig nod"""
        if node_id not in self.nodes:
            return None

        node = self.nodes[node_id]

        # Uppdatera tillåtna fält
        allowed_fields = {'name', 'description', 'summary', 'communities', 'metadata'}
        for key, value in updates.items():
            if key in allowed_fields:
                setattr(node, key, value)

        node.updated_at = datetime.utcnow()

        # Uppdatera i graf
        self.graph.nodes[node_id]['data'] = node

        # Spara
        self.save()
        return node

    def delete_nodes(
        self,
        node_ids: List[str],
        confirmed: bool = False
    ) -> DeleteNodesResult:
        """
        Ta bort noder (max 10 åt gången för säkerhet)
        Kräver confirmed=True
        """
        if len(node_ids) > 10:
            return DeleteNodesResult(
                deleted_node_ids=[],
                affected_edge_ids=[],
                success=False,
                message="Max 10 noder kan tas bort åt gången. Kontakta admin för bulk-deletion."
            )

        if not confirmed:
            return DeleteNodesResult(
                deleted_node_ids=[],
                affected_edge_ids=[],
                success=False,
                message="Deletion kräver confirmed=True parameter"
            )

        deleted_node_ids = []
        affected_edge_ids = []

        try:
            for node_id in node_ids:
                if node_id not in self.nodes:
                    continue

                # Hitta alla edges kopplade till denna nod
                edges_to_remove = []
                for edge_id, edge in self.edges.items():
                    if edge.source == node_id or edge.target == node_id:
                        edges_to_remove.append(edge_id)
                        affected_edge_ids.append(edge_id)

                # Ta bort edges
                for edge_id in edges_to_remove:
                    edge = self.edges[edge_id]
                    self.graph.remove_edge(edge.source, edge.target, key=edge_id)
                    del self.edges[edge_id]

                # Ta bort nod
                self.graph.remove_node(node_id)
                del self.nodes[node_id]
                deleted_node_ids.append(node_id)

            # Spara
            self.save()

            # TODO: Logga deletion för audit

            return DeleteNodesResult(
                deleted_node_ids=deleted_node_ids,
                affected_edge_ids=affected_edge_ids,
                success=True,
                message=f"Tog bort {len(deleted_node_ids)} noder och {len(affected_edge_ids)} edges"
            )

        except Exception as e:
            return DeleteNodesResult(
                deleted_node_ids=[],
                affected_edge_ids=[],
                success=False,
                message=f"Fel vid deletion: {str(e)}"
            )

    def get_stats(self, communities: Optional[List[str]] = None) -> GraphStats:
        """Hämta statistik för grafen"""
        # Filtrera noder baserat på communities
        relevant_nodes = self.nodes.values()
        if communities:
            relevant_nodes = [
                n for n in self.nodes.values()
                if any(comm in n.communities for comm in communities)
            ]

        # Räkna noder per typ
        nodes_by_type = {}
        for node in relevant_nodes:
            type_name = node.type.value
            nodes_by_type[type_name] = nodes_by_type.get(type_name, 0) + 1

        # Räkna noder per community
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
