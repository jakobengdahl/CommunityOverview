"""
Whole-graph import: validation and entity construction.

This module implements the "validate everything before touching the live
graph" half of graph.json import (see docs/adr/0006-graph-import-replace-mode.md
and the Corp graph decision ``dec-graph-import-v1-scoped-to-replace-whole-graph``).
v1 is REPLACE-only: the imported document is validated as a *closed* graph in
its own right — edges may only reference node ids present in the SAME
document — because there is no merge-with-collision-policy in this version.
That is a separate, deferred slice.

Nothing here touches ``GraphStorage``. Validation reads only the schema
configuration (node/relationship type names, applicability rules) and the
document itself, so a caller can validate without holding any lock on the live
graph, and a validation failure can never have written anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError as PydanticValidationError

from backend.config import config_loader
from backend.core.models import (
    Edge,
    Node,
    is_valid_node_type,
    is_valid_relationship_type,
)

# A generous but finite cap. Without one, a malformed or hostile document (a
# self-referential JSON structure is not possible via normal JSON decoding,
# but an enormous flat list is) could be validated node-by-node forever before
# ever reporting an error. This is a request-size sanity backstop, not a
# product limit — raise it if a real import needs more.
MAX_IMPORT_NODES = 200_000
MAX_IMPORT_EDGES = 400_000


@dataclass(frozen=True)
class ImportIssue:
    """One actionable validation problem, located within the document."""

    location: str  # e.g. "nodes[3]" or "edges[7] (id=edge-12)"
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"location": self.location, "message": self.message}


@dataclass
class ImportValidationResult:
    """Outcome of validating a whole graph.json-shaped import document."""

    errors: List[ImportIssue] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [issue.to_dict() for issue in self.errors],
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
        }


def _fail(errors: List[ImportIssue]) -> ImportValidationResult:
    return ImportValidationResult(errors=errors, nodes=[], edges=[])


def validate_import_document(document: Any) -> ImportValidationResult:
    """
    Validate a whole graph.json-shaped document for a REPLACE import.

    Collects every problem it finds rather than stopping at the first, so the
    caller can report an actionable list in one round trip. Returns
    constructed ``Node``/``Edge`` objects only when the document is fully
    valid; a document with any error yields an empty ``nodes``/``edges``.

    Checks performed (all against the document alone, never the live graph):
      * top-level shape: a JSON object with a "nodes" list (and an optional
        "edges" list, defaulting to empty)
      * every node is an object with a non-empty "id" and a "type" that is a
        configured node type
      * no two nodes in the document share an id
      * every node is constructible as a ``Node`` (field length limits, etc.)
      * every edge is an object with a non-empty "id", "source" and "target"
      * no two edges in the document share an id
      * every edge's "type" (when given) is a configured relationship type
      * every edge's source/target refers to a node id present in THIS
        document — a dangling reference is invalid because v1 replaces the
        whole graph, so there is no pre-existing graph left for it to resolve
        against
      * every edge is constructible as an ``Edge``
      * every edge's type is applicable between its endpoints' types/subtypes,
        per the same schema rule ``GraphStorage._validate_edge_applicability``
        enforces on ordinary writes
    """
    if not isinstance(document, dict):
        return _fail([ImportIssue("$", "the import document must be a JSON object")])

    raw_nodes = document.get("nodes")
    if raw_nodes is None:
        return _fail([ImportIssue("nodes", 'the document has no "nodes" list')])
    if not isinstance(raw_nodes, list):
        return _fail([ImportIssue("nodes", '"nodes" must be a list')])

    raw_edges = document.get("edges", [])
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        return _fail([ImportIssue("edges", '"edges" must be a list')])

    if len(raw_nodes) > MAX_IMPORT_NODES:
        return _fail(
            [
                ImportIssue(
                    "nodes",
                    f"{len(raw_nodes)} nodes exceeds the {MAX_IMPORT_NODES} import limit",
                )
            ]
        )
    if len(raw_edges) > MAX_IMPORT_EDGES:
        return _fail(
            [
                ImportIssue(
                    "edges",
                    f"{len(raw_edges)} edges exceeds the {MAX_IMPORT_EDGES} import limit",
                )
            ]
        )

    errors: List[ImportIssue] = []
    seen_node_ids: Dict[str, int] = {}
    node_types: Dict[str, Tuple[str, List[str]]] = {}  # id -> (type_str, subtypes)
    built_nodes: List[Node] = []

    for index, raw_node in enumerate(raw_nodes):
        location = f"nodes[{index}]"
        if not isinstance(raw_node, dict):
            errors.append(ImportIssue(location, "a node must be a JSON object"))
            continue

        node_id = raw_node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(ImportIssue(location, 'a node must have a non-empty "id"'))
            node_id = None

        node_type = raw_node.get("type")
        if not isinstance(node_type, str) or not node_type.strip():
            errors.append(ImportIssue(location, 'a node must have a non-empty "type"'))
        elif not is_valid_node_type(node_type):
            errors.append(
                ImportIssue(location, f"'{node_type}' is not a configured node type")
            )

        if node_id is not None:
            if node_id in seen_node_ids:
                errors.append(
                    ImportIssue(
                        location,
                        f"duplicate node id '{node_id}' (first seen at "
                        f"nodes[{seen_node_ids[node_id]}])",
                    )
                )
            else:
                seen_node_ids[node_id] = index

        try:
            node = Node.from_dict(dict(raw_node))
        except (PydanticValidationError, ValueError, TypeError) as exc:
            errors.append(ImportIssue(location, f"invalid node: {exc}"))
            continue

        # Only the first occurrence of a given id is added to the reference
        # map / commit set — a later duplicate is already reported above and
        # must not silently shadow the first one's type for applicability
        # checks below.
        if node_id is not None and seen_node_ids.get(node_id) == index:
            node_types[node.id] = (node.type_str, list(node.subtypes))
            built_nodes.append(node)

    seen_edge_ids: Dict[str, int] = {}
    built_edges: List[Edge] = []

    for index, raw_edge in enumerate(raw_edges):
        location = f"edges[{index}]"
        if not isinstance(raw_edge, dict):
            errors.append(ImportIssue(location, "an edge must be a JSON object"))
            continue

        edge_id = raw_edge.get("id")
        if not isinstance(edge_id, str) or not edge_id.strip():
            errors.append(ImportIssue(location, 'an edge must have a non-empty "id"'))
            edge_id = None
        elif edge_id in seen_edge_ids:
            errors.append(
                ImportIssue(
                    location,
                    f"duplicate edge id '{edge_id}' (first seen at "
                    f"edges[{seen_edge_ids[edge_id]}])",
                )
            )
        else:
            seen_edge_ids[edge_id] = index

        source = raw_edge.get("source")
        target = raw_edge.get("target")
        if not isinstance(source, str) or not source.strip():
            errors.append(
                ImportIssue(location, 'an edge must have a non-empty "source"')
            )
        if not isinstance(target, str) or not target.strip():
            errors.append(
                ImportIssue(location, 'an edge must have a non-empty "target"')
            )

        edge_type = raw_edge.get("type")
        if edge_type not in (None, "") and not isinstance(edge_type, str):
            errors.append(ImportIssue(location, '"type" must be a string'))
        elif (
            isinstance(edge_type, str)
            and edge_type
            and not is_valid_relationship_type(edge_type)
        ):
            errors.append(
                ImportIssue(
                    location, f"'{edge_type}' is not a configured relationship type"
                )
            )

        if isinstance(source, str) and source and source not in node_types:
            errors.append(
                ImportIssue(
                    location,
                    f"source '{source}' does not reference a node in this document",
                )
            )
        if isinstance(target, str) and target and target not in node_types:
            errors.append(
                ImportIssue(
                    location,
                    f"target '{target}' does not reference a node in this document",
                )
            )

        try:
            edge = Edge.from_dict(dict(raw_edge))
        except (PydanticValidationError, ValueError, TypeError) as exc:
            errors.append(ImportIssue(location, f"invalid edge: {exc}"))
            continue

        if (
            isinstance(source, str)
            and source in node_types
            and isinstance(target, str)
            and target in node_types
        ):
            source_type, source_subtypes = node_types[source]
            target_type, target_subtypes = node_types[target]
            decision = config_loader.relationship_type_allows_node_types(
                edge.type_str,
                source_type,
                target_type,
                source_subtypes,
                target_subtypes,
            )
            if not decision.get("allowed"):
                errors.append(
                    ImportIssue(
                        location,
                        decision.get("message")
                        or f"relationship type '{edge.type_str}' is not allowed between "
                        f"'{source_type}' and '{target_type}'",
                    )
                )

        if edge_id is not None and seen_edge_ids.get(edge_id) == index:
            built_edges.append(edge)

    if errors:
        return _fail(errors)

    return ImportValidationResult(errors=[], nodes=built_nodes, edges=built_edges)
