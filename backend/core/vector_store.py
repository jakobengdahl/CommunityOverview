"""
Vector Store for embeddings.
Handles generating, storing, and searching embeddings for nodes.

This module is part of graph_core - the core graph storage layer.
It uses sentence-transformers with CPU-only PyTorch for lightweight embeddings.

Required dependencies (see backend/requirements.txt):
  numpy, sentence-transformers, scikit-learn, torch (CPU)

Imports are deferred (lazy) to speed up initial module load, but the
packages MUST be installed in the environment.
"""

import pickle
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path

from .models import Node

# Global references for lazy-loaded modules
_np = None
_SentenceTransformer = None
_cosine_similarity = None


def _ensure_numpy():
    """Lazy load numpy"""
    global _np
    if _np is None:
        import numpy as np
        _np = np
    return _np


def _ensure_sentence_transformers():
    """Lazy load sentence-transformers"""
    global _SentenceTransformer
    if _SentenceTransformer is None:
        from sentence_transformers import SentenceTransformer
        _SentenceTransformer = SentenceTransformer
    return _SentenceTransformer


def _ensure_sklearn():
    """Lazy load sklearn cosine_similarity"""
    global _cosine_similarity
    if _cosine_similarity is None:
        from sklearn.metrics.pairwise import cosine_similarity
        _cosine_similarity = cosine_similarity
    return _cosine_similarity


class VectorStore:
    """
    Manages vector embeddings for graph nodes.
    Uses sentence-transformers for generating embeddings
    and sklearn for cosine similarity search.

    Embeddings are stored directly on the Node objects and passed to this class
    to build the in-memory search index.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = None
        self.embeddings: Dict[str, Any] = {}  # node_id -> embedding (numpy array)
        self.node_ids: List[str] = []  # ordered list of node ids corresponding to embeddings matrix
        self.embedding_matrix: Optional[Any] = None  # numpy array

    def _load_model(self):
        """Lazy load the model"""
        if self.model is None:
            SentenceTransformer = _ensure_sentence_transformers()
            print(f"Loading embedding model: {self.model_name}...")
            self.model = SentenceTransformer(self.model_name)
            print("Model loaded.")

    def preload_model(self):
        """
        Preload the embedding model in a background thread.
        Call at startup to avoid slow first request.
        """
        import threading

        def _load():
            try:
                self._load_model()
                print(f"Embedding model '{self.model_name}' preloaded in background.")
            except Exception as e:
                print(f"Warning: Background model preload failed: {e}")

        t = threading.Thread(target=_load, name="embedding-preload", daemon=True)
        t.start()

    def rebuild_index(self, nodes: List[Node]):
        """Rebuild the search index from a list of nodes."""
        np = _ensure_numpy()
        self.embeddings = {}

        for node in nodes:
            if node.embedding is not None:
                # Convert list to numpy array if needed
                self.embeddings[node.id] = np.array(node.embedding)

        self._update_matrix()
        print(f"VectorStore index rebuilt with {len(self.embeddings)} embeddings")

    def _update_matrix(self):
        """Update the numpy matrix for vectorized operations"""
        if not self.embeddings:
            self.node_ids = []
            self.embedding_matrix = None
            return

        np = _ensure_numpy()
        self.node_ids = list(self.embeddings.keys())
        # Stack embeddings into a matrix
        self.embedding_matrix = np.vstack([self.embeddings[nid] for nid in self.node_ids])

    def _get_text_representation(self, node: Node) -> str:
        """Create a text representation of the node for embedding"""
        # Combine name, description, summary, and tags
        # Tags are important for similarity search
        tags_text = " ".join(node.tags) if hasattr(node, 'tags') and node.tags else ""
        text = f"{node.name}. {node.description or ''}. {node.summary or ''}. {tags_text}"
        return text.strip()

    def generate_embedding(self, node: Node) -> List[float]:
        """Generate embedding for a single node and return as list"""
        self._load_model()
        text = self._get_text_representation(node)
        embedding = self.model.encode(text)
        return embedding.tolist()

    def update_node_embedding(self, node: Node):
        """Update or add embedding for a node (updates the node object too)"""
        embedding_list = self.generate_embedding(node)
        node.embedding = embedding_list

        # Update internal index
        np = _ensure_numpy()
        self.embeddings[node.id] = np.array(embedding_list)
        self._update_matrix()

    def update_nodes_embeddings(self, nodes: List[Node]):
        """Update embeddings for multiple nodes in batch"""
        if not nodes:
            return

        self._load_model()
        texts = [self._get_text_representation(node) for node in nodes]
        embeddings = self.model.encode(texts)

        for node, embedding in zip(nodes, embeddings):
            # Convert to list for JSON storage
            node.embedding = embedding.tolist()
            # Update internal index
            self.embeddings[node.id] = embedding

        self._update_matrix()

    def remove_node_embedding(self, node_id: str):
        """Remove embedding for a node"""
        if node_id in self.embeddings:
            del self.embeddings[node_id]
            self._update_matrix()

    def remove_nodes_embeddings(self, node_ids: List[str]):
        """Remove embeddings for multiple nodes"""
        changed = False
        for node_id in node_ids:
            if node_id in self.embeddings:
                del self.embeddings[node_id]
                changed = True

        if changed:
            self._update_matrix()

    def search(self, query_text: str = None, query_node: Node = None, limit: int = 5, threshold: float = 0.0) -> List[Tuple[str, float]]:
        """
        Search for similar nodes.
        Can search by query text or by existing node.

        Returns:
            List of (node_id, score) tuples, sorted by score descending.
        """
        if not self.embeddings or self.embedding_matrix is None:
            return []

        self._load_model()
        cosine_similarity = _ensure_sklearn()

        if query_node:
            # If searching by node, check if we already have its embedding
            if query_node.id in self.embeddings:
                query_embedding = self.embeddings[query_node.id]
            else:
                query_embedding = self.generate_embedding(query_node)
        elif query_text:
            query_embedding = self.model.encode(query_text)
        else:
            return []

        # Reshape for sklearn (1, embedding_dim)
        query_embedding = query_embedding.reshape(1, -1)

        # Calculate cosine similarity
        similarities = cosine_similarity(query_embedding, self.embedding_matrix)[0]

        # Get indices of top results
        # We can filter by threshold here
        results = []
        for idx, score in enumerate(similarities):
            if score >= threshold:
                results.append((self.node_ids[idx], float(score)))

        # Sort by score descending
        results.sort(key=lambda x: x[1], reverse=True)

        # If query was a node in the database, remove it from results (similarity 1.0)
        if query_node:
            results = [r for r in results if r[0] != query_node.id]

        return results[:limit]

    def get_embedding_count(self) -> int:
        """Get the number of stored embeddings"""
        return len(self.embeddings)

    def has_embedding(self, node_id: str) -> bool:
        """Check if a node has an embedding"""
        return node_id in self.embeddings
