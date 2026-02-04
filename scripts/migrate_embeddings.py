import sys
import os
import pickle
from pathlib import Path

# Add project root to sys.path
sys.path.append(os.getcwd())

from backend.core import GraphStorage

def migrate_embeddings():
    print("Migrating embeddings from pickle to graph.json...")

    graph_path = Path("backend/graph.json")
    embeddings_path = Path("backend/embeddings.pkl")

    if not embeddings_path.exists():
        print(f"No embeddings file found at {embeddings_path}. Nothing to migrate.")
        return

    # Load raw pickle
    try:
        with open(embeddings_path, 'rb') as f:
            data = pickle.load(f)
            embeddings = data.get('embeddings', {})
            print(f"Loaded {len(embeddings)} embeddings from pickle.")
    except Exception as e:
        print(f"Error loading pickle: {e}")
        return

    # Load storage (this loads nodes)
    # Note: We initialize with embeddings_path=None to use the new in-memory VectorStore logic,
    # effectively ignoring the pickle file for the storage itself initially.
    storage = GraphStorage(json_path=str(graph_path), embeddings_path=None)

    updated_count = 0
    for node_id, embedding in embeddings.items():
        if node_id in storage.nodes:
            # Assign embedding to node
            # Ensure it's a list for JSON serialization
            if hasattr(embedding, 'tolist'):
                embedding_list = embedding.tolist()
            else:
                embedding_list = list(embedding)

            storage.nodes[node_id].embedding = embedding_list
            updated_count += 1

    print(f"Matched and assigned {updated_count} embeddings to nodes.")

    # Save graph (this will write nodes with embeddings to graph.json)
    storage.save()
    print("Graph saved with embeddings.")

    # Rename old pickle to indicate it's deprecated/backup
    backup_path = embeddings_path.with_suffix('.pkl.bak')
    embeddings_path.rename(backup_path)
    print(f"Moved {embeddings_path} to {backup_path}")

if __name__ == "__main__":
    migrate_embeddings()
