import sys
import os
import argparse

# Add project root to sys.path
sys.path.append(os.getcwd())

from backend.core import GraphStorage
from backend.core.embedding_sidecar import resolve_sidecar_path

DEFAULT_GRAPH_PATH = "data/active/graph.json"


def generate_embeddings(graph_path=DEFAULT_GRAPH_PATH, embeddings_file=None):
    print(f"Loading graph from {graph_path}...")
    sidecar_path = resolve_sidecar_path(graph_path, embeddings_file)
    storage = GraphStorage(
        json_path=graph_path,
        embeddings_path=str(sidecar_path) if sidecar_path else None,
    )

    nodes = list(storage.nodes.values())
    node_count = len(nodes)
    print(f"Found {node_count} nodes.")

    if node_count == 0:
        print("No nodes to embed.")
        return

    print("Generating embeddings (this may take a moment)...")
    try:
        # Vectors land in the vector store, not on the node objects.
        storage.vector_store.update_nodes_embeddings(nodes)
        # save() persists them through the embedding sidecar. Waiting orders the
        # write before the report; whether it succeeded is a separate question,
        # asked below, because a sidecar failure does not fail the save.
        storage.save().result()

        if not storage.vectors_persisted:
            # A sidecar write failure is not fatal to the graph save, so it
            # must be asked about rather than inferred from save() returning.
            print(
                f"FAILED: embeddings were generated but not written to "
                f"{storage.embeddings_path}. See the warning above for the cause."
            )
            raise SystemExit(1)

        print(f"Success! Embeddings written to {storage.embeddings_path}.")
        print(f"Total embeddings: {storage.vector_store.get_embedding_count()}")
    except SystemExit:
        raise
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
    parser.add_argument(
        "--embeddings-file",
        default=None,
        help="Path to the embedding sidecar (default: EMBEDDINGS_FILE, else "
        "derived from the graph file)",
    )
    args = parser.parse_args()
    generate_embeddings(args.graph_file, args.embeddings_file)
