"""
Vector Store for embeddings.
Handles generating, storing, and searching embeddings for nodes.
"""

import json
import os
import pickle
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from models import Node

class VectorStore:
    """
    Manages vector embeddings for graph nodes.
    Uses sentence-transformers for generating embeddings
    and sklearn for cosine similarity search.
    """

    def __init__(self, storage_path: str = "embeddings.pkl", model_name: str = "all-MiniLM-L6-v2"):
        self.storage_path = Path(storage_path)
        self.model_name = model_name
        self.model = None
        self.embeddings: Dict[str, np.ndarray] = {}  # node_id -> embedding
        self.node_ids: List[str] = [] # ordered list of node ids corresponding to embeddings matrix
        self.embedding_matrix: Optional[np.ndarray] = None

        self.load()

    def _load_model(self):
        """Lazy load the model"""
        if self.model is None:
            print(f"Loading embedding model: {self.model_name}...")
            self.model = SentenceTransformer(self.model_name)
            print("Model loaded.")

    def load(self):
        """Load embeddings from disk"""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, 'rb') as f:
                    data = pickle.load(f)
                    self.embeddings = data.get('embeddings', {})
                    self._update_matrix()
                print(f"Loaded {len(self.embeddings)} embeddings from {self.storage_path}")
            except Exception as e:
                print(f"Error loading embeddings: {e}")
                self.embeddings = {}
        else:
            print(f"No existing embeddings found at {self.storage_path}")

    def save(self):
        """Save embeddings to disk"""
        try:
            with open(self.storage_path, 'wb') as f:
                pickle.dump({
                    'embeddings': self.embeddings,
                    'model_name': self.model_name
                }, f)
            print(f"Saved {len(self.embeddings)} embeddings to {self.storage_path}")
        except Exception as e:
            print(f"Error saving embeddings: {e}")

    def _update_matrix(self):
        """Update the numpy matrix for vectorized operations"""
        if not self.embeddings:
            self.node_ids = []
            self.embedding_matrix = None
            return

        self.node_ids = list(self.embeddings.keys())
        self.embedding_matrix = np.array([self.embeddings[nid] for nid in self.node_ids])

    def _get_text_representation(self, node: Node) -> str:
        """Create a text representation of the node for embedding"""
        # Combine name, description, and summary
        # Giving more weight to name by repeating it? No, just concatenation is usually fine.
        text = f"{node.name}. {node.description or ''}. {node.summary or ''}"
        return text.strip()

    def generate_embedding(self, node: Node) -> np.ndarray:
        """Generate embedding for a single node"""
        self._load_model()
        text = self._get_text_representation(node)
        embedding = self.model.encode(text)
        return embedding

    def update_node_embedding(self, node: Node):
        """Update or add embedding for a node"""
        embedding = self.generate_embedding(node)
        self.embeddings[node.id] = embedding
        self._update_matrix()
        self.save()

    def update_nodes_embeddings(self, nodes: List[Node]):
        """Update embeddings for multiple nodes in batch"""
        if not nodes:
            return

        self._load_model()
        texts = [self._get_text_representation(node) for node in nodes]
        embeddings = self.model.encode(texts)

        for node, embedding in zip(nodes, embeddings):
            self.embeddings[node.id] = embedding

        self._update_matrix()
        self.save()

    def remove_node_embedding(self, node_id: str):
        """Remove embedding for a node"""
        if node_id in self.embeddings:
            del self.embeddings[node_id]
            self._update_matrix()
            self.save()

    def remove_nodes_embeddings(self, node_ids: List[str]):
        """Remove embeddings for multiple nodes"""
        changed = False
        for node_id in node_ids:
            if node_id in self.embeddings:
                del self.embeddings[node_id]
                changed = True

        if changed:
            self._update_matrix()
            self.save()

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
