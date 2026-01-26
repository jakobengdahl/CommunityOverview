"""
Backward-compatible wrapper for graph_core.vector_store

This module re-exports VectorStore from graph_core for backward compatibility.
New code should import directly from graph_core instead.

Example:
    # Old way (still works)
    from vector_store import VectorStore

    # New way (preferred)
    from graph_core import VectorStore
"""

# Re-export VectorStore from graph_core
from graph_core.vector_store import VectorStore

__all__ = ["VectorStore"]
