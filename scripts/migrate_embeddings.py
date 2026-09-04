import sys
import os
import pickle
import argparse
from pathlib import Path


class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        # Allow specific safe classes for unpickling
        allowed_classes = {
            ("numpy.core.multiarray", "_reconstruct"),
            ("numpy", "ndarray"),
            ("numpy", "dtype"),
            ("numpy.core.numeric", "_frombuffer"),
            ("numpy.core.multiarray", "scalar"),
            ("numpy", "float64"),
            ("numpy", "float32"),
            ("numpy", "_core.multiarray"),
            ("numpy._core.multiarray", "_reconstruct"),
            ("numpy._core.numeric", "_frombuffer"),
        }

        if (module, name) in allowed_classes:
            return super().find_class(module, name)

        raise pickle.UnpicklingError(f"Global '{module}.{name}' is forbidden")


# Add project root to sys.path
sys.path.append(os.getcwd())

from backend.core import GraphStorage  # noqa: E402

DEFAULT_GRAPH_PATH = "data/active/graph.json"


def migrate_embeddings(graph_path=DEFAULT_GRAPH_PATH):
    print(f"Migrating embeddings from pickle to {graph_path}...")

    graph_path = Path(graph_path)
    embeddings_path = graph_path.parent / "embeddings.pkl"

    if not embeddings_path.exists():
        print(f"No embeddings file found at {embeddings_path}. Nothing to migrate.")
        return

    # Load raw pickle
    try:
        with open(embeddings_path, "rb") as f:
            data = RestrictedUnpickler(f).load()
            embeddings = data.get("embeddings", {})
            print(f"Loaded {len(embeddings)} embeddings from pickle.")
    except Exception as e:
        print(f"Error loading pickle: {e}")
        return

    # embeddings_path=None keeps the default sidecar location next to the graph.
    storage = GraphStorage(json_path=str(graph_path), embeddings_path=None)

    updated_count = 0
    for node_id, embedding in embeddings.items():
        if node_id in storage.nodes:
            # Assign embedding to node
            # Ensure it's a list for JSON serialization
            if hasattr(embedding, "tolist"):
                embedding_list = embedding.tolist()
            else:
                embedding_list = list(embedding)

            storage.nodes[node_id].embedding = embedding_list
            updated_count += 1

    print(f"Matched and assigned {updated_count} embeddings to nodes.")

    # Merge rather than rebuild: the graph may already have a sidecar, and
    # rebuild_index would drop every vector in it that the pickle does not
    # also carry. save() then persists the merged set into the sidecar.
    merged = storage.vector_store.export_vectors()
    for node in storage.nodes.values():
        if node.embedding is not None:
            merged[node.id] = node.embedding
            node.embedding = None
    storage.vector_store.load_vectors(merged)

    storage.save().result()
    print("Graph saved; embeddings written to the sidecar.")

    # Rename old pickle to indicate it's deprecated/backup
    backup_path = embeddings_path.with_suffix(".pkl.bak")
    embeddings_path.rename(backup_path)
    print(f"Moved {embeddings_path} to {backup_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate embeddings from pickle into a graph file."
    )
    parser.add_argument(
        "--graph-file",
        default=DEFAULT_GRAPH_PATH,
        help=f"Path to the graph JSON file (default: {DEFAULT_GRAPH_PATH})",
    )
    args = parser.parse_args()
    migrate_embeddings(args.graph_file)
