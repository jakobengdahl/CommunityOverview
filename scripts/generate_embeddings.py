import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(os.getcwd())

from backend.core import GraphStorage

def generate_embeddings():
    graph_path = "backend/graph.json"
    embeddings_path = "backend/embeddings.pkl"

    print(f"Loading graph from {graph_path}...")
    storage = GraphStorage(json_path=graph_path, embeddings_path=embeddings_path)

    nodes = list(storage.nodes.values())
    node_count = len(nodes)
    print(f"Found {node_count} nodes.")

    if node_count == 0:
        print("No nodes to embed.")
        return

    print("Generating embeddings (this may take a moment)...")
    try:
        storage.vector_store.update_nodes_embeddings(nodes)
        print("Success! Embeddings generated and saved.")
        print(f"Total embeddings: {storage.vector_store.get_embedding_count()}")
    except Exception as e:
        print(f"Error generating embeddings: {e}")

if __name__ == "__main__":
    generate_embeddings()
