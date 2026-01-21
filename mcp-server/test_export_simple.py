#!/usr/bin/env python3
"""
Simple test to verify export functionality works correctly
This can be run without starting the full server
"""

import sys
import json
from datetime import datetime
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

from models import Node, Edge, NodeType, RelationshipType
from graph_storage import GraphStorage

def test_export_serialization():
    """Test that export serialization works correctly"""
    print("=" * 60)
    print("Testing Export Serialization")
    print("=" * 60)

    # Create temporary graph
    import tempfile
    import os

    temp_file = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    temp_file.write('{"nodes": [], "edges": [], "metadata": {}}')
    temp_file.close()

    try:
        # Initialize graph storage
        graph = GraphStorage(temp_file.name)

        # Add test nodes
        node1 = Node(
            type=NodeType.ACTOR,
            name="Test Agency",
            description="A test agency",
            communities=["eSam"]
        )

        node2 = Node(
            type=NodeType.INITIATIVE,
            name="Test Initiative",
            description="A test initiative",
            communities=["eSam"]
        )

        edge1 = Edge(
            source=node1.id,
            target=node2.id,
            type=RelationshipType.BELONGS_TO
        )

        graph.add_nodes([node1, node2], [edge1])

        print(f"\n✓ Created graph with {len(graph.nodes)} nodes and {len(graph.edges)} edges")

        # Simulate export endpoint logic
        print("\n[Export] Starting graph export...")
        print(f"[Export] Total nodes in storage: {len(graph.nodes)}")
        print(f"[Export] Total edges in storage: {len(graph.edges)}")

        # Get all nodes and edges
        all_nodes = []
        for node in graph.nodes.values():
            try:
                node_dict = node.model_dump()
                all_nodes.append(node_dict)
            except Exception as e:
                print(f"[Export] Error dumping node {node.id}: {e}")
                raise

        all_edges = []
        for edge in graph.edges.values():
            try:
                edge_dict = edge.model_dump()
                all_edges.append(edge_dict)
            except Exception as e:
                print(f"[Export] Error dumping edge {edge.id}: {e}")
                raise

        print(f"[Export] Successfully dumped {len(all_nodes)} nodes and {len(all_edges)} edges")

        export_data = {
            "version": "1.0",
            "exportDate": datetime.utcnow().isoformat(),
            "nodes": all_nodes,
            "edges": all_edges,
            "total_nodes": len(all_nodes),
            "total_edges": len(all_edges)
        }

        # Custom JSON serializer
        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        print("[Export] Serializing export data to JSON...")
        serialized_data = json.dumps(export_data, default=json_serializer)
        print(f"[Export] Serialized {len(serialized_data)} bytes")

        result = json.loads(serialized_data)
        print("[Export] Successfully parsed JSON")

        # Verify structure
        assert "version" in result
        assert "exportDate" in result
        assert "nodes" in result
        assert "edges" in result
        assert "total_nodes" in result
        assert "total_edges" in result
        assert result["total_nodes"] == 2
        assert result["total_edges"] == 1

        # Verify datetime serialization
        for node in result["nodes"]:
            assert "created_at" in node
            assert "updated_at" in node
            assert isinstance(node["created_at"], str)
            assert isinstance(node["updated_at"], str)
            # Verify can be parsed
            datetime.fromisoformat(node["created_at"])
            datetime.fromisoformat(node["updated_at"])

        for edge in result["edges"]:
            assert "created_at" in edge
            assert isinstance(edge["created_at"], str)
            datetime.fromisoformat(edge["created_at"])

        print("\n" + "=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        print("\nExport data structure:")
        print(f"  - Version: {result['version']}")
        print(f"  - Export Date: {result['exportDate']}")
        print(f"  - Total Nodes: {result['total_nodes']}")
        print(f"  - Total Edges: {result['total_edges']}")
        print(f"\nSample node:")
        print(f"  - ID: {result['nodes'][0]['id']}")
        print(f"  - Name: {result['nodes'][0]['name']}")
        print(f"  - Type: {result['nodes'][0]['type']}")
        print(f"  - Created: {result['nodes'][0]['created_at']}")

        return True

    except Exception as e:
        print("\n" + "=" * 60)
        print("✗ TEST FAILED")
        print("=" * 60)
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Cleanup
        if os.path.exists(temp_file.name):
            os.remove(temp_file.name)

if __name__ == "__main__":
    success = test_export_serialization()
    sys.exit(0 if success else 1)
