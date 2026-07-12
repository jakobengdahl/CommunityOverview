"""
Server-side session store for multi-user shared sessions.

A *session* is stored **outside** the knowledge graph: it holds node
references + layout + annotations, never node copies (design decision D4 in
docs/MULTI_USER_SESSIONS_DESIGN.md). Core ships a file-per-session backend;
the SaaS layer swaps in a DB-backed one behind the ``SessionPersistenceBackend``
seam (D5). This module owns:

* the ``Session`` data model and its JSON (de)serialisation,
* the persistence protocol + a file implementation (atomic temp+rename writes,
  mirroring ``storage_backends.py``),
* the in-memory ``SessionStore`` (CRUD + applying state ops + a per-session
  ring buffer used for realtime catch-up).

Realtime fan-out, presence and selection claims live in ``session_hub`` and the
orchestration in ``session_manager`` — this module never imports either, so it
stays a pure state layer with no asyncio dependency.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Protocol

# Session IDs keep the historical DDDD-DDDD shape so they interoperate with the
# legacy connect-by-ID flow and the existing SSE plumbing.
SESSION_ID_RE = re.compile(r"^\d{4}-\d{4}$")

_ANNOTATION_KINDS = {"group", "note", "label", "arrow"}
_DEFAULT_MAX_ANNOTATIONS = 2000
_DEFAULT_RING_SIZE = 500

# State-mutating ops that are persisted, sequenced and mirrored to catch-up.
STATE_OPS = {
    "nodes_added",
    "nodes_removed",
    "node_moved",
    "nodes_hidden",
    "nodes_shown",
    "edges_hidden",
    "edges_shown",
    "annotation_created",
    "annotation_updated",
    "annotation_deleted",
    "group_membership_changed",
    "session_renamed",
    "layout_applied",
}


def is_valid_session_id(session_id: str) -> bool:
    return bool(isinstance(session_id, str) and SESSION_ID_RE.match(session_id))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty_state() -> Dict[str, Any]:
    return {
        "node_refs": [],
        "positions": {},
        "hidden_node_ids": [],
        "hidden_edge_ids": [],
        "annotations": [],
        "manual_edges": [],
    }


class OpError(ValueError):
    """Raised when an op payload fails validation at the boundary."""


@dataclass
class Session:
    """A shared visualization session (node refs + layout + annotations)."""

    id: str
    name: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    seq: int = 0
    state: Dict[str, Any] = field(default_factory=_empty_state)
    # Ephemeral (not persisted, not part of to_dict/from_dict): recently
    # deleted annotation ids, so a create-op retry for an id another
    # collaborator has since deleted is dropped instead of resurrecting it —
    # matching annotation_updated's existing "update on deleted is dropped"
    # rule. Bounded so a long-lived session's memory footprint stays flat.
    _deleted_annotation_ids: Deque[str] = field(
        default_factory=lambda: deque(maxlen=200)
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "seq": self.seq,
            "state": self.state,
        }

    def meta(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        state = data.get("state") or {}
        merged = _empty_state()
        merged.update({k: v for k, v in state.items() if k in merged})
        return cls(
            id=data["id"],
            name=data.get("name"),
            created_at=data.get("created_at") or _now_iso(),
            updated_at=data.get("updated_at") or _now_iso(),
            seq=int(data.get("seq") or 0),
            state=merged,
        )


# ==================== Persistence seam (D5) ====================


class SessionPersistenceBackend(Protocol):
    """Storage seam for sessions. Core ships the file impl; SaaS swaps a DB."""

    def load(self, session_id: str) -> Optional[Dict[str, Any]]: ...

    def save(self, session: Dict[str, Any]) -> None: ...

    def delete(self, session_id: str) -> None: ...

    def list_meta(self) -> List[Dict[str, Any]]: ...


# Cross-platform file locking mirrors storage_backends.py so session writes get
# the same guarantees as graph writes.
if sys.platform == "win32":  # pragma: no cover - platform specific
    import msvcrt

    def _lock_file(f, exclusive: bool = True) -> None:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if exclusive else msvcrt.LK_LOCK, 1)

    def _unlock_file(f) -> None:
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f, exclusive: bool = True) -> None:
        fcntl.flock(f, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    def _unlock_file(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)


class FileSessionPersistenceBackend:
    """One JSON file per session under ``directory`` with atomic writes.

    Per-session files keep write amplification low under frequent op-driven
    saves (a large session never rewrites its neighbours). No retention or
    eviction (design D13): a session lives until explicitly deleted.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, session_id: str) -> Path:
        # session_id is always validated before it reaches here, so it cannot
        # contain path separators — but guard anyway to keep this a hard boundary.
        if not is_valid_session_id(session_id):
            raise OpError(f"invalid session id: {session_id!r}")
        return self.directory / f"{session_id}.json"

    def load(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(session_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            _lock_file(f, exclusive=False)
            try:
                return json.load(f)
            finally:
                _unlock_file(f)

    def save(self, session: Dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(session["id"])
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".json", prefix="session_", dir=self.directory
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                _lock_file(f, exclusive=True)
                try:
                    json.dump(session, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    _unlock_file(f)
            if sys.platform == "win32" and path.exists():
                os.replace(temp_path, path)
            else:
                os.rename(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            os.unlink(path)

    def list_meta(self) -> List[Dict[str, Any]]:
        if not self.directory.exists():
            return []
        metas: List[Dict[str, Any]] = []
        for path in self.directory.glob("*.json"):
            sid = path.stem
            if not is_valid_session_id(sid):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _lock_file(f, exclusive=False)
                    try:
                        data = json.load(f)
                    finally:
                        _unlock_file(f)
            except Exception:
                continue
            metas.append(Session.from_dict(data).meta())
        metas.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return metas


class InMemorySessionPersistenceBackend:
    """Non-persistent backend for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}

    def load(self, session_id: str) -> Optional[Dict[str, Any]]:
        raw = self._data.get(session_id)
        return json.loads(json.dumps(raw)) if raw is not None else None

    def save(self, session: Dict[str, Any]) -> None:
        self._data[session["id"]] = json.loads(json.dumps(session))

    def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)

    def list_meta(self) -> List[Dict[str, Any]]:
        metas = [Session.from_dict(d).meta() for d in self._data.values()]
        metas.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return metas


# ==================== Op validation + application ====================


def _require_id_list(op: Dict[str, Any], key: str) -> List[str]:
    value = op.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise OpError(f"{op.get('op')}: '{key}' must be a list of strings")
    return value


def _validate_position(value: Any) -> Dict[str, float]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("x"), (int, float))
        or not isinstance(value.get("y"), (int, float))
    ):
        raise OpError("position must be an object with numeric x and y")
    return {"x": float(value["x"]), "y": float(value["y"])}


def _validate_annotation(value: Any, *, require_id: bool) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise OpError("annotation must be an object")
    kind = value.get("kind")
    if kind not in _ANNOTATION_KINDS:
        raise OpError(f"annotation kind must be one of {sorted(_ANNOTATION_KINDS)}")
    if require_id and not isinstance(value.get("id"), str):
        raise OpError("annotation update/delete requires a string 'id'")
    if "position" in value and value["position"] is not None:
        _validate_position(value["position"])
    return value


def _union(existing: List[str], incoming: List[str]) -> List[str]:
    seen = set(existing)
    result = list(existing)
    for item in incoming:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _remove_all(existing: List[str], removals: List[str]) -> List[str]:
    drop = set(removals)
    return [item for item in existing if item not in drop]


class SessionStore:
    """In-memory registry of sessions backed by a persistence seam.

    Thread-safety: a single lock guards the in-memory map. State ops are applied
    while the caller (``SessionManager``) holds a per-session async lock, so op
    ordering is serialised upstream; this lock only protects the dict itself and
    the lazy load-through.
    """

    def __init__(
        self,
        backend: SessionPersistenceBackend,
        *,
        max_annotations: int = _DEFAULT_MAX_ANNOTATIONS,
        ring_size: int = _DEFAULT_RING_SIZE,
    ) -> None:
        self._backend = backend
        self._max_annotations = max_annotations
        self._ring_size = ring_size
        self._sessions: Dict[str, Session] = {}
        self._rings: Dict[str, Deque[Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    # ---------------- lifecycle ----------------

    def _new_id(self) -> str:
        for _ in range(100):
            candidate = f"{secrets.randbelow(10000):04d}-{secrets.randbelow(10000):04d}"
            if (
                candidate not in self._sessions
                and self._backend.load(candidate) is None
            ):
                return candidate
        raise RuntimeError("could not allocate a free session id")

    def create(self, name: Optional[str] = None) -> Session:
        with self._lock:
            session = Session(id=self._new_id(), name=name)
            self._sessions[session.id] = session
            self._rings[session.id] = deque(maxlen=self._ring_size)
            self._backend.save(session.to_dict())
            return session

    def get_or_create(self, session_id: str) -> "tuple[Session, bool]":
        """Return ``(session, created)``, creating it with the given id if absent.

        Supports the connect-by-ID / share-URL flow where a client joins a
        specific session id that may not exist server-side yet (design 3.6).
        """
        if not is_valid_session_id(session_id):
            raise OpError(f"invalid session id: {session_id!r}")
        existing = self.get(session_id)
        if existing is not None:
            return existing, False
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing, False
            session = Session(id=session_id)
            self._sessions[session_id] = session
            self._rings[session_id] = deque(maxlen=self._ring_size)
            self._backend.save(session.to_dict())
            return session, True

    def get(self, session_id: str) -> Optional[Session]:
        if not is_valid_session_id(session_id):
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                return session
            raw = self._backend.load(session_id)
            if raw is None:
                return None
            session = Session.from_dict(raw)
            self._sessions[session_id] = session
            self._rings.setdefault(session_id, deque(maxlen=self._ring_size))
            return session

    def exists(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    def list_meta(self) -> List[Dict[str, Any]]:
        with self._lock:
            metas = {m["id"]: m for m in self._backend.list_meta()}
            # In-memory sessions may hold unpersisted-but-saved newer meta.
            for session in self._sessions.values():
                metas[session.id] = session.meta()
        ordered = sorted(
            metas.values(), key=lambda m: m.get("updated_at") or "", reverse=True
        )
        return ordered

    def rename(self, session_id: str, name: Optional[str]) -> Optional[Session]:
        session = self.get(session_id)
        if session is None:
            return None
        with self._lock:
            session.name = name
            session.updated_at = _now_iso()
            self._backend.save(session.to_dict())
            return session

    def delete(self, session_id: str) -> bool:
        with self._lock:
            existed = (
                session_id in self._sessions
                or self._backend.load(session_id) is not None
            )
            self._sessions.pop(session_id, None)
            self._rings.pop(session_id, None)
            self._backend.delete(session_id)
            return existed

    def persist(self, session: Session) -> None:
        with self._lock:
            self._backend.save(session.to_dict())

    def session_count(self) -> int:
        return len(self._sessions)

    # ---------------- op application ----------------

    def apply_state_op(
        self, session: Session, op: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Apply one persisted state op to ``session``.

        Mutates ``session.state`` in place, bumps ``session.seq``, appends the
        applied (normalised) op to the ring buffer and returns it tagged with
        its ``seq``. Returns ``None`` when the op is a legitimate no-op that must
        not advance the sequence (e.g. an update on an already-deleted
        annotation) so callers do not broadcast a phantom event.
        """
        op_type = op.get("op")
        if op_type not in STATE_OPS:
            raise OpError(f"unknown state op: {op_type!r}")

        state = session.state
        applied: Optional[Dict[str, Any]] = dict(op)

        if op_type == "nodes_added":
            state["node_refs"] = _union(
                state["node_refs"], _require_id_list(op, "node_ids")
            )
        elif op_type == "nodes_removed":
            removals = _require_id_list(op, "node_ids")
            drop = set(removals)
            state["node_refs"] = _remove_all(state["node_refs"], removals)
            state["positions"] = {
                k: v for k, v in state["positions"].items() if k not in drop
            }
            state["hidden_node_ids"] = _remove_all(state["hidden_node_ids"], removals)
            for ann in state["annotations"]:
                members = ann.get("member_node_ids")
                if isinstance(members, list):
                    ann["member_node_ids"] = [m for m in members if m not in drop]
        elif op_type == "node_moved":
            node_id = op.get("node_id")
            if not isinstance(node_id, str):
                raise OpError("node_moved requires a string 'node_id'")
            position = _validate_position(op.get("position"))
            state["positions"][node_id] = position
            applied["position"] = position
        elif op_type == "nodes_hidden":
            state["hidden_node_ids"] = _union(
                state["hidden_node_ids"], _require_id_list(op, "node_ids")
            )
        elif op_type == "nodes_shown":
            state["hidden_node_ids"] = _remove_all(
                state["hidden_node_ids"], _require_id_list(op, "node_ids")
            )
        elif op_type == "edges_hidden":
            state["hidden_edge_ids"] = _union(
                state["hidden_edge_ids"], _require_id_list(op, "edge_ids")
            )
        elif op_type == "edges_shown":
            state["hidden_edge_ids"] = _remove_all(
                state["hidden_edge_ids"], _require_id_list(op, "edge_ids")
            )
        elif op_type == "annotation_created":
            annotation = dict(
                _validate_annotation(op.get("annotation"), require_id=False)
            )
            incoming_id = (
                annotation.get("id") if isinstance(annotation.get("id"), str) else None
            )
            if (
                incoming_id is not None
                and incoming_id in session._deleted_annotation_ids
            ):
                # A create-op retry for an id another collaborator has since
                # deleted must not resurrect it — same rule as an update
                # arriving after the delete, just below.
                return None
            existing = (
                next(
                    (a for a in state["annotations"] if a.get("id") == incoming_id),
                    None,
                )
                if incoming_id is not None
                else None
            )
            if existing is not None:
                # A retried create (lost response, resent batch) carries the same
                # client-assigned id as the one already applied: upsert so the
                # retry is idempotent instead of appending a duplicate.
                existing.update(annotation)
                existing["updated_at"] = _now_iso()
                applied["annotation"] = existing
            else:
                if len(state["annotations"]) >= self._max_annotations:
                    raise OpError("annotation limit reached for this session")
                if not isinstance(annotation.get("id"), str):
                    annotation["id"] = secrets.token_hex(8)
                annotation.setdefault("created_by", op.get("client_id"))
                annotation["updated_at"] = _now_iso()
                state["annotations"].append(annotation)
                applied["annotation"] = annotation
        elif op_type == "annotation_updated":
            incoming = _validate_annotation(op.get("annotation"), require_id=True)
            target = next(
                (a for a in state["annotations"] if a.get("id") == incoming["id"]), None
            )
            if target is None:
                return None  # update on deleted annotation is dropped (D-table rule)
            target.update(incoming)
            target["updated_at"] = _now_iso()
            applied["annotation"] = target
        elif op_type == "annotation_deleted":
            ann_id = op.get("annotation_id") or op.get("id")
            if not isinstance(ann_id, str):
                raise OpError("annotation_deleted requires a string 'annotation_id'")
            state["annotations"] = [
                a for a in state["annotations"] if a.get("id") != ann_id
            ]
            session._deleted_annotation_ids.append(ann_id)
            applied["annotation_id"] = ann_id
        elif op_type == "group_membership_changed":
            group_id = op.get("group_id")
            if not isinstance(group_id, str):
                raise OpError("group_membership_changed requires a string 'group_id'")
            members = _require_id_list(op, "member_node_ids")
            group = next(
                (
                    a
                    for a in state["annotations"]
                    if a.get("id") == group_id and a.get("kind") == "group"
                ),
                None,
            )
            if group is None:
                raise OpError(f"group annotation not found: {group_id}")
            group["member_node_ids"] = list(members)
            group["updated_at"] = _now_iso()
        elif op_type == "session_renamed":
            name = op.get("name")
            if name is not None and not isinstance(name, str):
                raise OpError("session_renamed 'name' must be a string or null")
            session.name = name
        elif op_type == "layout_applied":
            positions = op.get("positions")
            if not isinstance(positions, dict):
                raise OpError("layout_applied requires a 'positions' object")
            normalised = {
                nid: _validate_position(pos) for nid, pos in positions.items()
            }
            state["positions"].update(normalised)
            applied["positions"] = normalised

        session.seq += 1
        session.updated_at = _now_iso()
        applied["op"] = op_type
        applied["seq"] = session.seq
        applied.pop("client_id", None)
        self._rings.setdefault(session.id, deque(maxlen=self._ring_size)).append(
            applied
        )
        return applied

    def ring(self, session_id: str) -> Optional[Deque[Dict[str, Any]]]:
        """Return the per-session op ring buffer (or None), for batch rollback."""
        return self._rings.get(session_id)

    def ops_since(
        self, session_id: str, since_seq: int
    ) -> Optional[List[Dict[str, Any]]]:
        """Return applied ops with ``seq > since_seq`` from the ring buffer.

        Returns ``None`` when the ring cannot prove continuity (it was trimmed
        past ``since_seq``), signalling the caller to fall back to a full
        snapshot instead of replaying an incomplete op stream.
        """
        ring = self._rings.get(session_id)
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if since_seq >= session.seq:
            return []
        if not ring:
            return None
        oldest = ring[0]["seq"]
        if oldest > since_seq + 1:
            return None  # gap: a needed op has already been evicted
        return [op for op in ring if op["seq"] > since_seq]
