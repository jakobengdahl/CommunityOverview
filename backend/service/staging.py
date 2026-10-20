"""Staged writes (docs/SESSION_OVERLAY_CONTRACT.md §6, §9).

In a staged session every graph write goes to the session's layer instead of
the graph: no node, edge or graph metadata changes and no graph event fires
(I1). The write is validated exactly as a direct one would be, but against the
**composed** view - the graph with the layer applied (§6.3) - and the layer
keeps at most one entry per entity, the net change (§6.2). Each staging op is
one session op (§15): it advances the session sequence and fans out as
``graph_change_staged``.

``SessionGate`` decides, per call, whether a write lands in the graph or in a
layer, from the session the request acts in (``runtime.session_context``)
and that session's write mode (``core.write_modes``). ``StagedWrite`` does the
staging. Reads that compose (§8) arrive in a later slice; until then a
composing read made in a staged session is refused rather than answered from
the main graph alone.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple

from backend.core import Edge, EntityRevisionConflict, Node, StaleUpdateError
from backend.core import layer_store
from backend.core.models import _parse_datetime
from backend.core.session_annotations import saved_view_annotation_error
from backend.core.session_manager import (
    LayoutBusy,
    SessionNotFound,
    SessionSeqConflict,
)
from backend.core.storage import (
    EDGE_UPDATE_FIELDS,
    _apply_metadata_patch,
    build_edge_update,
    build_node_update,
    edge_applicability_error,
)
from backend.core.write_modes import STAGED_UNSUPPORTED, WRITE_MODE_STAGED
from backend.runtime import session_context
from backend.runtime.authorization import GraphAccessNarrowing

from . import access
from .serializers import serialize_edge, serialize_node

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from backend.core.session_manager import SessionManager

LayerKey = Tuple[str, str]

# The node fields an update or an archive change stages (§6.3): what a direct
# node update accepts, plus the archived flag.
_NODE_STAGED_FIELDS = (
    "name",
    "description",
    "summary",
    "tags",
    "subtypes",
    "aliases",
    "metadata",
    "archived",
)
# A node re-created over its own tombstone stages every content field (§6.2),
# its type included.
_NODE_RECREATED_FIELDS = ("type",) + _NODE_STAGED_FIELDS
_EDGE_STAGED_FIELDS = tuple(sorted(EDGE_UPDATE_FIELDS)) + ("archived",)
_EDGE_RECREATED_FIELDS = ("source", "target") + _EDGE_STAGED_FIELDS

_SAVED_VIEW_NODE_TYPES = frozenset({"SavedView", "VisualizationView"})

SESSION_SEQ_WITHOUT_SESSION = "session_seq_without_session"
BUSY = "busy"
INVALID_ENTITY = "invalid_entity"


class StagingRefused(Exception):
    """A staged write was refused; nothing was staged."""

    def __init__(self, code: str, message: str, **extra: Any):
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)

    def to_result(self, **defaults: Any) -> Dict[str, Any]:
        return {
            **defaults,
            "success": False,
            "error": self.code,
            "message": self.message,
            **self.extra,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc(entity: Any) -> Dict[str, Any]:
    """An entity as a layer holds it: its ``to_dict()``, without a vector."""
    data = entity.to_dict()
    data.pop("embedding", None)
    return data


def _apply_staged(doc: Dict[str, Any], staged: Dict[str, Any]) -> Dict[str, Any]:
    """``doc`` with an update's staged fields applied (§6.1): `metadata` is a
    merge-patch, every other field replaces the value outright."""
    result = copy.deepcopy(doc)
    for field, value in staged.items():
        if field == "metadata":
            result["metadata"] = _apply_metadata_patch(
                dict(result.get("metadata") or {}), value or {}
            )
        else:
            result[field] = copy.deepcopy(value)
    return result


def compose(
    entry: Optional[Dict[str, Any]], main: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The composed entity for one id (§8.1), from its entry and the main
    graph's entity (both as dicts)."""
    if entry is None:
        return main
    action = entry.get("action")
    if action == "create":
        return copy.deepcopy(entry["staged"])
    if action == "update":
        if main is not None:
            return _apply_staged(main, entry["staged"])
        orphaned = _apply_staged(entry["base"], entry["staged"])
        orphaned["revision"] = 0
        return orphaned
    return None


def _metadata_patch(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """The merge-patch taking ``before`` to ``after``: changed keys with their
    new value, removed keys with None."""
    patch = {k: v for k, v in after.items() if before.get(k, _ABSENT) != v}
    patch.update({k: None for k in before if k not in after})
    return patch


_ABSENT = object()


class Layer:
    """A session's layer as one staging op sees it: the entries loaded at the
    start of the op, the changes the op makes, and composed lookups over both
    and the main graph."""

    def __init__(self, storage: "GraphStorage", entries: Iterable[Dict[str, Any]]):
        self._storage = storage
        self._loaded: Dict[LayerKey, Dict[str, Any]] = {
            (e["kind"], e["entity_id"]): e for e in entries
        }
        self._changed: Dict[LayerKey, Optional[Dict[str, Any]]] = {}

    # -- entries ---------------------------------------------------------------

    def entry(self, key: LayerKey) -> Optional[Dict[str, Any]]:
        if key in self._changed:
            return self._changed[key]
        return self._loaded.get(key)

    def put(self, key: LayerKey, entry: Optional[Dict[str, Any]]) -> None:
        self._changed[key] = entry

    def current_entries(self) -> Dict[LayerKey, Dict[str, Any]]:
        merged = dict(self._loaded)
        for key, entry in self._changed.items():
            if entry is None:
                merged.pop(key, None)
            else:
                merged[key] = entry
        return merged

    def upserts(self) -> List[Dict[str, Any]]:
        return [entry for entry in self._changed.values() if entry is not None]

    def removals(self) -> List[LayerKey]:
        return [
            key
            for key, entry in self._changed.items()
            if entry is None and key in self._loaded
        ]

    # -- the main graph ---------------------------------------------------------

    def main_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        node = self._storage.get_node(node_id)
        return _doc(node) if node is not None else None

    def main_edge(self, edge_id: str) -> Optional[Dict[str, Any]]:
        edge = self._storage.edges.get(edge_id)
        return _doc(edge) if edge is not None else None

    # -- the composed view (§8.1) -------------------------------------------------

    def node(self, node_id: str) -> Optional[Dict[str, Any]]:
        return compose(self.entry(("node", node_id)), self.main_node(node_id))

    def edge(self, edge_id: str) -> Optional[Dict[str, Any]]:
        """The composed edge, absent when either endpoint is absent."""
        doc = compose(self.entry(("edge", edge_id)), self.main_edge(edge_id))
        if doc is None:
            return None
        if self.node(doc["source"]) is None or self.node(doc["target"]) is None:
            return None
        return doc

    def incident_edges(self, node_id: str) -> List[str]:
        """Ids of the composed view's edges incident to ``node_id``."""
        candidates = {edge.id for edge in self._storage.get_edges_for_node(node_id)}
        for (kind, entity_id), entry in self.current_entries().items():
            if kind != "edge" or entry.get("action") == "delete":
                continue
            doc = self.edge_ignoring_endpoints(entity_id)
            if doc is not None and node_id in (doc["source"], doc["target"]):
                candidates.add(entity_id)
        incident = []
        for edge_id in sorted(candidates):
            # As composed: a main-graph edge re-created over its tombstone may
            # have other endpoints now.
            doc = self.edge_ignoring_endpoints(edge_id)
            if doc is not None and node_id in (doc["source"], doc["target"]):
                incident.append(edge_id)
        return incident

    def edge_ignoring_endpoints(self, edge_id: str) -> Optional[Dict[str, Any]]:
        return compose(self.entry(("edge", edge_id)), self.main_edge(edge_id))

    def nodes(self) -> Iterable[Dict[str, Any]]:
        """Every node of the composed view."""
        entries = self.current_entries()
        for node_id, node in self._storage.nodes.items():
            if ("node", node_id) not in entries:
                yield _doc(node)
        for (kind, entity_id), entry in entries.items():
            if kind == "node":
                doc = compose(entry, self.main_node(entity_id))
                if doc is not None:
                    yield doc

    def name_to_id(self) -> Dict[str, str]:
        """Composed node names, as a direct edge create resolves endpoints."""
        return {doc["name"]: doc["id"] for doc in self.nodes()}


def _node(doc: Dict[str, Any]) -> Node:
    return Node.from_dict(copy.deepcopy(doc))


def _edge(doc: Dict[str, Any]) -> Edge:
    return Edge.from_dict(copy.deepcopy(doc))


class StagedWrite:
    """Stages one write into one staged session's layer."""

    def __init__(
        self,
        storage: "GraphStorage",
        session_manager: "SessionManager",
        session_id: str,
        *,
        expected_session_seq: Optional[int],
        max_entries: int,
        actor: str,
    ):
        self._storage = storage
        self._manager = session_manager
        self.session_id = session_id
        self._expected_session_seq = expected_session_seq
        self._max_entries = max_entries
        self._actor = actor

    # -- running one op ---------------------------------------------------------

    def _load(self) -> Layer:
        store = self._manager.write_modes.layer_store
        return Layer(self._storage, store.load_layer(self.session_id))

    def _commit(self, layer: Layer) -> Dict[str, Any]:
        """Write the op's changes to the layer and fan them out (§15).

        Returns ``{"entries": [...], "removals": [...], "session_seq": n}``;
        an op that changes nothing stages nothing and emits no op.
        """
        upserts = layer.upserts()
        removals = layer.removals()
        if not upserts and not removals:
            # Nothing to stage, but the caller's expectation is still answered.
            seq = self._manager.check_session_seq(
                self.session_id, self._expected_session_seq
            )
            return {"entries": [], "removals": [], "session_seq": seq}
        store = self._manager.write_modes.layer_store

        staged_entries: List[Dict[str, Any]] = []

        def commit(seq: int) -> Dict[str, Any]:
            stamped = [{**entry, "session_seq": seq} for entry in upserts]
            staged_entries.extend(stamped)
            store.stage(
                self.session_id, stamped, removals, max_entries=self._max_entries
            )
            return {"entries": stamped, "removals": [list(k) for k in removals]}

        applied = self._manager.stage_graph_change(
            self.session_id,
            expected_session_seq=self._expected_session_seq,
            commit=commit,
        )
        return {
            "entries": staged_entries,
            "removals": applied["removals"],
            "session_seq": applied["seq"],
        }

    def _staged(self, response: Dict[str, Any], committed: Dict[str, Any]) -> Dict:
        return {
            **response,
            "staged": True,
            "session_id": self.session_id,
            "session_seq": committed["session_seq"],
            # Without the base: a cascade reaches edges the writer's narrowed
            # view may not show, and the base is the diff's, read with
            # authorization.
            "entries": [
                {k: v for k, v in entry.items() if k != "base"}
                for entry in committed["entries"]
            ],
            "removed_entries": committed["removals"],
        }

    def _entry(
        self,
        kind: str,
        entity_id: str,
        action: str,
        base: Optional[Dict[str, Any]],
        staged: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "kind": kind,
            "entity_id": entity_id,
            "action": action,
            "base_revision": None if base is None else int(base.get("revision") or 0),
            "base": base,
            "staged": staged,
            "staged_by": self._actor,
            "staged_at": _now_iso(),
            "session_seq": None,
        }

    # -- the collapsing rules (§6.2) ----------------------------------------------

    def _stage_create(self, layer: Layer, kind: str, doc: Dict[str, Any]) -> None:
        key = (kind, doc["id"])
        existing = layer.entry(key)
        if existing is not None and existing.get("action") == "delete":
            # Re-created over its own tombstone: an update against the first
            # base, staging every content field, and nulling each metadata key
            # of the base the new entity lacks - so the composed view shows
            # exactly the new entity.
            base = existing["base"]
            fields = (
                _NODE_RECREATED_FIELDS if kind == "node" else _EDGE_RECREATED_FIELDS
            )
            staged = {f: copy.deepcopy(doc.get(f)) for f in fields if f != "metadata"}
            staged["metadata"] = _metadata_patch(
                base.get("metadata") or {}, doc.get("metadata") or {}
            )
            layer.put(key, self._entry(kind, doc["id"], "update", base, staged))
            return
        layer.put(key, self._entry(kind, doc["id"], "create", None, doc))

    def _stage_update(
        self,
        layer: Layer,
        kind: str,
        current: Dict[str, Any],
        updated: Dict[str, Any],
        fields: Tuple[str, ...],
    ) -> bool:
        """Stage the change from composed ``current`` to ``updated``.

        Returns False when no staged field changes.
        """
        entity_id = current["id"]
        changes = {
            f: copy.deepcopy(updated.get(f))
            for f in fields
            if f != "metadata" and updated.get(f) != current.get(f)
        }
        if "metadata" in fields:
            patch = _metadata_patch(
                current.get("metadata") or {}, updated.get("metadata") or {}
            )
            if patch:
                changes["metadata"] = patch
        if not changes:
            return False
        key = (kind, entity_id)
        existing = layer.entry(key)
        if existing is None:
            base = (
                layer.main_node(entity_id)
                if kind == "node"
                else layer.main_edge(entity_id)
            )
            layer.put(key, self._entry(kind, entity_id, "update", base, changes))
        elif existing["action"] == "create":
            layer.put(
                key,
                {
                    **existing,
                    "staged": updated,
                    "staged_by": self._actor,
                    "staged_at": _now_iso(),
                },
            )
        else:
            staged = dict(existing["staged"])
            for field, value in changes.items():
                if field == "metadata":
                    staged["metadata"] = {**(staged.get("metadata") or {}), **value}
                else:
                    staged[field] = value
            layer.put(
                key,
                {
                    **existing,
                    "staged": staged,
                    "staged_by": self._actor,
                    "staged_at": _now_iso(),
                },
            )
        return True

    def _stage_delete(self, layer: Layer, kind: str, entity_id: str) -> None:
        key = (kind, entity_id)
        existing = layer.entry(key)
        if existing is not None and existing["action"] == "create":
            # Never in the graph: the entry goes, and nothing is left to do.
            layer.put(key, None)
            return
        if existing is not None:
            base = existing["base"]
        else:
            base = (
                layer.main_node(entity_id)
                if kind == "node"
                else layer.main_edge(entity_id)
            )
        layer.put(key, self._entry(kind, entity_id, "delete", base, None))

    def _delete_node(self, layer: Layer, node_id: str) -> List[str]:
        """Tombstone a node and, in the same op, every composed edge incident to
        it (§6.3). Returns the edge ids."""
        edges = layer.incident_edges(node_id)
        for edge_id in edges:
            self._stage_delete(layer, "edge", edge_id)
        self._stage_delete(layer, "node", node_id)
        return edges

    # -- expectations -------------------------------------------------------------

    @staticmethod
    def _expect_revision(
        kind: str, entity_id: str, doc: Optional[Dict[str, Any]], expected: int
    ) -> None:
        """§9.2: an entity expectation, checked against the composed entity's
        revision - the main graph's, since staging never advances it."""
        current = None if doc is None else int(doc.get("revision") or 0)
        if current != expected:
            raise EntityRevisionConflict(kind, entity_id, expected, current)

    @staticmethod
    def _visible(doc: Optional[Dict[str, Any]], graph_access: GraphAccessNarrowing):
        return doc is not None and access.is_node_visible(_node(doc), graph_access)

    # -- the writes (§9.1) ----------------------------------------------------------

    def add_nodes(
        self,
        nodes: List[Node],
        edges: List[Edge],
        *,
        expect_absent: bool,
        graph_access: GraphAccessNarrowing,
    ) -> Dict[str, Any]:
        layer = self._load()
        failure = {"added_node_ids": [], "added_edge_ids": []}
        for node in nodes:
            if expect_absent and layer.node(node.id) is not None:
                raise EntityRevisionConflict("node", node.id, None, None)
            if layer.node(node.id) is not None:
                return {
                    **failure,
                    "success": False,
                    "message": f"Node with ID {node.id} already exists",
                }
            doc = _doc(node)
            doc["revision"] = 0
            self._stage_create(layer, "node", doc)
        names = layer.name_to_id() if edges else {}
        for edge in edges:
            endpoints = []
            for role, value in (("Source", edge.source), ("Target", edge.target)):
                if layer.node(value) is None:
                    if value not in names:
                        return {
                            **failure,
                            "success": False,
                            "message": (
                                f"{role} node '{value}' does not exist "
                                "(not found by ID or name)"
                            ),
                        }
                    value = names[value]
                endpoints.append(value)
            edge.source, edge.target = endpoints
            if graph_access.enabled and not all(
                self._visible(layer.node(nid), graph_access) for nid in endpoints
            ):
                return {
                    **failure,
                    "success": False,
                    "message": "Could not add edge (source or target not found)",
                }
            if expect_absent and layer.edge_ignoring_endpoints(edge.id) is not None:
                raise EntityRevisionConflict("edge", edge.id, None, None)
            if layer.edge_ignoring_endpoints(edge.id) is not None:
                return {
                    **failure,
                    "success": False,
                    "message": f"Edge with ID {edge.id} already exists",
                }
            error = edge_applicability_error(
                edge.type_str,
                _node(layer.node(edge.source)),
                _node(layer.node(edge.target)),
            )
            if error:
                return {
                    **failure,
                    "success": False,
                    "message": f"Error validating input: {error}",
                }
            doc = _doc(edge)
            doc["revision"] = 0
            self._stage_create(layer, "edge", doc)
        committed = self._commit(layer)
        added_nodes = [_node(layer.node(n.id)) for n in nodes]
        added_edges = [_edge(layer.edge_ignoring_endpoints(e.id)) for e in edges]
        return self._staged(
            {
                "success": True,
                "message": f"Staged {len(nodes)} nodes and {len(edges)} edges",
                "added_node_ids": [n.id for n in nodes],
                "added_edge_ids": [e.id for e in edges],
                "nodes": [serialize_node(n) for n in added_nodes],
                "edges": [serialize_edge(e) for e in added_edges],
                "action": "add_to_visualization",
            },
            committed,
        )

    def update_node(
        self,
        node_id: str,
        updates: Dict[str, Any],
        *,
        metadata_merge: bool,
        expected_updated_at: Optional[str],
        expected_revision: Optional[int],
        graph_access: GraphAccessNarrowing,
    ) -> Dict[str, Any]:
        layer = self._load()
        current = layer.node(node_id)
        if graph_access.enabled and not self._visible(current, graph_access):
            current = None
        if expected_revision is not None:
            self._expect_revision("node", node_id, current, expected_revision)
        if current is None:
            return {"success": False, "error": f"Node with ID {node_id} not found"}
        if expected_updated_at is not None:
            current_node = _node(current)
            expected = (
                _parse_datetime(expected_updated_at)
                if isinstance(expected_updated_at, str)
                else expected_updated_at
            )
            if current_node.updated_at != expected:
                raise StaleUpdateError(
                    node_id, expected_updated_at, current_node.updated_at
                )
        if current.get("type") in _SAVED_VIEW_NODE_TYPES and isinstance(
            updates.get("metadata"), dict
        ):
            annotation_error = saved_view_annotation_error(updates["metadata"])
            if annotation_error:
                return {
                    "success": False,
                    "error": f"Error validating input: {annotation_error}",
                    "error_code": "validation_error",
                }
        updated = _doc(
            build_node_update(current, updates, metadata_merge=metadata_merge)
        )
        self._stage_update(layer, "node", current, updated, _NODE_STAGED_FIELDS)
        committed = self._commit(layer)
        node = serialize_node(_node(layer.node(node_id)))
        return self._staged(
            {
                "success": True,
                "node": node,
                "nodes": [node],
                "action": "update_in_visualization",
            },
            committed,
        )

    def delete_nodes(
        self,
        node_ids: List[str],
        *,
        expected_revisions: Optional[Dict[str, int]],
        graph_access: GraphAccessNarrowing,
    ) -> Dict[str, Any]:
        layer = self._load()
        present = [
            nid
            for nid in dict.fromkeys(node_ids)
            if layer.node(nid) is not None
            and (
                not graph_access.enabled or self._visible(layer.node(nid), graph_access)
            )
        ]
        for node_id, expected in (expected_revisions or {}).items():
            self._expect_revision(
                "node",
                node_id,
                layer.node(node_id) if node_id in present else None,
                expected,
            )
        affected: List[str] = []
        for node_id in present:
            for edge_id in self._delete_node(layer, node_id):
                if edge_id not in affected:
                    affected.append(edge_id)
        committed = self._commit(layer)
        return self._staged(
            {
                "success": True,
                "deleted_node_ids": present,
                "affected_edge_ids": affected,
                "message": f"Staged the deletion of {len(present)} nodes and "
                f"{len(affected)} edges",
            },
            committed,
        )

    def set_nodes_archived(
        self,
        node_ids: List[str],
        archived: bool,
        *,
        graph_access: GraphAccessNarrowing,
    ) -> Dict[str, Any]:
        layer = self._load()
        found: List[str] = []
        for node_id in dict.fromkeys(node_ids):
            current = layer.node(node_id)
            if current is None or (
                graph_access.enabled and not self._visible(current, graph_access)
            ):
                continue
            found.append(node_id)
            self._stage_update(
                layer, "node", current, {**current, "archived": archived}, ("archived",)
            )
        committed = self._commit(layer)
        nodes = [serialize_node(_node(layer.node(nid))) for nid in found]
        return self._staged(
            {
                "success": True,
                "archived": archived,
                "node_ids": found,
                "nodes": nodes,
                "action": "update_in_visualization",
            },
            committed,
        )

    def add_edge(
        self, edge: Edge, *, graph_access: GraphAccessNarrowing
    ) -> Dict[str, Any]:
        layer = self._load()
        not_found = {
            "success": False,
            "message": "Could not add edge (source or target not found)",
        }
        names = None
        endpoints = []
        for value in (edge.source, edge.target):
            if layer.node(value) is None:
                names = names if names is not None else layer.name_to_id()
                if value not in names:
                    return not_found
                value = names[value]
            if graph_access.enabled and not self._visible(
                layer.node(value), graph_access
            ):
                return not_found
            endpoints.append(value)
        edge.source, edge.target = endpoints
        if layer.edge_ignoring_endpoints(edge.id) is not None:
            return not_found
        error = edge_applicability_error(
            edge.type_str,
            _node(layer.node(edge.source)),
            _node(layer.node(edge.target)),
        )
        if error:
            return {"success": False, "message": error}
        doc = _doc(edge)
        doc["revision"] = 0
        self._stage_create(layer, "edge", doc)
        committed = self._commit(layer)
        staged = serialize_edge(_edge(layer.edge_ignoring_endpoints(edge.id)))
        return self._staged(
            {
                "success": True,
                "edge": staged,
                "edges": [staged],
                "action": "add_to_visualization",
            },
            committed,
        )

    def _visible_edge(
        self, layer: Layer, edge_id: str, graph_access: GraphAccessNarrowing
    ) -> Optional[Dict[str, Any]]:
        doc = layer.edge(edge_id)
        if doc is None or not graph_access.enabled:
            return doc
        if self._visible(layer.node(doc["source"]), graph_access) and self._visible(
            layer.node(doc["target"]), graph_access
        ):
            return doc
        return None

    def update_edge(
        self,
        edge_id: str,
        updates: Dict[str, Any],
        *,
        expected_revision: Optional[int],
        graph_access: GraphAccessNarrowing,
    ) -> Dict[str, Any]:
        layer = self._load()
        current = self._visible_edge(layer, edge_id, graph_access)
        if expected_revision is not None:
            self._expect_revision("edge", edge_id, current, expected_revision)
        if current is None:
            return {"success": False, "error": f"Edge with ID {edge_id} not found"}
        updated = build_edge_update(current, updates)
        error = edge_applicability_error(
            updated.type_str,
            _node(layer.node(current["source"])),
            _node(layer.node(current["target"])),
        )
        if error:
            raise ValueError(error)
        self._stage_update(
            layer, "edge", current, _doc(updated), tuple(sorted(EDGE_UPDATE_FIELDS))
        )
        committed = self._commit(layer)
        staged = serialize_edge(_edge(layer.edge(edge_id)))
        return self._staged({"success": True, "edge": staged}, committed)

    def delete_edge(
        self,
        edge_id: str,
        *,
        expected_revision: Optional[int],
        graph_access: GraphAccessNarrowing,
    ) -> Dict[str, Any]:
        layer = self._load()
        current = self._visible_edge(layer, edge_id, graph_access)
        if expected_revision is not None:
            self._expect_revision("edge", edge_id, current, expected_revision)
        if current is None:
            return {"success": False, "error": f"Edge with ID {edge_id} not found"}
        self._stage_delete(layer, "edge", edge_id)
        committed = self._commit(layer)
        return self._staged({"success": True, "deleted_edge_id": edge_id}, committed)

    def delete_edges(
        self, edge_ids: List[str], *, graph_access: GraphAccessNarrowing
    ) -> Dict[str, Any]:
        layer = self._load()
        deleted = [
            eid
            for eid in dict.fromkeys(edge_ids)
            if self._visible_edge(layer, eid, graph_access) is not None
        ]
        for edge_id in deleted:
            self._stage_delete(layer, "edge", edge_id)
        committed = self._commit(layer)
        return self._staged(
            {
                "success": True,
                "deleted_edge_ids": deleted,
                "message": f"Staged the deletion of {len(deleted)} edges",
            },
            committed,
        )

    def set_edges_archived(
        self,
        edge_ids: List[str],
        archived: bool,
        *,
        graph_access: GraphAccessNarrowing,
    ) -> Dict[str, Any]:
        layer = self._load()
        found: List[str] = []
        for edge_id in dict.fromkeys(edge_ids):
            current = self._visible_edge(layer, edge_id, graph_access)
            if current is None:
                continue
            found.append(edge_id)
            self._stage_update(
                layer, "edge", current, {**current, "archived": archived}, ("archived",)
            )
        committed = self._commit(layer)
        edges = [serialize_edge(_edge(layer.edge(eid))) for eid in found]
        return self._staged(
            {
                "success": True,
                "archived": archived,
                "edge_ids": found,
                "edges": edges,
                "action": "update_in_visualization",
            },
            committed,
        )

    # -- federation adoption --------------------------------------------------------

    def find_adopted(self, federated_node_id: str) -> Optional[Node]:
        """The composed view's node already adopted from ``federated_node_id``."""
        for doc in self._load().nodes():
            adopted_from = (doc.get("metadata") or {}).get("adopted_from") or {}
            if adopted_from.get("federated_node_id") == federated_node_id:
                return _node(doc)
        return None

    def node_exists(self, node_id: str) -> bool:
        return self._load().node(node_id) is not None


class SessionGate:
    """Decides, per call, whether a graph operation acts in a staged session.

    ``session_manager`` is None where sessions are not wired (a GraphService
    built on its own): then no session id resolves, and a request naming one
    is refused rather than handled as if it named none.
    """

    def __init__(
        self,
        storage: "GraphStorage",
        session_manager: Optional["SessionManager"] = None,
        *,
        push_target_known: Optional[Callable[[str], bool]] = None,
    ):
        self._storage = storage
        self._manager = session_manager
        self._push_target_known = push_target_known

    @property
    def session_manager(self) -> Optional["SessionManager"]:
        return self._manager

    def _mode(self, session_id: str) -> str:
        """The write mode of ``session_id``, or ``session_not_found`` (§16.1)."""
        if self._manager is None:
            raise session_context.not_found(session_id)
        status = self._manager.write_modes.staged_status(session_id)
        if status == layer_store.STATUS_RETIRED:
            raise session_context.not_found(session_id)
        if status == layer_store.STATUS_ACTIVE:
            return WRITE_MODE_STAGED
        session = self._manager.get_session(session_id)
        if (
            session is not None
            and session.write_mode == WRITE_MODE_STAGED
            and not self._manager.write_modes.supports_staged
        ):
            # Staged once, on a deployment that can no longer read its record:
            # refused, never treated as direct (I2).
            raise StagingRefused(
                STAGED_UNSUPPORTED,
                f"session {session_id!r} is staged, and staged sessions are not "
                "enabled on this deployment's graph backend",
            )
        known = session is not None or bool(
            self._push_target_known and self._push_target_known(session_id)
        )
        if not known:
            raise session_context.not_found(session_id)
        return "direct"

    def for_write(
        self, target: str, expected_session_seq: Optional[int] = None
    ) -> Optional[StagedWrite]:
        """The staging a write in the current request goes through, or None
        for a direct write. Raises SessionRefused or StagingRefused."""
        session_id = session_context.current_session_id()
        if session_id is None:
            if expected_session_seq is not None:
                raise StagingRefused(
                    SESSION_SEQ_WITHOUT_SESSION,
                    "expected_session_seq needs the session it is a sequence of",
                )
            return None
        mode = self._mode(session_id)
        if mode != WRITE_MODE_STAGED:
            if expected_session_seq is not None:
                session = self._manager.get_session(session_id)
                actual = session.seq if session is not None else 0
                if expected_session_seq != actual:
                    raise SessionSeqConflict(expected_session_seq, actual)
            return None
        actor = access.build_mutation_actor_id(target=target)
        return StagedWrite(
            self._storage,
            self._manager,
            session_id,
            expected_session_seq=expected_session_seq,
            max_entries=self._manager.write_modes.max_entries,
            actor=actor,
        )

    def check_read(self, target: str) -> None:
        """Refuse a composing read made in a staged session (§8 is not built
        yet), and one naming a session that does not resolve."""
        session_id = session_context.current_session_id()
        if session_id is None:
            return
        if self._mode(session_id) == WRITE_MODE_STAGED:
            raise session_context.SessionRefused(
                session_context.SESSION_NOT_SUPPORTED,
                f"{target} does not yet read a staged session's composed view; "
                "send the request without the session",
            )


def refusal_result(exc: Exception, **defaults: Any) -> Optional[Dict[str, Any]]:
    """The result a session or staging refusal answers with, or None when
    ``exc`` is not one."""
    if isinstance(exc, session_context.SessionRefused):
        return {**defaults, **exc.to_result()}
    if isinstance(exc, StagingRefused):
        return exc.to_result(**defaults)
    if isinstance(exc, SessionSeqConflict):
        return {
            **defaults,
            "success": False,
            "error": exc.code,
            "message": str(exc),
            "expected_session_seq": exc.expected,
            "session_seq": exc.actual,
        }
    if isinstance(exc, layer_store.LayerFull):
        return {
            **defaults,
            "success": False,
            "error": exc.code,
            "message": str(exc),
            "limit": exc.limit,
        }
    if isinstance(exc, (layer_store.StagedSessionNotActive, SessionNotFound)):
        # Retired, or its document gone, since the write was routed.
        session_id = getattr(exc, "session_id", None) or (
            session_context.current_session_id() or ""
        )
        return {**defaults, **session_context.not_found(session_id).to_result()}
    if isinstance(exc, LayoutBusy):
        return {
            **defaults,
            "success": False,
            "error": BUSY,
            "message": "Another change is being applied to this session; retry.",
        }
    if isinstance(exc, layer_store.InvalidLayerRequest):
        return {
            **defaults,
            "success": False,
            "error": INVALID_ENTITY,
            "message": f"Error validating input: {exc}",
        }
    return None
