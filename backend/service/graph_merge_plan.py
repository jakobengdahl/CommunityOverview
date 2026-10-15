"""
Merge import, slice 1: the read-only planner and its plan digest.

Implements the plan half of docs/adr/0013-graph-import-merge-mode.md: given an
uploaded graph.json document, the merge parameters and a read view of the live
graph, compute what a merge commit would do to every entity (§1, §2, §5) and a
stateless digest that binds a later commit to exactly that plan (§3 (c)).

Nothing here writes. The planner reads the live graph one entity at a time
through ``LiveGraphView`` without holding the storage lock (ADR §4,
"Concurrency with live writes"); the digest records the revision of every
live entity whose presence or revision decided an outcome, and the graph's
``generation``, sampled before the first read and re-checked after the last,
so a commit can tell the plan it was shown from the plan it would now get.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple

from pydantic import ValidationError as PydanticValidationError

from backend.config import config_loader
from backend.core.models import (
    Edge,
    Node,
    is_valid_node_type,
    is_valid_relationship_type,
)

from .graph_import import (
    ImportIssue,
    build_document_nodes,
    check_document_edge_shape,
    read_document_lists,
)

if TYPE_CHECKING:
    from backend.core import GraphStorage

    from .attachments import AttachmentSettings

POLICIES = ("fail", "skip", "overwrite", "rename")
DEFAULT_POLICY = "fail"

# The caller-writable fields an overwrite takes from the document — the ones
# ``update_node`` / ``update_edge`` accept. Server-owned fields (``revision``,
# timestamps) and ``archived`` (its own write path) never come from it.
NODE_OVERWRITE_FIELDS = (
    "name",
    "description",
    "summary",
    "tags",
    "subtypes",
    "aliases",
    "metadata",
)
EDGE_OVERWRITE_FIELDS = ("type", "label", "metadata")
# The fields whose change makes ``update_node`` re-embed a node (ADR §6).
EMBEDDING_FIELDS = ("name", "description", "summary", "tags", "aliases")

# Ids listed per plan category; counts are always exact (ADR §3).
PLAN_LIST_CAP = 1_000
# A replace landing mid-plan restarts it; this bounds the restarts.
MAX_PLAN_ATTEMPTS = 3
DIGEST_VERSION = 1

_ALLOWED_PARAMETER_KEYS = {
    "document",
    "policy",
    "include_system_types",
    "stamp_provenance",
}


class MergeRequestError(Exception):
    """A request the planner refuses before planning; nothing was read."""

    def __init__(
        self,
        error_code: str,
        message: str,
        errors: Optional[List[ImportIssue]] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.errors = errors or []


class MergePlanUnstable(Exception):
    """The graph was replaced during every planning attempt."""


@dataclass(frozen=True)
class MergePolicy:
    default: str = DEFAULT_POLICY
    by_type: Tuple[Tuple[str, str], ...] = ()

    def for_type(self, type_name: str) -> str:
        for name, policy in self.by_type:
            if name == type_name:
                return policy
        return self.default

    def canonical(self) -> Dict[str, Any]:
        return {"default": self.default, "by_type": dict(sorted(self.by_type))}


@dataclass(frozen=True)
class MergeParameters:
    policy: MergePolicy = MergePolicy()
    include_system_types: bool = False
    stamp_provenance: bool = False

    def canonical(self) -> Dict[str, Any]:
        return {
            "policy": self.policy.canonical(),
            "include_system_types": self.include_system_types,
            "stamp_provenance": self.stamp_provenance,
        }


def _invalid(message: str) -> MergeRequestError:
    return MergeRequestError("invalid_parameters", message)


def parse_merge_request(body: Any) -> Tuple[Any, MergeParameters]:
    """Split a preview request body into ``(document, parameters)``.

    Unknown keys are refused rather than ignored, so a misspelt parameter
    cannot silently fall back to a default. ``stamp_provenance: true`` is
    refused until stamping ships (ADR slice 1).
    """
    if not isinstance(body, dict):
        raise _invalid("the request body must be a JSON object")
    unknown = sorted(set(body) - _ALLOWED_PARAMETER_KEYS)
    if unknown:
        raise _invalid(f"unknown request field(s): {', '.join(unknown)}")
    if "document" not in body:
        raise _invalid('the request has no "document"')

    include_system_types = body.get("include_system_types", False)
    if not isinstance(include_system_types, bool):
        raise _invalid('"include_system_types" must be a boolean')
    stamp_provenance = body.get("stamp_provenance", False)
    if not isinstance(stamp_provenance, bool):
        raise _invalid('"stamp_provenance" must be a boolean')
    if stamp_provenance:
        raise MergeRequestError(
            "provenance_not_supported",
            "stamp_provenance is not supported yet; send false or omit it.",
        )

    return body["document"], MergeParameters(
        policy=_parse_policy(body.get("policy")),
        include_system_types=include_system_types,
        stamp_provenance=False,
    )


def _parse_policy(raw: Any) -> MergePolicy:
    if raw is None:
        return MergePolicy()
    if not isinstance(raw, dict):
        raise _invalid('"policy" must be an object')
    unknown = sorted(set(raw) - {"default", "by_type"})
    if unknown:
        raise _invalid(f"unknown policy field(s): {', '.join(unknown)}")
    default = raw.get("default", DEFAULT_POLICY)
    if default not in POLICIES:
        raise _invalid(f'policy "default" must be one of {", ".join(POLICIES)}')
    by_type_raw = raw.get("by_type") or {}
    if not isinstance(by_type_raw, dict):
        raise _invalid('policy "by_type" must be an object')
    by_type: List[Tuple[str, str]] = []
    for type_name, policy in by_type_raw.items():
        if not (is_valid_node_type(type_name) or is_valid_relationship_type(type_name)):
            raise _invalid(
                f"policy by_type key '{type_name}' is neither a configured node "
                "type nor a configured relationship type"
            )
        if policy not in POLICIES:
            raise _invalid(
                f"policy for '{type_name}' must be one of {', '.join(POLICIES)}"
            )
        by_type.append((type_name, policy))
    return MergePolicy(default=default, by_type=tuple(sorted(by_type)))


class LiveGraphView(Protocol):
    """The reads the planner makes of the live graph, one entity at a time."""

    @property
    def generation(self) -> int: ...

    def get_node(self, node_id: str) -> Optional[Node]: ...

    def get_edge(self, edge_id: str) -> Optional[Edge]: ...

    def edges_from_to(self, source: str, target: str) -> List[Edge]: ...

    def incident_edges(self, node_id: str) -> List[Edge]: ...

    def has_attachments(self, node_id: str) -> Optional[bool]: ...

    def nodes_without_vector(self) -> int: ...


class StorageGraphView:
    """``LiveGraphView`` over a ``GraphStorage`` and its attachment store."""

    def __init__(
        self,
        storage: "GraphStorage",
        attachment_settings: Optional["AttachmentSettings"] = None,
    ):
        self._storage = storage
        self._attachments = attachment_settings

    @property
    def generation(self) -> int:
        return self._storage.generation

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._storage.get_node(node_id)

    def get_edge(self, edge_id: str) -> Optional[Edge]:
        return self._storage.edges.get(edge_id)

    def edges_from_to(self, source: str, target: str) -> List[Edge]:
        return self._storage.get_edges_from_to(source, target)

    def incident_edges(self, node_id: str) -> List[Edge]:
        return self._storage.get_edges_for_node(node_id)

    def has_attachments(self, node_id: str) -> Optional[bool]:
        if self._attachments is None:
            return None
        from backend.core.attachment_store import AttachmentStoreError

        try:
            return bool(
                self._attachments.store.list(self._attachments.namespace, node_id)
            )
        except AttachmentStoreError:
            # An unreadable index still means files sit under that id.
            return True

    def nodes_without_vector(self) -> int:
        vector_store = self._storage.vector_store
        return sum(
            1
            for node in self._storage.get_all_nodes()
            if not vector_store.has_embedding(node.id)
        )


@dataclass
class MergePlan:
    report: Dict[str, Any]
    digest: str
    committable: bool


# --- planning ---------------------------------------------------------------


@dataclass
class _NodeDecision:
    index: int
    node: Node
    outcome: str  # create | overwrite | unchanged | skip | rename | excluded | conflict
    reason: str = ""
    live: Optional[Node] = None
    changes: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class _Endpoint:
    node_id: str
    kind: str  # document | renamed | live | excluded | conflict | dangling
    type_name: str = ""
    subtypes: Tuple[str, ...] = ()
    archived: bool = False
    has_live_edges: bool = False  # the id exists live, so live edges may join it


@dataclass
class _EdgeDecision:
    index: int
    edge: Edge
    outcome: str  # create | overwrite | unchanged | skip | rename | excluded | conflict | rejected
    reason: str = ""
    source: Optional[_Endpoint] = None
    target: Optional[_Endpoint] = None
    changes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    duplicate_of: Optional[str] = None


_WRITTEN = ("create", "overwrite", "rename")


def compute_merge_plan(
    document: Any, parameters: MergeParameters, view: LiveGraphView
) -> MergePlan:
    """Plan merging ``document`` into the live graph ``view`` reads; write nothing.

    Raises ``MergeRequestError`` (``validation_failed``) when the document's
    shape or size cannot be judged entity by entity, and ``MergePlanUnstable``
    when a whole-graph replace moved ``generation`` during every attempt.
    """
    raw_nodes, raw_edges, shape_errors = read_document_lists(document)
    if shape_errors:
        raise MergeRequestError(
            "validation_failed",
            "the import document cannot be planned",
            shape_errors,
        )
    for _attempt in range(MAX_PLAN_ATTEMPTS):
        generation = view.generation
        plan = _Planner(raw_nodes, raw_edges, parameters, view, generation).run()
        if view.generation == generation:
            return plan
    raise MergePlanUnstable(
        "the graph was replaced while the merge plan was being computed"
    )


class _Planner:
    def __init__(
        self,
        raw_nodes: List[Any],
        raw_edges: List[Any],
        parameters: MergeParameters,
        view: LiveGraphView,
        generation: int,
    ):
        self.raw_nodes = raw_nodes
        self.raw_edges = raw_edges
        self.params = parameters
        self.view = view
        self.generation = generation
        self.rejected: List[ImportIssue] = []
        self.conflicts: List[Dict[str, str]] = []
        # (kind, id) -> live revision, or None when the plan relied on absence.
        self.reads: Dict[Tuple[str, str], Optional[int]] = {}
        self.nodes: Dict[str, _NodeDecision] = {}
        self.edges: List[_EdgeDecision] = []
        self.archived_attach_edges: List[str] = []

    # -- live reads, each recorded for the digest ---------------------------

    def _read_node(self, node_id: str) -> Optional[Node]:
        node = self.view.get_node(node_id)
        self.reads[("node", node_id)] = node.revision if node is not None else None
        return node

    def _read_edge(self, edge_id: str) -> Optional[Edge]:
        edge = self.view.get_edge(edge_id)
        self.reads[("edge", edge_id)] = edge.revision if edge is not None else None
        return edge

    def _is_excluded_type(self, type_name: str) -> bool:
        return (
            not self.params.include_system_types
            and type_name in config_loader.SYSTEM_NODE_TYPES
        )

    def _conflict(
        self, kind: str, entity_id: str, location: str, reason: str, message: str
    ):
        self.conflicts.append(
            {
                "kind": kind,
                "id": entity_id,
                "location": location,
                "reason": reason,
                "message": message,
            }
        )

    # -- nodes ----------------------------------------------------------------

    def _plan_nodes(self) -> None:
        for index, node in build_document_nodes(self.raw_nodes, self.rejected):
            location = f"nodes[{index}]"
            if self._is_excluded_type(node.type_str):
                self.nodes[node.id] = _NodeDecision(
                    index, node, "excluded", "system_type_excluded"
                )
                continue
            live = self._read_node(node.id)
            if live is None:
                self.nodes[node.id] = _NodeDecision(index, node, "create")
                continue

            policy = self.params.policy.for_type(node.type_str)
            if live.type_str != node.type_str:
                if policy == "rename":
                    self.nodes[node.id] = _NodeDecision(
                        index, node, "rename", live=live
                    )
                    continue
                self.nodes[node.id] = _NodeDecision(
                    index, node, "conflict", "type_mismatch", live=live
                )
                self._conflict(
                    "node",
                    node.id,
                    location,
                    "type_mismatch",
                    f"the document says '{node.type_str}' but the live node is "
                    f"'{live.type_str}'; only the rename policy can resolve it",
                )
            elif policy == "fail":
                self.nodes[node.id] = _NodeDecision(
                    index, node, "conflict", "id_collision", live=live
                )
                self._conflict(
                    "node",
                    node.id,
                    location,
                    "id_collision",
                    f"node id '{node.id}' already exists and the policy for "
                    f"'{node.type_str}' is fail",
                )
            elif policy == "skip":
                self.nodes[node.id] = _NodeDecision(
                    index, node, "skip", "collision_kept_live", live=live
                )
            elif policy == "rename":
                self.nodes[node.id] = _NodeDecision(index, node, "rename", live=live)
            else:
                changes = _field_changes(live, node, NODE_OVERWRITE_FIELDS)
                outcome = "overwrite" if changes else "unchanged"
                self.nodes[node.id] = _NodeDecision(
                    index,
                    node,
                    outcome,
                    "" if changes else "unchanged",
                    live=live,
                    changes=changes,
                )

    # -- edges ----------------------------------------------------------------

    def _resolve(self, node_id: str) -> _Endpoint:
        decision = self.nodes.get(node_id)
        if decision is not None:
            node = decision.node
            if decision.outcome == "excluded":
                return _Endpoint(node_id, "excluded")
            if decision.outcome == "conflict":
                return _Endpoint(node_id, "conflict")
            if decision.outcome == "rename":
                return _Endpoint(
                    node_id, "renamed", node.type_str, tuple(node.subtypes)
                )
            if decision.outcome == "skip":
                live = decision.live
                assert live is not None
                return _Endpoint(
                    node_id,
                    "live",
                    live.type_str,
                    tuple(live.subtypes),
                    live.archived,
                    True,
                )
            return _Endpoint(
                node_id,
                "document",
                node.type_str,
                tuple(node.subtypes),
                node.archived if decision.live is None else decision.live.archived,
                decision.live is not None,
            )
        live = self._read_node(node_id)
        if live is None:
            return _Endpoint(node_id, "dangling")
        if self._is_excluded_type(live.type_str):
            return _Endpoint(node_id, "excluded")
        return _Endpoint(
            node_id, "live", live.type_str, tuple(live.subtypes), live.archived, True
        )

    def _plan_edges(self) -> None:
        seen_edge_ids: Dict[str, int] = {}
        for index, raw_edge in enumerate(self.raw_edges):
            location = f"edges[{index}]"
            errors_before = len(self.rejected)
            if not check_document_edge_shape(
                raw_edge, location, index, seen_edge_ids, self.rejected
            ):
                continue
            try:
                edge = Edge.from_dict(dict(raw_edge))
            except (PydanticValidationError, ValueError, TypeError) as exc:
                self.rejected.append(ImportIssue(location, f"invalid edge: {exc}"))
                continue
            if len(self.rejected) != errors_before:
                continue
            self.edges.append(self._plan_edge(index, location, edge))

    def _plan_edge(self, index: int, location: str, edge: Edge) -> _EdgeDecision:
        source = self._resolve(edge.source)
        target = self._resolve(edge.target)
        decision = _EdgeDecision(index, edge, "create", source=source, target=target)

        if "excluded" in (source.kind, target.kind):
            decision.outcome, decision.reason = "excluded", "system_type_excluded"
            return decision
        dangling = [
            (role, end)
            for role, end in (("source", source), ("target", target))
            if end.kind == "dangling"
        ]
        if dangling:
            for role, end in dangling:
                self.rejected.append(
                    ImportIssue(
                        location,
                        f"{role} '{end.node_id}' is neither a node in this document "
                        "nor a live node (dangling)",
                    )
                )
            decision.outcome = "rejected"
            return decision
        if "conflict" in (source.kind, target.kind):
            decision.outcome, decision.reason = "conflict", "endpoint_in_conflict"
            self._conflict(
                "edge",
                edge.id,
                location,
                "endpoint_in_conflict",
                "an endpoint of this edge is itself in conflict",
            )
            return decision

        live_edge = self._read_edge(edge.id)
        if live_edge is not None:
            policy = self.params.policy.for_type(edge.type_str)
            same_endpoints = (
                "renamed" not in (source.kind, target.kind)
                and live_edge.source == edge.source
                and live_edge.target == edge.target
            )
            if not same_endpoints:
                if policy != "rename":
                    decision.outcome, decision.reason = "conflict", "endpoint_mismatch"
                    self._conflict(
                        "edge",
                        edge.id,
                        location,
                        "endpoint_mismatch",
                        f"edge id '{edge.id}' exists with different endpoints; "
                        "only the rename policy can resolve it",
                    )
                    return decision
                decision.outcome = "rename"
            elif policy == "fail":
                decision.outcome, decision.reason = "conflict", "id_collision"
                self._conflict(
                    "edge",
                    edge.id,
                    location,
                    "id_collision",
                    f"edge id '{edge.id}' already exists and the policy for "
                    f"'{edge.type_str}' is fail",
                )
                return decision
            elif policy == "skip":
                decision.outcome, decision.reason = "skip", "collision_kept_live"
                return decision
            elif policy == "rename":
                decision.outcome = "rename"
            else:
                decision.changes = _field_changes(
                    live_edge, edge, EDGE_OVERWRITE_FIELDS
                )
                if not decision.changes:
                    decision.outcome, decision.reason = "unchanged", "unchanged"
                    return decision
                decision.outcome = "overwrite"

        if decision.outcome in ("create", "rename"):
            duplicate = self._live_duplicate(edge, source, target)
            if duplicate is not None:
                decision.outcome, decision.reason = "skip", "duplicate_edge"
                decision.duplicate_of = duplicate
                return decision

        verdict = config_loader.relationship_type_allows_node_types(
            edge.type_str,
            source.type_name,
            target.type_name,
            list(source.subtypes),
            list(target.subtypes),
        )
        if not verdict.get("allowed"):
            message = verdict.get("message") or (
                f"relationship type '{edge.type_str}' is not allowed between "
                f"'{source.type_name}' and '{target.type_name}'"
            )
            if "live" in (source.kind, target.kind):
                decision.outcome, decision.reason = "conflict", "inapplicable"
                self._conflict("edge", edge.id, location, "inapplicable", message)
            else:
                self.rejected.append(ImportIssue(location, message))
                decision.outcome = "rejected"
            return decision

        for end in (source, target):
            if end.kind == "live" and end.archived:
                self.archived_attach_edges.append(edge.id)
                break
        return decision

    def _live_duplicate(
        self, edge: Edge, source: _Endpoint, target: _Endpoint
    ) -> Optional[str]:
        if not (source.has_live_edges and target.has_live_edges):
            return None
        matches = sorted(
            live.id
            for live in self.view.edges_from_to(source.node_id, target.node_id)
            if live.type_str == edge.type_str and live.id != edge.id
        )
        if not matches:
            return None
        self._read_edge(matches[0])
        return matches[0]

    # -- §1: live incident edges of a node whose subtypes change -----------

    def _rejudge_incident_edges(self) -> None:
        overwritten_edge_ids = {
            d.edge.id for d in self.edges if d.outcome == "overwrite"
        }
        for decision in sorted(self.nodes.values(), key=lambda d: d.node.id):
            if decision.outcome != "overwrite" or "subtypes" not in decision.changes:
                continue
            for live_edge in sorted(
                self.view.incident_edges(decision.node.id), key=lambda e: e.id
            ):
                if live_edge.id in overwritten_edge_ids:
                    continue
                self._read_edge(live_edge.id)
                ends = [
                    self._resulting_state(live_edge.source),
                    self._resulting_state(live_edge.target),
                ]
                if None in ends:
                    continue
                (source_type, source_sub), (target_type, target_sub) = ends
                verdict = config_loader.relationship_type_allows_node_types(
                    live_edge.type_str,
                    source_type,
                    target_type,
                    list(source_sub),
                    list(target_sub),
                )
                if not verdict.get("allowed"):
                    self._conflict(
                        "edge",
                        live_edge.id,
                        f"nodes[{decision.index}]",
                        "incident_edge_inapplicable",
                        f"overwriting the subtypes of '{decision.node.id}' would "
                        f"leave live edge '{live_edge.id}' inapplicable: "
                        + (verdict.get("message") or ""),
                    )

    def _resulting_state(self, node_id: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
        decision = self.nodes.get(node_id)
        if decision is not None and decision.outcome in ("overwrite", "unchanged"):
            return decision.node.type_str, tuple(decision.node.subtypes)
        live = self._read_node(node_id)
        if live is None:
            return None
        return live.type_str, tuple(live.subtypes)

    # -- assembly -------------------------------------------------------------

    def run(self) -> MergePlan:
        self._plan_nodes()
        self._plan_edges()
        self._rejudge_incident_edges()
        live_without_vector = self.view.nodes_without_vector()
        report = self._report(live_without_vector)
        digest = self._digest()
        committable = not self.rejected and not self.conflicts
        report["committable"] = committable
        return MergePlan(report=report, digest=digest, committable=committable)

    def _renamed_edge_rewrites(self) -> Dict[str, List[str]]:
        rewrites: Dict[str, List[str]] = {}
        for d in self.edges:
            for end in (d.source, d.target):
                if end is not None and end.kind == "renamed":
                    rewrites.setdefault(end.node_id, []).append(d.edge.id)
        return {k: sorted(set(v)) for k, v in rewrites.items()}

    def _report(self, live_without_vector: int) -> Dict[str, Any]:
        nodes = sorted(self.nodes.values(), key=lambda d: d.node.id)
        edges = sorted(self.edges, key=lambda d: d.edge.id)

        def by(outcome, items):
            return [d for d in items if d.outcome == outcome]

        node_creates, edge_creates = by("create", nodes), by("create", edges)
        node_overwrites, edge_overwrites = (
            by("overwrite", nodes),
            by("overwrite", edges),
        )
        node_renames, edge_renames = by("rename", nodes), by("rename", edges)
        rewrites = self._renamed_edge_rewrites()

        skips: List[Dict[str, Any]] = []
        for kind, items in (("node", nodes), ("edge", edges)):
            for d in items:
                if d.outcome in ("skip", "unchanged", "excluded"):
                    entry: Dict[str, Any] = {
                        "kind": kind,
                        "id": (d.node.id if kind == "node" else d.edge.id),
                        "reason": d.reason,
                    }
                    if kind == "edge" and d.duplicate_of:
                        entry["live_edge_id"] = d.duplicate_of
                    skips.append(entry)

        overwritten_with_attachments: List[str] = []
        created_over_orphans: List[str] = []
        attachments_checked = True
        for d in node_overwrites + node_creates:
            flag = self.view.has_attachments(d.node.id)
            if flag is None:
                attachments_checked = False
                break
            if flag:
                (
                    overwritten_with_attachments
                    if d.outcome == "overwrite"
                    else created_over_orphans
                ).append(d.node.id)

        to_embed = (
            len(node_creates)
            + len(node_renames)
            + sum(
                1
                for d in node_overwrites
                if any(name in d.changes for name in EMBEDDING_FIELDS)
            )
        )
        events = sum(
            len(group)
            for group in (
                node_creates,
                node_overwrites,
                node_renames,
                edge_creates,
                edge_overwrites,
                edge_renames,
            )
        )

        return {
            "adds": {
                "nodes": _capped_ids([d.node.id for d in node_creates]),
                "edges": _capped_ids([d.edge.id for d in edge_creates]),
            },
            "overwrites": {
                "nodes": _capped_items(
                    [{"id": d.node.id, "changes": d.changes} for d in node_overwrites]
                ),
                "edges": _capped_items(
                    [{"id": d.edge.id, "changes": d.changes} for d in edge_overwrites]
                ),
            },
            "renames": {
                "nodes": _capped_items(
                    [
                        {
                            "id": d.node.id,
                            "edges_rewritten": rewrites.get(d.node.id, []),
                        }
                        for d in node_renames
                    ]
                ),
                "edges": _capped_ids([d.edge.id for d in edge_renames]),
            },
            "skips": _capped_items(skips),
            "conflicts": _capped_items(
                sorted(self.conflicts, key=lambda c: (c["kind"], c["id"], c["reason"]))
            ),
            "rejected": _capped_items([issue.to_dict() for issue in self.rejected]),
            "attachments": {
                "checked": attachments_checked,
                "overwritten_with_attachments": _capped_ids(
                    overwritten_with_attachments if attachments_checked else []
                ),
                "created_over_orphan_attachments": _capped_ids(
                    created_over_orphans if attachments_checked else []
                ),
            },
            "edges_to_archived_nodes": _capped_ids(sorted(self.archived_attach_edges)),
            "embeddings": {
                "nodes_to_embed": to_embed,
                "live_nodes_without_vector": live_without_vector,
            },
            "events": {"count": events},
        }

    def _digest(self) -> str:
        def endpoint(end: Optional[_Endpoint]) -> Any:
            if end is None:
                return None
            return [end.kind, end.node_id]

        canonical = {
            "digest_version": DIGEST_VERSION,
            "parameters": self.params.canonical(),
            "generation": self.generation,
            "nodes": [
                {
                    "id": d.node.id,
                    "outcome": d.outcome,
                    "reason": d.reason,
                    "content": _content(self.raw_nodes[d.index]),
                }
                for d in sorted(self.nodes.values(), key=lambda d: d.node.id)
            ],
            "edges": [
                {
                    "id": d.edge.id,
                    "outcome": d.outcome,
                    "reason": d.reason,
                    "source": endpoint(d.source),
                    "target": endpoint(d.target),
                    "duplicate_of": d.duplicate_of,
                    "content": _content(self.raw_edges[d.index]),
                }
                for d in sorted(self.edges, key=lambda d: d.edge.id)
            ],
            "conflicts": sorted(
                [c["kind"], c["id"], c["reason"], c["location"]] for c in self.conflicts
            ),
            "rejected": sorted(
                [issue.location, issue.message] for issue in self.rejected
            ),
            "reads": sorted(
                [kind, entity_id, revision]
                for (kind, entity_id), revision in self.reads.items()
            ),
        }
        encoded = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _content(raw: Dict[str, Any]) -> Dict[str, Any]:
    # ``revision`` is server-owned and ignored on every write, so a document
    # carrying a different one asks for the same writes.
    return {key: value for key, value in raw.items() if key != "revision"}


def _field_changes(live: Any, incoming: Any, fields: Tuple[str, ...]) -> Dict[str, Any]:
    changes: Dict[str, Dict[str, Any]] = {}
    for name in fields:
        main = getattr(live, name)
        new = getattr(incoming, name)
        if name == "type":
            main, new = live.type_str, incoming.type_str
        if main != new:
            changes[name] = {"main": main, "incoming": new}
    return changes


def _capped_ids(ids: List[str]) -> Dict[str, Any]:
    return {"count": len(ids), "ids": ids[:PLAN_LIST_CAP]}


def _capped_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"count": len(items), "items": items[:PLAN_LIST_CAP]}
