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
from backend.core.vector_store import (  # noqa: E402
    dominant_dimension,
    matching_dimension,
)
from backend.core.embedding_sidecar import resolve_sidecar_path  # noqa: E402

DEFAULT_GRAPH_PATH = "data/active/graph.json"


def migrate_embeddings(graph_path=DEFAULT_GRAPH_PATH, embeddings_file=None):
    print("Migrating embeddings from pickle into the embedding sidecar...")

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

    sidecar_path = resolve_sidecar_path(graph_path, embeddings_file)
    if sidecar_path is not None and Path(sidecar_path) == embeddings_path:
        print(
            f"Refusing to run: the sidecar path and the legacy pickle are the same "
            f"file ({embeddings_path}). The migration would rename away the "
            f"sidecar it just wrote. Point EMBEDDINGS_FILE somewhere else."
        )
        return

    storage = GraphStorage(
        json_path=str(graph_path),
        embeddings_path=str(sidecar_path) if sidecar_path else None,
    )
    print(f"Target sidecar: {storage.embeddings_path}")

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
    existing = storage.vector_store.export_vectors()
    merged = dict(existing)
    for node in storage.nodes.values():
        if node.embedding is not None:
            merged[node.id] = node.embedding
            node.embedding = None

    # The pickle can carry a different dimension than the sidecar already has
    # - a model change between the two. Stacking those raises out of numpy, so
    # the sidecar's own dimension wins and the rest is reported, not crashed on.
    dimension = dominant_dimension(existing) or dominant_dimension(merged)
    accepted = matching_dimension(merged, dimension)
    if len(accepted) != len(merged):
        print(
            f"Skipped {len(merged) - len(accepted)} embedding(s) whose dimension "
            f"is not {dimension}."
        )
    storage.vector_store.load_vectors(accepted)

    storage.save().result()

    if not storage.vectors_persisted:
        # The graph save swallows a sidecar write failure by design. Renaming
        # the pickle on the strength of that would destroy the only remaining
        # copy of the vectors, and a re-run would then find nothing to migrate.
        print(
            f"FAILED: the vectors were not written to {storage.embeddings_path}. "
            f"{embeddings_path} has been left in place; fix the cause and re-run."
        )
        raise SystemExit(1)

    print(f"Embeddings written to {storage.embeddings_path}.")

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
    parser.add_argument(
        "--embeddings-file",
        default=None,
        help="Path to the embedding sidecar (default: EMBEDDINGS_FILE, else "
        "derived from the graph file)",
    )
    args = parser.parse_args()
    migrate_embeddings(args.graph_file, args.embeddings_file)
