"""
Backward-compatible wrapper for graph_core.storage

This module re-exports GraphStorage from graph_core for backward compatibility.
New code should import directly from graph_core instead.

Example:
    # Old way (still works)
    from graph_storage import GraphStorage

    # New way (preferred)
    from graph_core import GraphStorage
"""

# Re-export GraphStorage from graph_core
from graph_core.storage import GraphStorage

__all__ = ["GraphStorage"]
