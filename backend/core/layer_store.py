"""The layer store: the `staged_sessions` half of the persistence seam.

docs/SESSION_OVERLAY_CONTRACT.md §7. A backend that declares
`staged_sessions` persists each staged session's layer - at most one entry
per entity - its staged-session record (§5.3), and the records of the merges
applied from it (§11.5), next to the graph it would change. That is what
lets one unit cover a merge's graph writes and its layer removal (I8), and
what shares a layer wherever the graph is shared.

This module holds what every implementation shares: the names, the errors,
the shape of a merge's checks, the boundary validation, and the reference
evaluation of a merge's expectations and checks against an in-memory model,
which the file backend and the in-memory reference backend both run. The
PostgreSQL backend evaluates the same rules in SQL, inside its transaction.

The store is deliberately ignorant of what an entry MEANS. It validates the
key an entry is stored under and that it is JSON, and stores it whole; the
collapsing rules (§6.2), validation against the composed view (§6.3) and the
diff (§10) belong to the slices that stage and merge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from .session_store import is_valid_session_id
from .storage_backends import (
    _STAGED_SESSION_METHODS,
    EntityOperation,
    EntityRevisionConflict,
)

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUS_NONE = "none"

MERGE_PENDING = "pending"
MERGE_APPLIED = "applied"

ENTITY_KINDS = ("node", "edge")

# (kind, entity_id): how a layer names an entry, and how a removal names one.
LayerKey = Tuple[str, str]

# The longest entity or merge id the store accepts. Far beyond any id the
# application mints, and short enough that a key can never reach a limit of
# the store itself (a PostgreSQL btree entry tops out near 2.7 kB).
MAX_ID_LENGTH = 512


class LayerStoreError(RuntimeError):
    """Base for the refusals of the layer store."""


class InvalidLayerRequest(LayerStoreError, ValueError):
    """A session id, key, entry or merge request the store will not hold.

    Raised before anything is read or written.
    """


class StagedSessionNotActive(LayerStoreError):
    """The session has no active staged-session record.

    A layer exists only for a session recorded as staged (§5.3), so staging
    into, merging from or discarding in any other session is refused rather
    than creating a layer nobody will ever read. `status` is what the store
    holds: `none` or `retired`. A retired id is answered as
    `session_not_found` by the slices that expose it (§5.3).
    """

    def __init__(self, session_id: str, status: str):
        self.session_id = session_id
        self.status = status
        super().__init__(f"session {session_id!r} is not an active staged session")


class StagedSessionRetired(LayerStoreError):
    """`record_staged` for an id whose record is retired.

    A retired id is never created again (§5.3), explicitly or implicitly.
    """

    code = "session_not_found"

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"session {session_id!r} was deleted and cannot be reused")


class LayerFull(LayerStoreError):
    """A staging op would take the layer past its cap (§6.4: `layer_full`).

    Nothing of the op was applied.
    """

    code = "layer_full"

    def __init__(self, session_id: str, limit: int, would_hold: int):
        self.session_id = session_id
        self.limit = limit
        self.would_hold = would_hold
        super().__init__(
            f"layer of session {session_id!r} would hold {would_hold} entries; "
            f"the limit is {limit}"
        )


class LayerNotEmpty(LayerStoreError):
    """`retire(require_empty=True)` met a layer that holds entries (§13:
    `layer_not_empty`).

    Checked inside the unit that would retire, so an entry staged by anyone
    up to that moment is seen, and nothing was retired or dropped.
    """

    code = "layer_not_empty"

    def __init__(self, session_id: str, entries: int):
        self.session_id = session_id
        self.entries = entries
        super().__init__(
            f"layer of session {session_id!r} holds {entries} staged entries"
        )


class LayerRaced(LayerStoreError):
    """A staging op's expectation about the layer failed inside its unit.

    The op read the layer, decided, and expected each entry it changes to
    be as it read it - the same `session_seq`, or still absent. Another op
    changed one in between, so applying this one would overwrite that change
    with a decision made without it. Nothing was applied; the caller reads
    the layer again and decides again.
    """

    code = "layer_raced"

    def __init__(self, session_id: str, key: "LayerKey"):
        self.session_id = session_id
        self.key = key
        super().__init__(
            f"the staged entry for {key[0]} {key[1]!r} changed while the op ran"
        )


class MergeRaced(LayerStoreError):
    """An expectation or check failed inside the merge unit (§11.3).

    Nothing was written: not the graph, not the layer, not a merge record.
    The caller re-reads the diff and tries again. `reason` says which
    condition failed, for the caller's log; it names only ids the request
    itself carried.
    """

    code = "merge_raced"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"merge raced: {reason}")


class MergeIdTaken(LayerStoreError):
    """A merge id already recorded for a DIFFERENT session.

    I9 makes a repeated merge id return the first result. That holds within
    the session that merged; returning another session's record would hand
    one session the audit trail of another's merge, and applying nothing
    while claiming success would tell the caller its work landed. So it is
    refused, and the caller picks another id.
    """

    def __init__(self, merge_id: str):
        self.merge_id = merge_id
        super().__init__(f"merge id {merge_id!r} is already used by another session")


@dataclass(frozen=True)
class MergeCheck:
    """One condition a merge decision depended on, verified inside the unit.

    §11.3. Build them with the constructors; the fields are what a backend
    reads.

    - `node_exists(node_id)`: after the unit's operations, the node exists.
      It is how "both endpoints of every edge the merge creates or keeps
      exist, or are created by the same unit" is said: one check per
      endpoint, judged on the state the unit leaves.
    - `incident_edges(node_id, edge_ids)`: before the unit's operations, the
      edges incident to the node in the store are exactly `edge_ids` - the
      ones the merge deletes with it. An edge added since the diff was read
      fails it.
    - `entry_unchanged(kind, entity_id, session_seq)`: the layer still holds
      the entry the decision was made on, as last changed by the op with
      that session sequence number. A participant restaging the entity
      between the diff and the merge fails it, so the merge cannot apply the
      older value and drop the newer one.
    """

    check: Literal["node_exists", "incident_edges", "entry_unchanged"]
    entity_id: str
    kind: str = "node"
    edge_ids: Tuple[str, ...] = ()
    session_seq: Optional[int] = None

    @classmethod
    def node_exists(cls, node_id: str) -> "MergeCheck":
        return cls("node_exists", node_id)

    @classmethod
    def incident_edges(cls, node_id: str, edge_ids: Iterable[str]) -> "MergeCheck":
        return cls("incident_edges", node_id, edge_ids=tuple(sorted(set(edge_ids))))

    @classmethod
    def entry_unchanged(
        cls, kind: str, entity_id: str, session_seq: Optional[int]
    ) -> "MergeCheck":
        return cls("entry_unchanged", entity_id, kind=kind, session_seq=session_seq)


@dataclass(frozen=True)
class MergeResult:
    """What `merge` returns.

    `record` is the stored merge record: the caller's record, with the
    store's `merge_id`, `session_id`, `status` and `applied` - one item per
    operation, `{kind, entity_id, action, revision}`, where `revision` is the
    revision the store wrote (None for a delete). `replayed` is True when the
    merge id was already recorded and nothing was written this time (I9).
    """

    record: Dict[str, Any]
    replayed: bool = False

    @property
    def revisions(self) -> List[Optional[int]]:
        return [item.get("revision") for item in self.record.get("applied", [])]


@runtime_checkable
class LayerStoreBackend(Protocol):
    """The operations a backend declaring `staged_sessions` implements.

    Names and meaning are §7's; the Python shapes are fixed here and in
    docs/PERSISTENCE_BACKENDS.md. Every operation validates its arguments
    before it touches the store, and every one that changes something is one
    unit: it lands whole or not at all.
    """

    def load_layer(self, session_id: str) -> List[Dict[str, Any]]:
        """Every entry of the layer; an unknown session yields []."""

    def stage(
        self,
        session_id: str,
        entries: Sequence[Mapping[str, Any]],
        removals: Sequence[LayerKey] = (),
        *,
        max_entries: Optional[int] = None,
        expected: Optional[Mapping[LayerKey, Optional[int]]] = None,
    ) -> None:
        """Upsert `entries` and remove `removals`, atomically. Refused with
        StagedSessionNotActive unless the session is active, and with
        LayerFull, applying nothing, when the layer would end up holding more
        than `max_entries`. `expected` maps keys to the `session_seq` of the
        entry the caller read, None for one it found absent; any that no
        longer holds refuses the op with LayerRaced, applying nothing."""

    def merge(
        self,
        session_id: str,
        merge_id: str,
        operations: Sequence[EntityOperation],
        checks: Sequence[MergeCheck],
        removals: Sequence[LayerKey],
        record: Mapping[str, Any],
    ) -> MergeResult:
        """Verify, apply, remove and record as one unit (§7, §11.3, §11.6)."""

    def discard(
        self, session_id: str, removals: Optional[Sequence[LayerKey]] = None
    ) -> List[LayerKey]:
        """Remove the given entries, or every entry when `removals` is None.
        Returns the keys actually removed. Touches nothing else (I10)."""

    def load_merge(self, merge_id: str) -> Optional[Dict[str, Any]]:
        """The stored merge record, or None."""

    def drop_layer(self, session_id: str) -> None:
        """Remove the whole layer, leaving the record as it is."""

    def record_staged(self, session_id: str) -> None:
        """Create the active staged-session record; idempotent for an active
        one, refused with StagedSessionRetired for a retired one."""

    def staged_status(self, session_id: str) -> str:
        """`active`, `retired` or `none`."""

    def retire(self, session_id: str, *, require_empty: bool = False) -> None:
        """Drop the layer and mark the record retired, as one unit. Retiring
        an id with no record records it retired, so it can never be created
        later either. With `require_empty`, a layer holding entries is
        refused with LayerNotEmpty inside that unit, and nothing changes -
        so a delete never drops work staged after its caller looked (I11)."""

    def layers_in_use(self) -> List[str]:
        """Ids of the sessions whose layers are not empty."""


LAYER_STORE_METHODS = _STAGED_SESSION_METHODS


# --- validation at the boundary ---------------------------------------------


def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidLayerRequest(f"{what} must be a non-empty string")
    if len(value) > MAX_ID_LENGTH:
        raise InvalidLayerRequest(f"{what} is longer than {MAX_ID_LENGTH} characters")
    if "\x00" in value:
        raise InvalidLayerRequest(f"{what} contains a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidLayerRequest(f"{what} is not valid Unicode text") from None
    return value


def check_session_id(session_id: Any) -> str:
    """The session id, or InvalidLayerRequest.

    Only the shape a session id has (`session_store.SESSION_ID_RE`): the
    store never holds a key no session could have, and a value it holds
    never needs escaping anywhere - a file name, a log line, a statement.
    """
    if not is_valid_session_id(session_id):
        raise InvalidLayerRequest("session id is not a valid session id")
    return session_id


def check_merge_id(merge_id: Any) -> str:
    return _check_id(merge_id, "merge id")


def check_key(key: Any) -> LayerKey:
    try:
        kind, entity_id = key
    except (TypeError, ValueError):
        raise InvalidLayerRequest("a layer key is a (kind, entity_id) pair") from None
    if kind not in ENTITY_KINDS:
        raise InvalidLayerRequest(f"entity kind must be one of {ENTITY_KINDS}")
    return (kind, _check_id(entity_id, "entity id"))


def check_keys(keys: Iterable[Any]) -> List[LayerKey]:
    return [check_key(key) for key in keys]


def _as_json(value: Any, what: str) -> Any:
    """A deep, JSON-only copy of `value`: what the store keeps.

    Round-tripped, so the caller cannot change what was stored by mutating
    what it passed, and a value JSON cannot carry - a set, a datetime, a
    non-finite float, which a PostgreSQL `jsonb` refuses - is refused here,
    the same in every backend, rather than by whichever store meets it.
    """
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise InvalidLayerRequest(f"{what} is not JSON: {exc}") from None


def _check_text_values(value: Any, what: str) -> None:
    """Refuse strings no store can hold: a NUL, a lone surrogate."""
    if isinstance(value, str):
        if "\x00" in value:
            raise InvalidLayerRequest(f"{what} contains a NUL character")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidLayerRequest(f"{what} is not valid Unicode text") from None
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_text_values(k, what)
            _check_text_values(v, what)
    elif isinstance(value, list):
        for v in value:
            _check_text_values(v, what)


def check_entry(entry: Any) -> Tuple[LayerKey, Dict[str, Any]]:
    """An entry's key and a stored copy of it, or InvalidLayerRequest."""
    if not isinstance(entry, Mapping):
        raise InvalidLayerRequest("a layer entry is an object")
    key = check_key((entry.get("kind"), entry.get("entity_id")))
    stored = _as_json(dict(entry), "layer entry")
    _check_text_values(stored, "layer entry")
    return key, stored


def check_entries(
    entries: Iterable[Any], removals: Iterable[Any]
) -> Tuple[Dict[LayerKey, Dict[str, Any]], List[LayerKey]]:
    """A staging op's entries by key, and its removals, or InvalidLayerRequest.

    A key named twice - two entries, or an entry and a removal - is refused:
    the op would mean different things depending on the order it was read in.
    """
    by_key: Dict[LayerKey, Dict[str, Any]] = {}
    for entry in entries:
        key, stored = check_entry(entry)
        if key in by_key:
            raise InvalidLayerRequest(f"entry {key} is staged twice in one op")
        by_key[key] = stored
    removed = check_keys(removals)
    if len(set(removed)) != len(removed) or set(removed) & set(by_key):
        raise InvalidLayerRequest("a key is named twice in one op")
    return by_key, removed


def check_expected(expected: Any) -> Dict[LayerKey, Optional[int]]:
    """A staging op's expectations about the layer, or InvalidLayerRequest."""
    if expected is None:
        return {}
    if not isinstance(expected, Mapping):
        raise InvalidLayerRequest("expected maps layer keys to session_seq")
    checked: Dict[LayerKey, Optional[int]] = {}
    for key, seq in expected.items():
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int)):
            raise InvalidLayerRequest("an expected session_seq is an integer or None")
        checked[check_key(key)] = seq
    return checked


def verify_expected(
    session_id: str,
    layer: Mapping[LayerKey, Mapping[str, Any]],
    expected: Mapping[LayerKey, Optional[int]],
) -> None:
    """Raise LayerRaced unless every key is as the op read it."""
    for key, seq in sorted(expected.items()):
        entry = layer.get(key)
        held = None if entry is None else entry.get("session_seq")
        if (entry is None) != (seq is None) or held != seq:
            raise LayerRaced(session_id, key)


def check_max_entries(max_entries: Any) -> Optional[int]:
    if max_entries is None:
        return None
    if isinstance(max_entries, bool) or not isinstance(max_entries, int):
        raise InvalidLayerRequest("max_entries must be an integer")
    if max_entries < 0:
        raise InvalidLayerRequest("max_entries must not be negative")
    return max_entries


def check_merge_request(
    operations: Sequence[Any],
    checks: Sequence[Any],
    removals: Sequence[Any],
    record: Any,
) -> Tuple[List[EntityOperation], List[MergeCheck], List[LayerKey], Dict[str, Any]]:
    ops = list(operations)
    for op in ops:
        if not isinstance(op, EntityOperation):
            raise InvalidLayerRequest("a merge operation is an EntityOperation")
        check_key((op.kind, op.entity_id))
        if op.action not in ("upsert", "delete"):
            raise InvalidLayerRequest("operation action must be upsert or delete")
        if op.action == "upsert":
            if not isinstance(op.payload, dict) or op.payload.get("id") != op.entity_id:
                raise InvalidLayerRequest(
                    "an upsert carries its entity, under the id it names"
                )
            _check_text_values(_as_json(op.payload, "operation payload"), "payload")
    keyed = [(op.kind, op.entity_id) for op in ops]
    if len(set(keyed)) != len(keyed):
        # One operation per entity: a merge applies one net change to each,
        # and the file backend's recovery judges each entity by one outcome.
        raise InvalidLayerRequest("a merge names an entity in two operations")
    verified = list(checks)
    for check in verified:
        if not isinstance(check, MergeCheck):
            raise InvalidLayerRequest("a merge check is a MergeCheck")
        if check.check not in ("node_exists", "incident_edges", "entry_unchanged"):
            raise InvalidLayerRequest(f"unknown merge check {check.check!r}")
        check_key((check.kind, check.entity_id))
        for edge_id in check.edge_ids:
            _check_id(edge_id, "edge id")
    removed = check_keys(removals)
    if len(set(removed)) != len(removed):
        raise InvalidLayerRequest("a merge removes one entry twice")
    if not isinstance(record, Mapping):
        raise InvalidLayerRequest("a merge record is an object")
    stored = _as_json(dict(record), "merge record")
    _check_text_values(stored, "merge record")
    return ops, verified, removed, stored


# --- the reference evaluation -------------------------------------------------


def stored_revision(doc: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The revision of a stored entity, None when it is absent.

    An entity without the field is a legacy one, revision 0 (§4.2).
    """
    if doc is None:
        return None
    try:
        return int(doc.get("revision") or 0)
    except (TypeError, ValueError):
        return 0


def apply_merge_to_model(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
    layer: Mapping[LayerKey, Mapping[str, Any]],
    operations: Sequence[EntityOperation],
    checks: Sequence[MergeCheck],
    removals: Sequence[LayerKey],
) -> None:
    """Verify a merge against `nodes`/`edges`/`layer` and apply its operations.

    `nodes` and `edges` are changed in place, so a caller wanting atomicity
    hands in copies. Raises MergeRaced, having applied nothing the caller
    keeps, when an expectation or check fails.

    Order, which the PostgreSQL backend follows too:

    1. layer conditions - every removal names an entry the layer holds, and
       every `entry_unchanged` holds;
    2. `incident_edges`, on the state before the operations;
    3. the operations, in order, each checked against its expectation on
       the state the operations before it left; a delete must find its
       entity;
    4. `node_exists`, on the state the operations leave.
    """
    for key in removals:
        if key not in layer:
            raise MergeRaced(f"{key[0]} {key[1]!r} is no longer staged")
    for check in checks:
        if check.check != "entry_unchanged":
            continue
        entry = layer.get((check.kind, check.entity_id))
        if entry is None or entry.get("session_seq") != check.session_seq:
            raise MergeRaced(
                f"the staged entry for {check.kind} {check.entity_id!r} changed"
            )
    for check in checks:
        if check.check != "incident_edges":
            continue
        incident = sorted(
            edge_id
            for edge_id, edge in edges.items()
            if check.entity_id in (edge.get("source"), edge.get("target"))
        )
        if incident != list(check.edge_ids):
            raise MergeRaced(
                f"node {check.entity_id!r} has incident edges the merge did not list"
            )
    for op in operations:
        table = nodes if op.kind == "node" else edges
        current = stored_revision(table.get(op.entity_id))
        if op.expect_absent and current is not None:
            raise _raced(op, current)
        if op.expected_revision is not None and current != op.expected_revision:
            raise _raced(op, current)
        if op.action == "delete" and current is None:
            # A delete with nothing left to delete is not an operation: it is
            # left out of the merge (§10), and one issued anyway was decided
            # on a graph that has changed since.
            raise MergeRaced(f"{op.kind} {op.entity_id!r} is already absent")
        if op.action == "upsert":
            table[op.entity_id] = op.payload
        else:
            table.pop(op.entity_id, None)
    for check in checks:
        if check.check == "node_exists" and check.entity_id not in nodes:
            raise MergeRaced(f"node {check.entity_id!r} does not exist")


def _raced(op: EntityOperation, current: Optional[int]) -> MergeRaced:
    conflict = EntityRevisionConflict(
        op.kind,
        op.entity_id,
        None if op.expect_absent else op.expected_revision,
        current,
    )
    raced = MergeRaced(str(conflict))
    raced.__cause__ = conflict
    return raced


def applied_items(
    operations: Sequence[EntityOperation], revisions: Sequence[Optional[int]]
) -> List[Dict[str, Any]]:
    return [
        {
            "kind": op.kind,
            "entity_id": op.entity_id,
            "action": op.action,
            "revision": revision,
        }
        for op, revision in zip(operations, revisions)
    ]


def build_record(
    record: Mapping[str, Any],
    *,
    merge_id: str,
    session_id: str,
    status: str,
    applied: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The stored record: the caller's, with the store's own keys laid over it."""
    return {
        **record,
        "merge_id": merge_id,
        "session_id": session_id,
        "status": status,
        "applied": [dict(item) for item in applied],
    }


def replay(stored: Mapping[str, Any], session_id: str, merge_id: str) -> MergeResult:
    """The answer to a merge id already recorded (I9), or MergeIdTaken."""
    if stored.get("session_id") != session_id:
        raise MergeIdTaken(merge_id)
    return MergeResult(record=json.loads(json.dumps(stored)), replayed=True)
