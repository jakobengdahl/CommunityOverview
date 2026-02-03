import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(os.getcwd())

from backend.core import GraphStorage

def generate_embeddings():
    graph_path = "backend/graph.json"

    print(f"Loading graph from {graph_path}...")
    # Initialize without specifying embeddings path to use in-memory/json storage
    storage = GraphStorage(json_path=graph_path)

    nodes = list(storage.nodes.values())
    node_count = len(nodes)
    print(f"Found {node_count} nodes.")

    if node_count == 0:
        print("No nodes to embed.")
        return

    print("Generating embeddings (this may take a moment)...")
    try:
        # This will update node.embedding on the objects
        storage.vector_store.update_nodes_embeddings(nodes)
        # We must explicitly save the storage to persist the updated nodes to graph.json
        storage.save()
        print("Success! Embeddings generated and saved to graph.json.")
        print(f"Total embeddings: {storage.vector_store.get_embedding_count()}")
    except Exception as e:
        print(f"Error generating embeddings: {e}")

if __name__ == "__main__":
    generate_embeddings()
