#!/usr/bin/env python3
"""
Minimal test to verify export serialization logic
Tests ONLY the JSON serialization without GraphStorage dependencies
"""

import sys
import json
from datetime import datetime
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

from models import Node, Edge, NodeType, RelationshipType

def test_json_serialization():
    """Test that Node and Edge can be serialized to JSON"""
    print("=" * 60)
    print("Testing JSON Serialization Logic")
    print("=" * 60)

    # Create test nodes
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

    # Create test edge
    edge1 = Edge(
        source=node1.id,
        target=node2.id,
        type=RelationshipType.BELONGS_TO
    )

    print(f"\n✓ Created 2 test nodes and 1 test edge")

    # Simulate what the export endpoint does
    print("\n[Export] Starting serialization test...")

    # Dump models to dict (this is what model_dump() does)
    all_nodes = [node1.model_dump(), node2.model_dump()]
    all_edges = [edge1.model_dump()]

    print(f"[Export] Dumped {len(all_nodes)} nodes and {len(all_edges)} edges")

    # Create export structure
    export_data = {
        "version": "1.0",
        "exportDate": datetime.utcnow().isoformat(),
        "nodes": all_nodes,
        "edges": all_edges,
        "total_nodes": len(all_nodes),
        "total_edges": len(all_edges)
    }

    # Custom JSON serializer (same as in server.py)
    def json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    # Test serialization
    try:
        print("[Export] Serializing to JSON...")
        serialized_data = json.dumps(export_data, default=json_serializer)
        print(f"[Export] ✓ Serialized {len(serialized_data)} bytes")

        # Parse it back
        print("[Export] Parsing JSON...")
        result = json.loads(serialized_data)
        print("[Export] ✓ Successfully parsed JSON")

        # Verify structure
        errors = []

        if "version" not in result:
            errors.append("Missing 'version' field")
        if "exportDate" not in result:
            errors.append("Missing 'exportDate' field")
        if "nodes" not in result:
            errors.append("Missing 'nodes' field")
        if "edges" not in result:
            errors.append("Missing 'edges' field")
        if "total_nodes" not in result:
            errors.append("Missing 'total_nodes' field")
        if "total_edges" not in result:
            errors.append("Missing 'total_edges' field")

        if result.get("total_nodes") != 2:
            errors.append(f"Expected 2 nodes, got {result.get('total_nodes')}")
        if result.get("total_edges") != 1:
            errors.append(f"Expected 1 edge, got {result.get('total_edges')}")

        # Verify datetime serialization in nodes
        for i, node in enumerate(result["nodes"]):
            if "created_at" not in node:
                errors.append(f"Node {i}: Missing 'created_at'")
            elif not isinstance(node["created_at"], str):
                errors.append(f"Node {i}: 'created_at' is not a string")
            else:
                try:
                    datetime.fromisoformat(node["created_at"])
                except ValueError as e:
                    errors.append(f"Node {i}: 'created_at' not in ISO format: {e}")

            if "updated_at" not in node:
                errors.append(f"Node {i}: Missing 'updated_at'")
            elif not isinstance(node["updated_at"], str):
                errors.append(f"Node {i}: 'updated_at' is not a string")
            else:
                try:
                    datetime.fromisoformat(node["updated_at"])
                except ValueError as e:
                    errors.append(f"Node {i}: 'updated_at' not in ISO format: {e}")

        # Verify datetime serialization in edges
        for i, edge in enumerate(result["edges"]):
            if "created_at" not in edge:
                errors.append(f"Edge {i}: Missing 'created_at'")
            elif not isinstance(edge["created_at"], str):
                errors.append(f"Edge {i}: 'created_at' is not a string")
            else:
                try:
                    datetime.fromisoformat(edge["created_at"])
                except ValueError as e:
                    errors.append(f"Edge {i}: 'created_at' not in ISO format: {e}")

        if errors:
            print("\n" + "=" * 60)
            print("✗ VALIDATION FAILED")
            print("=" * 60)
            for error in errors:
                print(f"  - {error}")
            return False

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
        print(f"  - Updated: {result['nodes'][0]['updated_at']}")
        print(f"\nSample edge:")
        print(f"  - ID: {result['edges'][0]['id']}")
        print(f"  - Source: {result['edges'][0]['source']}")
        print(f"  - Target: {result['edges'][0]['target']}")
        print(f"  - Type: {result['edges'][0]['type']}")
        print(f"  - Created: {result['edges'][0]['created_at']}")

        return True

    except Exception as e:
        print("\n" + "=" * 60)
        print("✗ TEST FAILED - Exception during serialization")
        print("=" * 60)
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_json_serialization()
    sys.exit(0 if success else 1)
