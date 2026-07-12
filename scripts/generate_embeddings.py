import sys
import os
import argparse

# Add project root to sys.path
sys.path.append(os.getcwd())

from backend.core import GraphStorage

DEFAULT_GRAPH_PATH = "data/active/graph.json"


def generate_embeddings(graph_path=DEFAULT_GRAPH_PATH):
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
        # We must explicitly save the storage to persist the updated nodes
        storage.save()
        print(f"Success! Embeddings generated and saved to {graph_path}.")
        print(f"Total embeddings: {storage.vector_store.get_embedding_count()}")
    except Exception as e:
        print(f"Error generating embeddings: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate embeddings for a graph file."
    )
    parser.add_argument(
        "--graph-file",
        default=DEFAULT_GRAPH_PATH,
        help=f"Path to the graph JSON file (default: {DEFAULT_GRAPH_PATH})",
    )
    args = parser.parse_args()
    generate_embeddings(args.graph_file)
