"""Review, merge and discard of a staged session's changes
(docs/SESSION_OVERLAY_CONTRACT.md §10–§12).

``Review`` reads a layer against the main graph: one item per entry, with
each staged field's base, staged and main value, its status (§10) and the
entries it cannot be merged without (§11.4). ``plan_merge`` turns a selection
and its resolutions (§11.2) into the merge unit the layer store applies
(§7, §11.3): every write with its expectation, every check the decision
rested on, the entries it removes and the record it stores.

A reader narrowed by the authorization hook sees only the entries its view
wholly covers - the rule composed reads apply (``composed``): an entry's
composed entity, the main entity it stands over and its base must all be in
view, and for an edge their endpoints too. An entry that depends on one
outside the view is outside it as well, so a narrowed caller can neither see
nor select it; nothing about such an entry - not its key, not its count - is
answered. Everything here reads the main graph from memory; the store
re-checks every condition inside the merge unit (§11.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

from backend.core import Edge, Node
from backend.core.layer_store import MergeCheck, entry_version
from backend.core.models import metadata_depth_error
from backend.core.session_annotations import (
    sanitize_saved_view_metadata,
    saved_view_write_error,
)
from backend.core.storage import edge_applicability_error
from backend.core.storage_merge import SessionMergePlan, SessionMergeWrite
from backend.runtime.authorization import GraphAccessNarrowing

from .composed import doc_visible, main_endpoint_shown
from .staging import _apply_staged, _doc, compose, json_copy

if TYPE_CHECKING:
    from backend.core import GraphStorage

LayerKey = Tuple[str, str]

STATUS_CLEAN = "clean"
STATUS_CONFLICT = "conflict"
STATUS_ORPHANED = "orphaned"
STATUS_ID_TAKEN = "id_taken"
STATUS_DANGLING = "dangling"

TAKE_SESSION = "take_session"
KEEP_MAIN = "keep_main"
AS_NEW = "as_new"
MANUAL = "manual"
RESOLUTIONS = (TAKE_SESSION, KEEP_MAIN, AS_NEW, MANUAL)

MERGE_BLOCKED = "merge_blocked"
MERGE_RACED = "merge_raced"
MERGE_INCOMPLETE = "merge_incomplete"
INVALID_MERGE_REQUEST = "invalid_merge_request"

# What a field-by-field comparison leaves out: the id names the entity, the
# revision and timestamps are stamps of writes, not content (§11.1).
_NOT_CONTENT = frozenset({"id", "revision", "created_at", "updated_at", "embedding"})
# A delete conflicts on any difference but these (§11.1).
_DELETE_IGNORED = frozenset({"revision", "updated_at", "embedding"})

_METADATA_PREFIX = "metadata."

_SAVED_VIEW_TYPES = frozenset({"SavedView", "VisualizationView"})
# The fields a saved view's annotations live in, as the diff names them.
_ANNOTATION_FIELDS = ("metadata.annotations", "metadata.annotation_document")


class ReviewRefused(Exception):
    """A merge or discard was refused; nothing was written."""

    def __init__(self, code: str, message: str, status: int, **extra: Any):
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra
        super().__init__(message)

    def to_result(self) -> Dict[str, Any]:
        return {
            "success": False,
            "error": self.code,
            "message": self.message,
            **self.extra,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fields(doc: Optional[Dict[str, Any]], exclude: Iterable[str]) -> Dict[str, Any]:
    """An entity as fields (§2): its top-level attributes, and each top-level
    ``metadata`` key as a field of its own, named ``metadata.<key>``."""
    if doc is None:
        return {}
    skip = set(exclude)
    out = {k: v for k, v in doc.items() if k != "metadata" and k not in skip}
    for key, value in (doc.get("metadata") or {}).items():
        out[_METADATA_PREFIX + key] = value
    return out


def _staged_fields(staged: Dict[str, Any]) -> Dict[str, Any]:
    """An update's staged fields: a metadata key set to None removes it, and
    a removed key reads as null, which is what an absent field counts as."""
    return _fields(staged, ())


def _main_doc(storage: "GraphStorage", key: LayerKey) -> Optional[Dict[str, Any]]:
    kind, entity_id = key
    entity = (
        storage.get_node(entity_id) if kind == "node" else storage.edges.get(entity_id)
    )
    return None if entity is None else _doc(entity)


def _revision(doc: Optional[Dict[str, Any]]) -> Optional[int]:
    return None if doc is None else int(doc.get("revision") or 0)


class Review:
    """A layer read against the main graph, for one reader (§10).

    ``in_view`` is the set of entries the reader may see and select;
    ``items`` describes them, with each edge's ``dangling`` judged as if
    every one of them were merged with no resolution.
    """

    def __init__(
        self,
        storage: "GraphStorage",
        entries: Iterable[Dict[str, Any]],
        graph_access: Optional[GraphAccessNarrowing] = None,
    ):
        self.storage = storage
        self.narrowing = graph_access or GraphAccessNarrowing()
        self.entries: Dict[LayerKey, Dict[str, Any]] = {
            (e["kind"], e["entity_id"]): e for e in entries
        }
        self.main: Dict[LayerKey, Optional[Dict[str, Any]]] = {
            key: _main_doc(storage, key) for key in self.entries
        }
        self.depends_on: Dict[LayerKey, List[LayerKey]] = {
            key: self._dependencies(key) for key in self.entries
        }
        shown = {key for key in self.entries if self._own_shown(key)}
        # An entry that cannot be merged without one outside the view is
        # outside it too; repeated until nothing more drops out.
        changed = True
        while changed:
            changed = False
            for key in sorted(shown):
                if any(dep not in shown for dep in self.depends_on[key]):
                    shown.discard(key)
                    changed = True
        self.in_view: Set[LayerKey] = shown

    # -- what an entry composes to -------------------------------------------

    def composed(self, key: LayerKey) -> Optional[Dict[str, Any]]:
        """The entity the entry composes to, its endpoints ignored; for an
        update whose entity main no longer holds, its base with the staged
        fields applied (§8.1)."""
        return compose(self.entries[key], self.main[key])

    def endpoints(self, key: LayerKey) -> Tuple[str, ...]:
        doc = self.composed(key)
        if doc is None:
            return ()
        return (doc.get("source") or "", doc.get("target") or "")

    def _dependencies(self, key: LayerKey) -> List[LayerKey]:
        """§11.4: an edge written by the merge needs the create entries of its
        endpoints; a node delete needs the tombstones of its edges."""
        kind, entity_id = key
        entry = self.entries[key]
        deps: Set[LayerKey] = set()
        if kind == "edge" and entry["action"] != "delete":
            for end in self.endpoints(key):
                dep = self.entries.get(("node", end))
                if dep is not None and dep["action"] == "create":
                    deps.add(("node", end))
        # A node delete takes its edges' tombstones with it, and so does a
        # re-create of the node as another type (an update staging `type`):
        # the old node's edges are gone in the composed view, and some may
        # not fit the new type.
        recreated = entry["action"] == "update" and "type" in (entry["staged"] or {})
        if kind == "node" and (entry["action"] == "delete" or recreated):
            for edge_key, edge_entry in self.entries.items():
                if edge_key[0] != "edge" or edge_entry["action"] != "delete":
                    continue
                snapshots = (edge_entry.get("base"), self.main[edge_key])
                if any(
                    doc is not None
                    and entity_id in (doc.get("source"), doc.get("target"))
                    for doc in snapshots
                ):
                    deps.add(edge_key)
        return sorted(deps)

    # -- narrowing -----------------------------------------------------------------

    def _own_shown(self, key: LayerKey) -> bool:
        """Whether the reader's view wholly covers the entry itself: what it
        composes to, the main entity and the base, and for an edge each of
        their endpoints (the S4 rule)."""
        narrowing = self.narrowing
        if not narrowing.enabled:
            return True
        entry = self.entries[key]
        main = self.main[key]
        base = entry.get("base")
        composed = self.composed(key)
        if key[0] == "node":
            retyped = (
                composed is not None
                and main is not None
                and composed.get("type") != main.get("type")
            )
            if retyped:
                # A type change in place is judged against every main edge to
                # the node not deleted with it, and an id_taken create does not
                # depend on the tombstones it might be merged without: any
                # edge the reader cannot see keeps the entry out of its view.
                edges = [e.id for e in self.storage.get_edges_for_node(key[1])]
            elif entry["action"] == "delete" and main is not None:
                # A delete also deletes what main added to the node since; one
                # such edge the reader cannot see keeps the whole entry out of
                # its view, as a hidden tombstone it depends on does.
                edges = self.unstaged_edges(key[1])
            else:
                edges = []
            if not all(self._edge_shown_now(e) for e in edges):
                return False
            return all(
                doc_visible(doc, narrowing)
                for doc in (composed, main, base)
                if doc is not None
            )
        if composed is not None and not all(
            self._composed_node_shown(end) for end in self.endpoints(key)
        ):
            return False
        since = None if base is None else int(entry.get("base_revision") or 0)
        return all(
            main_endpoint_shown(self.storage, narrowing, doc.get(end), since)
            for doc in (main, base)
            if doc is not None
            for end in ("source", "target")
        )

    def _composed_node_shown(self, node_id: str) -> bool:
        key = ("node", node_id)
        if key in self.entries:
            return self._own_shown(key) and self.composed(key) is not None
        return doc_visible(
            self.main.get(key) or _main_doc(self.storage, key), self.narrowing
        )

    # -- the diff (§10) -------------------------------------------------------------

    def field_diff(self, key: LayerKey) -> Dict[str, Dict[str, Any]]:
        entry = self.entries[key]
        main = self.main[key]
        base = entry.get("base")
        action = entry["action"]
        main_fields = _fields(main, ())
        if action == "create":
            return {
                field: {
                    "base": None,
                    "staged": value,
                    "main": main_fields.get(field),
                    "conflict": main is not None and main_fields.get(field) != value,
                }
                for field, value in sorted(
                    _fields(entry["staged"], _NOT_CONTENT).items()
                )
            }
        base_fields = _fields(base, ())
        if action == "update":
            out = {}
            for field, staged in sorted(_staged_fields(entry["staged"]).items()):
                base_value = base_fields.get(field)
                main_value = main_fields.get(field) if main is not None else None
                out[field] = {
                    "base": base_value,
                    "staged": staged,
                    "main": main_value,
                    "conflict": main is not None
                    and main_value != base_value
                    and main_value != staged,
                }
            return out
        if main is None:
            return {}
        before = _fields(base, _DELETE_IGNORED)
        now = _fields(main, _DELETE_IGNORED)
        return {
            field: {
                "base": before.get(field),
                "staged": None,
                "main": now.get(field),
                "conflict": True,
            }
            for field in sorted(set(before) | set(now))
            if before.get(field) != now.get(field)
        }

    def unstaged_edges(self, node_id: str) -> List[str]:
        """Main-graph edges incident to ``node_id`` the layer does not
        tombstone (§11.1)."""
        return sorted(
            edge.id
            for edge in self.storage.get_edges_for_node(node_id)
            if (self.entries.get(("edge", edge.id)) or {}).get("action") != "delete"
        )

    def base_status(self, key: LayerKey) -> str:
        """The status an entry has on its own, before ``dangling`` (§10)."""
        entry = self.entries[key]
        main = self.main[key]
        action = entry["action"]
        if action == "create":
            return STATUS_ID_TAKEN if main is not None else STATUS_CLEAN
        if action == "update":
            if main is None:
                return STATUS_ORPHANED
            conflicted = any(f["conflict"] for f in self.field_diff(key).values())
            return STATUS_CONFLICT if conflicted else STATUS_CLEAN
        if main is None:
            return STATUS_CLEAN
        if self.field_diff(key):
            return STATUS_CONFLICT
        if key[0] == "node" and self.unstaged_edges(key[1]):
            return STATUS_CONFLICT
        return STATUS_CLEAN

    def shown_fields(self, key: LayerKey) -> Dict[str, Dict[str, Any]]:
        """``field_diff`` as answered: a side whose own entity is a saved view
        has its annotations sanitised, as every read of a saved view does. The
        type can differ between base, main and what the session staged, so
        each side is judged by its own. Conflicts are judged on the stored
        values."""
        fields = self.field_diff(key)
        views = {
            side: (doc or {}).get("type") in _SAVED_VIEW_TYPES
            for side, doc in (
                ("base", self.entries[key].get("base")),
                ("main", self.main[key]),
                ("staged", self.composed(key)),
            )
        }
        for name in _ANNOTATION_FIELDS:
            if name not in fields:
                continue
            key_name = name[len(_METADATA_PREFIX) :]
            fields[name] = {
                side: (
                    sanitize_saved_view_metadata({key_name: value})[key_name]
                    if views.get(side) and value is not None
                    else value
                )
                for side, value in fields[name].items()
            }
        return fields

    def item(self, key: LayerKey, status: str) -> Dict[str, Any]:
        entry = self.entries[key]
        main = self.main[key]
        item = {
            "kind": key[0],
            "entity_id": key[1],
            "action": entry["action"],
            "base_revision": entry.get("base_revision"),
            "main_revision": _revision(main),
            "fields": self.shown_fields(key),
            "status": status,
            "depends_on": [
                {"kind": dep[0], "entity_id": dep[1]} for dep in self.depends_on[key]
            ],
            "session_seq": entry.get("session_seq"),
            "staged_by": entry.get("staged_by"),
            "staged_at": entry.get("staged_at"),
        }
        if key[0] == "node" and entry["action"] == "delete" and main is not None:
            item["unstaged_edges"] = [
                edge_id
                for edge_id in self.unstaged_edges(key[1])
                if self._edge_shown_now(edge_id)
            ]
        return item

    def _edge_shown_now(self, edge_id: str) -> bool:
        edge = self.storage.edges.get(edge_id)
        if edge is None or not self.narrowing.enabled:
            return edge is not None
        return all(
            main_endpoint_shown(self.storage, self.narrowing, end, None)
            for end in (edge.source, edge.target)
        )

    def items(self) -> List[Dict[str, Any]]:
        outcome = _Outcomes(self, self.in_view, {})
        return [self.item(key, outcome.status[key]) for key in sorted(self.in_view)]


class _Outcomes:
    """What merging ``selected`` with ``resolutions`` does to each entry:
    its status, and the effect its resolution gives it (``apply``,
    ``drop``, ``noop``, ``as_new`` or ``blocked``)."""

    def __init__(
        self,
        review: Review,
        selected: Iterable[LayerKey],
        resolutions: Dict[LayerKey, Dict[str, Any]],
    ):
        self.review = review
        self.selected = set(selected)
        self.resolutions = resolutions
        self.status: Dict[LayerKey, str] = {}
        self.effect: Dict[LayerKey, str] = {}
        self.blocked: List[LayerKey] = []
        nodes = sorted(k for k in self.selected if k[0] == "node")
        edges = sorted(k for k in self.selected if k[0] == "edge")
        for key in nodes:
            self.status[key] = review.base_status(key)
            self.effect[key] = self._effect(key)
        for key in edges:
            self.status[key] = self._edge_status(key)
            self.effect[key] = self._effect(key)

    def choice(self, key: LayerKey) -> Optional[str]:
        return (self.resolutions.get(key) or {}).get("choice")

    def _effect(self, key: LayerKey) -> str:
        status = self.status[key]
        entry = self.review.entries[key]
        choice = self.choice(key)
        if choice == KEEP_MAIN:
            return "drop"
        if status == STATUS_CLEAN:
            if choice not in (None, TAKE_SESSION):
                # Nothing to resolve: the choice does not fit the request.
                raise ReviewRefused(
                    INVALID_MERGE_REQUEST,
                    f"{key[0]} {key[1]!r} is clean; it takes no {choice} resolution",
                    422,
                )
            if entry["action"] == "delete" and self.review.main[key] is None:
                return "noop"
            return "apply"
        allowed = {TAKE_SESSION}
        if key[0] == "node" and status in (
            STATUS_CONFLICT,
            STATUS_ORPHANED,
            STATUS_ID_TAKEN,
        ):
            if entry["action"] != "delete":
                allowed.add(AS_NEW)
        if status == STATUS_CONFLICT and entry["action"] == "update":
            allowed.add(MANUAL)
        if status == STATUS_DANGLING and not self._held_by_dropped_create(key):
            allowed = set()
        if choice not in allowed:
            self.blocked.append(key)
            return "blocked"
        if choice == MANUAL:
            conflicting = {
                f for f, d in self.review.field_diff(key).items() if d["conflict"]
            }
            values = (self.resolutions.get(key) or {}).get("values") or {}
            if not conflicting <= set(values):
                self.blocked.append(key)
                return "blocked"
        return "as_new" if choice == AS_NEW else "apply"

    # -- dangling (§10, §11.2) ----------------------------------------------------

    def node_present_after(self, node_id: str) -> bool:
        """Whether ``node_id`` names a node in the main graph after the merge."""
        key = ("node", node_id)
        in_main = node_id in self.review.storage.nodes
        effect = self.effect.get(key)
        if effect is None or effect in ("drop", "blocked", "as_new"):
            return in_main
        return self.review.entries[key]["action"] != "delete"

    def _created_here(self, node_id: str) -> Optional[str]:
        """The effect on ``node_id``'s create entry, when one is selected."""
        key = ("node", node_id)
        if key in self.selected and self.review.entries[key]["action"] == "create":
            return self.effect.get(key)
        return None

    def endpoints(self, key: LayerKey) -> Tuple[str, ...]:
        """The edge's endpoints as merged: an edge create follows a node
        merged ``as_new`` to its new id (§11.2)."""
        ends = self.review.endpoints(key)
        if self.review.entries[key]["action"] != "create":
            return ends
        return tuple(
            "\x00as_new" if self.effect.get(("node", end)) == "as_new" else end
            for end in ends
        )

    def _held_by_dropped_create(self, key: LayerKey) -> bool:
        return any(
            self._created_here(end) == "drop" and end in self.review.storage.nodes
            for end in self.review.endpoints(key)
        )

    def _edge_status(self, key: LayerKey) -> str:
        review = self.review
        entry = review.entries[key]
        if entry["action"] != "delete":
            ends = self.endpoints(key)
            absent = [
                end
                for end in ends
                if end != "\x00as_new" and not self.node_present_after(end)
            ]
            if absent or self._held_by_dropped_create(key):
                return STATUS_DANGLING
        return review.base_status(key)


def _content(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: json_copy(v) for k, v in doc.items() if k not in _NOT_CONTENT}


def _with_manual(
    doc: Dict[str, Any], values: Dict[str, Any], allowed: Iterable[str]
) -> Dict[str, Any]:
    """``doc`` with the caller's final values for the given fields (§11.2)."""
    result = json_copy(doc)
    for field in allowed:
        if field not in values:
            continue
        value = json_copy(values[field])
        if field.startswith(_METADATA_PREFIX):
            metadata = dict(result.get("metadata") or {})
            name = field[len(_METADATA_PREFIX) :]
            if value is None:
                metadata.pop(name, None)
            else:
                metadata[name] = value
            result["metadata"] = metadata
        else:
            result[field] = value
    return result


def requested_keys(selection: Any) -> List[LayerKey]:
    """The well-formed keys a list selection names, in order."""
    if not isinstance(selection, list):
        return []
    keys: List[LayerKey] = []
    for item in selection:
        if isinstance(item, dict):
            key = (item.get("kind"), item.get("entity_id"))
            if key not in keys:
                keys.append(key)
    return keys


def _retyped(review: Review, write: SessionMergeWrite) -> bool:
    """Whether a node upsert changes the type of the node main holds."""
    if write.kind != "node" or write.expect_absent:
        return False
    main_doc = _main_doc(review.storage, ("node", write.entity_id))
    return main_doc is not None and main_doc.get("type") != write.payload.get("type")


def _validate_writes(
    review: Review,
    node_upserts: List[SessionMergeWrite],
    edge_upserts: List[SessionMergeWrite],
    node_deletes: List[SessionMergeWrite],
    sent: Dict[str, Set[str]],
    edge_deletes: List[SessionMergeWrite] = (),
) -> None:
    """Refuse a merge whose writes a direct write would refuse (§6.3): the
    model's own limits, nested metadata depth, a saved view's annotations,
    and for an edge, endpoints that exist after the merge, lie in the
    caller's view and fit the relationship type. A manual resolution's values
    and main's changes since staging reach the graph only through here. An
    endpoint the caller cannot see is refused exactly as one that does not
    exist, so the refusal tells nothing about it."""
    deleted = {w.entity_id for w in node_deletes}
    planned = {w.entity_id: w.payload for w in node_upserts}

    def refuse(write: SessionMergeWrite, reason: str) -> ReviewRefused:
        return ReviewRefused(
            INVALID_MERGE_REQUEST,
            f"{write.kind} {write.entity_id!r}: {reason}",
            422,
        )

    for write in node_upserts + edge_upserts:
        metadata = write.payload.get("metadata") or {}
        depth_error = metadata_depth_error(metadata)
        if depth_error:
            raise refuse(write, depth_error)
        if write.kind == "node":
            # The rule a direct write is judged by, over the node main holds
            # now: what the session sent counts as sent, changed or not.
            annotation_error = saved_view_write_error(
                None
                if write.expect_absent
                else _main_doc(review.storage, ("node", write.entity_id)),
                write.payload,
                sent.get(write.entity_id, ()),
            )
            if annotation_error:
                raise refuse(write, annotation_error)
    # A node whose type the merge changes in place keeps the main edges the
    # merge does not delete: each must still fit the type, as a direct edge
    # create to it would have to. An entry with such an edge the caller
    # cannot see is outside its view (``_own_shown``), so every edge counted
    # here is one the caller can see.
    gone = {w.entity_id for w in edge_deletes} | {w.entity_id for w in edge_upserts}
    for write in node_upserts:
        if not _retyped(review, write):
            continue
        misfits = 0
        for edge in review.storage.get_edges_for_node(write.entity_id):
            if edge.id in gone:
                continue
            source = _endpoint_doc(review, planned, edge.source)
            target = _endpoint_doc(review, planned, edge.target)
            if (
                source is None
                or target is None
                or edge_applicability_error(
                    edge.type_str,
                    Node.from_dict(json_copy(source)),
                    Node.from_dict(json_copy(target)),
                )
            ):
                misfits += 1
        if misfits:
            raise refuse(
                write,
                f"{misfits} existing edge(s) would not fit the new type; "
                "merge or stage their removal with it",
            )
    for write in edge_upserts:
        for end in (write.payload.get("source"), write.payload.get("target")):
            doc = planned.get(end)
            if doc is None and end not in deleted:
                doc = _main_doc(review.storage, ("node", end)) if end else None
            if doc is None or (
                review.narrowing.enabled and not doc_visible(doc, review.narrowing)
            ):
                raise refuse(write, "an endpoint does not exist")
    for write in node_upserts + edge_upserts:
        try:
            entity = (Node if write.kind == "node" else Edge).from_dict(
                json_copy(write.payload)
            )
        except ValueError as exc:
            raise refuse(write, str(exc)) from None
        if write.kind == "edge":
            error = edge_applicability_error(
                entity.type_str,
                Node.from_dict(
                    json_copy(_endpoint_doc(review, planned, entity.source))
                ),
                Node.from_dict(
                    json_copy(_endpoint_doc(review, planned, entity.target))
                ),
            )
            if error:
                raise refuse(write, error)


def _endpoint_doc(review: Review, planned: Dict[str, Any], node_id: str):
    return planned.get(node_id) or _main_doc(review.storage, ("node", node_id))


def normalise_selection(review: Review, selection: Any) -> List[LayerKey]:
    """The keys a request names: ``all`` - every entry in the reader's view -
    or a list of ``{kind, entity_id}``. A key the reader cannot select is
    refused as an entry no longer staged, the same answer as for a key the
    layer never held, so it tells nothing about entries outside the view."""
    if selection == "all":
        return sorted(review.in_view)
    if not isinstance(selection, list) or not selection:
        raise ReviewRefused(
            INVALID_MERGE_REQUEST,
            "entries is 'all' or a non-empty list of {kind, entity_id}",
            422,
        )
    keys: List[LayerKey] = []
    for item in selection:
        if not isinstance(item, dict):
            raise ReviewRefused(
                INVALID_MERGE_REQUEST, "each entry is an object {kind, entity_id}", 422
            )
        key = (item.get("kind"), item.get("entity_id"))
        if key[0] not in ("node", "edge") or not isinstance(key[1], str):
            raise ReviewRefused(
                INVALID_MERGE_REQUEST, "each entry names a kind and an entity_id", 422
            )
        if key not in keys:
            keys.append(key)
    gone = [key for key in keys if key not in review.in_view]
    if gone:
        raise ReviewRefused(
            MERGE_RACED,
            "some selected entries are no longer staged; read the diff again",
            409,
            entries=[{"kind": k, "entity_id": i} for k, i in gone],
        )
    return keys


def normalise_resolutions(
    resolutions: Any, selected: Iterable[LayerKey]
) -> Dict[LayerKey, Dict[str, Any]]:
    """``[{kind, entity_id, choice, values?}]`` by key; every resolution must
    name a selected entry and a known choice."""
    if resolutions in (None, []):
        return {}
    if not isinstance(resolutions, list):
        raise ReviewRefused(
            INVALID_MERGE_REQUEST, "resolutions is a list of objects", 422
        )
    chosen = set(selected)
    out: Dict[LayerKey, Dict[str, Any]] = {}
    for item in resolutions:
        if not isinstance(item, dict):
            raise ReviewRefused(
                INVALID_MERGE_REQUEST, "each resolution is an object", 422
            )
        key = (item.get("kind"), item.get("entity_id"))
        if key not in chosen:
            raise ReviewRefused(
                INVALID_MERGE_REQUEST,
                "a resolution names an entry the merge does not select",
                422,
            )
        if key in out:
            raise ReviewRefused(
                INVALID_MERGE_REQUEST, "an entry is resolved twice", 422
            )
        choice = item.get("choice")
        if choice not in RESOLUTIONS:
            raise ReviewRefused(
                INVALID_MERGE_REQUEST,
                f"choice must be one of {', '.join(RESOLUTIONS)}",
                422,
            )
        values = item.get("values")
        if choice == MANUAL:
            if not isinstance(values, dict):
                raise ReviewRefused(
                    INVALID_MERGE_REQUEST,
                    "a manual resolution carries values: {field: final value}",
                    422,
                )
            # Before anything copies them: nesting past what metadata may
            # hold is refused here, as a direct write refuses it.
            depth_error = metadata_depth_error(values)
            if depth_error:
                raise ReviewRefused(INVALID_MERGE_REQUEST, depth_error, 422)
        elif values is not None:
            raise ReviewRefused(
                INVALID_MERGE_REQUEST, "only a manual resolution carries values", 422
            )
        out[key] = {"choice": choice, "values": values}
    return out


def plan_merge(
    review: Review,
    selected: List[LayerKey],
    resolutions: Dict[LayerKey, Dict[str, Any]],
    *,
    actor: str,
    merge_id: str,
    new_id: Any = None,
) -> Tuple[SessionMergePlan, List[Dict[str, Any]]]:
    """The merge unit for ``selected`` (§11), and one result item per entry.

    Raises ReviewRefused: ``merge_incomplete`` for a selection not closed
    under dependencies, ``merge_blocked`` (with the current diff of the
    blocking entries) for an entry no allowed resolution covers,
    ``invalid_merge_request`` for a resolution that does not fit."""
    new_id = new_id or (lambda: str(uuid.uuid4()))
    chosen = set(selected)
    missing = sorted(
        {dep for key in selected for dep in review.depends_on[key] if dep not in chosen}
    )
    if missing:
        raise ReviewRefused(
            MERGE_INCOMPLETE,
            "the selection leaves out entries it cannot be merged without",
            422,
            missing=[{"kind": k, "entity_id": i} for k, i in missing],
        )
    outcome = _Outcomes(review, selected, resolutions)

    # A node taken as a new one moves the staged edge creates naming it along
    # (§11.2): they must be merged with it, and one outside the reader's view
    # cannot be, so the node is blocked rather than leaving it to attach
    # silently to whatever later holds the old id.
    renamed: Dict[str, str] = {}
    for key in selected:
        if key[0] != "node" or outcome.effect[key] != "as_new":
            continue
        followers = [
            k
            for k, e in review.entries.items()
            if k[0] == "edge"
            and e["action"] == "create"
            and key[1] in review.endpoints(k)
        ]
        if any(k not in review.in_view for k in followers):
            outcome.effect[key] = "blocked"
            outcome.blocked.append(key)
            continue
        absent = sorted(k for k in followers if k not in chosen)
        if absent:
            raise ReviewRefused(
                MERGE_INCOMPLETE,
                "a node merged as new takes the staged edges to it along",
                422,
                missing=[{"kind": k, "entity_id": i} for k, i in absent],
            )
        renamed[key[1]] = new_id()

    for key in selected:
        values = (resolutions.get(key) or {}).get("values")
        if values is not None:
            staged = set(_staged_fields(review.entries[key]["staged"] or {}))
            stray = sorted(set(values) - staged)
            if stray:
                raise ReviewRefused(
                    INVALID_MERGE_REQUEST,
                    f"manual values name fields the entry does not stage: {stray}",
                    422,
                )

    # A node delete takes its edges' tombstones with it; dropping one would
    # leave an edge without its endpoint.
    for key in selected:
        entry = review.entries[key]
        if key[0] == "node" and entry["action"] == "delete":
            if outcome.effect[key] != "apply":
                continue
            dropped = [d for d in review.depends_on[key] if outcome.effect[d] == "drop"]
            if dropped:
                raise ReviewRefused(
                    INVALID_MERGE_REQUEST,
                    "a node deleted by the merge keeps none of its edges",
                    422,
                    entries=[{"kind": k, "entity_id": i} for k, i in dropped],
                )

    if outcome.blocked:
        blocking = sorted(set(outcome.blocked))
        raise ReviewRefused(
            MERGE_BLOCKED,
            "some selected entries need a resolution",
            409,
            items=[review.item(key, outcome.status[key]) for key in blocking],
        )

    now = _now_iso()
    node_upserts: List[SessionMergeWrite] = []
    edge_deletes: List[SessionMergeWrite] = []
    edge_upserts: List[SessionMergeWrite] = []
    node_deletes: List[SessionMergeWrite] = []
    checks: List[MergeCheck] = []
    results: List[Dict[str, Any]] = []
    # An edge added since between two nodes the merge deletes is deleted once.
    deleted_here: Set[str] = set()
    # Per node updated in place: the metadata keys sent to it.
    sent: Dict[str, Set[str]] = {}

    for key in sorted(selected):
        kind, entity_id = key
        entry = review.entries[key]
        main = review.main[key]
        effect = outcome.effect[key]
        status = outcome.status[key]
        choice = outcome.choice(key) or (
            None if status == STATUS_CLEAN else TAKE_SESSION
        )
        staged_by = entry.get("staged_by")
        checks.append(MergeCheck.entry_unchanged(kind, entity_id, entry_version(entry)))
        result = {
            "kind": kind,
            "entity_id": entity_id,
            "action": entry["action"],
            "status": status,
            "resolution": outcome.choice(key),
            "base_revision": entry.get("base_revision"),
            "outcome": {"drop": "dropped", "noop": "noop"}.get(effect, "applied"),
        }
        results.append(result)
        if effect in ("drop", "noop"):
            continue

        if entry["action"] == "delete":
            if kind == "edge":
                edge_deletes.append(
                    SessionMergeWrite(
                        "edge",
                        "delete",
                        entity_id,
                        expected_revision=_revision(main),
                        staged_by=staged_by,
                    )
                )
                continue
            incident = [
                edge_id
                for edge_id in review.unstaged_edges(entity_id)
                if edge_id not in deleted_here
            ]
            deleted_here.update(incident)
            # take_session on a conflicting delete deletes the edges added
            # since, each with its own expectation (§11.2).
            for edge_id in incident:
                edge_deletes.append(
                    SessionMergeWrite(
                        "edge",
                        "delete",
                        edge_id,
                        expected_revision=_revision(
                            _main_doc(review.storage, ("edge", edge_id))
                        ),
                        staged_by=staged_by,
                    )
                )
            checks.append(
                MergeCheck.incident_edges(
                    entity_id,
                    [e.id for e in review.storage.get_edges_for_node(entity_id)],
                )
            )
            node_deletes.append(
                SessionMergeWrite(
                    "node",
                    "delete",
                    entity_id,
                    expected_revision=_revision(main),
                    staged_by=staged_by,
                )
            )
            continue

        # Creates and updates: the entity the merge writes.
        if entry["action"] == "create":
            doc = json_copy(entry["staged"])
            if status == STATUS_ID_TAKEN and effect == "apply":
                # Replaces the main entity, as an update of every field.
                doc = {**_content(doc), "id": entity_id}
                for stamp in ("created_at",):
                    if main.get(stamp) is not None:
                        doc[stamp] = main[stamp]
                expectation = {"expected_revision": _revision(main)}
            else:
                doc["created_at"] = now
                expectation = {"expect_absent": True}
        else:
            source = main if main is not None else entry["base"]
            doc = _apply_staged(source, entry["staged"] or {})
            if effect == "apply" and choice == MANUAL:
                conflicting = [
                    f for f, d in review.field_diff(key).items() if d["conflict"]
                ]
                doc = _with_manual(
                    doc, (resolutions.get(key) or {}).get("values") or {}, conflicting
                )
            if main is not None and effect == "apply":
                expectation = {"expected_revision": _revision(main)}
            else:
                expectation = {"expect_absent": True}
                if effect == "as_new":
                    doc["created_at"] = now
        if kind == "node":
            doc["updated_at"] = now
        target_id = entity_id
        if effect == "as_new":
            target_id = renamed[entity_id]
            doc["created_at"] = now
            expectation = {"expect_absent": True}
            result["new_entity_id"] = target_id
        doc["id"] = target_id
        doc.pop("revision", None)
        if kind == "edge":
            if entry["action"] == "create":
                doc["source"] = renamed.get(doc["source"], doc["source"])
                doc["target"] = renamed.get(doc["target"], doc["target"])
            for end in (doc["source"], doc["target"]):
                checks.append(MergeCheck.node_exists(end))
        write = SessionMergeWrite(
            kind, "upsert", target_id, doc, staged_by=staged_by, **expectation
        )
        (node_upserts if kind == "node" else edge_upserts).append(write)
        if kind == "node" and not write.expect_absent:
            # The metadata keys the session sent this node, and the caller's
            # manual metadata values: judged as a direct update judges what
            # it sends, re-sent unchanged or not.
            keys = set(((entry["staged"] or {}).get("metadata") or {}))
            if effect == "apply" and choice == MANUAL:
                keys |= {
                    f[len(_METADATA_PREFIX) :]
                    for f in (resolutions.get(key) or {}).get("values") or {}
                    if f.startswith(_METADATA_PREFIX)
                }
            sent[target_id] = keys

    _validate_writes(
        review, node_upserts, edge_upserts, node_deletes, sent, edge_deletes
    )
    # The kept edges of a retyped node were judged against the new type on
    # what this instance holds; the store refuses the unit if another one
    # added an edge to the node since, or changed one of them, which no
    # direct retype could keep.
    for write in node_upserts:
        if _retyped(review, write):
            edges = review.storage.get_edges_for_node(write.entity_id)
            checks.append(
                MergeCheck.incident_edges(
                    write.entity_id,
                    [e.id for e in edges],
                    {e.id: _revision(_doc(e)) for e in edges},
                )
            )
    writes = node_upserts + edge_deletes + edge_upserts + node_deletes
    if renamed:
        # Which staged edge creates follow a node merged as new was decided
        # on the whole layer, entries outside the selection (and the reader's
        # view) included: the unit refuses if any of it changed, so an edge
        # staged meanwhile never attaches to whatever holds the old id.
        checks.extend(
            MergeCheck.entry_unchanged(k, i, entry_version(e))
            for (k, i), e in sorted(review.entries.items())
        )
        checks.append(MergeCheck.layer_holds_only(review.entries))
    unique_checks = list(dict.fromkeys(checks))
    record = {
        "actor": actor,
        "merged_at": now,
        "entries": json_copy(results),
    }
    plan = SessionMergePlan(
        writes=writes,
        checks=unique_checks,
        removals=sorted(selected),
        record=record,
    )
    return plan, results
