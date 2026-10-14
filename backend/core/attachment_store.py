"""
Node attachment storage seam (ADR 0008).

A narrow store for the bytes of files attached to graph nodes, deliberately
beside the graph persistence seam rather than inside it: graph entities are
held in memory by ``GraphStorage``, attachment bytes must stream and are never
held whole.

Addressing (ADR 0008 section 2): no user input ever reaches a path. The
namespace and the node id are hashed into fixed hex segments, blobs are named
by a server-minted attachment id, and the original file name is metadata only.
The per-node ``index.json`` is the single source of truth for which files a
node has; an id with no index record is "not found", whatever is on disk.

Type policy (sniffing) and authorization live in the service layer
(``backend/service/attachments.py``); this module only moves bytes and keeps
the index consistent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    AsyncIterator,
    BinaryIO,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Protocol,
)

from .storage_backends import _lock_file, _unlock_file

INDEX_SCHEMA_VERSION = 1
ATTACHMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

MIB = 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 25 * MIB
DEFAULT_MAX_FILES_PER_NODE = 10
DEFAULT_MAX_BYTES_PER_NODE = 100 * MIB

# Staged uploads older than this are removed by the start-up sweep. An upload
# still in flight keeps writing, and so keeps its mtime fresh (ADR 0008 sec 8).
STALE_STAGED_UPLOAD_SECONDS = 3600

_INDEX_FILE = "index.json"
_BLOBS_DIR = "blobs"
_LOCKS_DIR = ".locks"
_TMP_DIR = ".tmp"
_COPY_CHUNK = 1024 * 1024


class AttachmentStoreError(Exception):
    """Base class for attachment store errors."""


class AttachmentTooLarge(AttachmentStoreError):
    """More bytes arrived than the cap the upload was staged with."""


class AttachmentLimitExceeded(AttachmentStoreError):
    """Committing would take the node past its count or total-bytes limit."""


class AttachmentNotFound(AttachmentStoreError):
    """The attachment id has no record in the node's index."""


class AttachmentNodeGone(AttachmentStoreError):
    """The node was deleted while the upload was in flight."""


class AttachmentNodeArchived(AttachmentStoreError):
    """The node was archived while the upload was in flight."""


class AttachmentIndexCorrupt(AttachmentStoreError):
    """A node's index document is unreadable or not schema-valid."""


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    node_id: str
    filename: str
    size: int
    content_type: str
    declared_content_type: str
    sha256: str
    uploaded_at: str
    uploaded_by: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NewAttachment:
    filename: str
    declared_content_type: str
    content_type: str
    uploaded_by: Optional[str]


@dataclass(frozen=True)
class AttachmentLimits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files_per_node: int = DEFAULT_MAX_FILES_PER_NODE
    max_bytes_per_node: int = DEFAULT_MAX_BYTES_PER_NODE

    def __post_init__(self) -> None:
        for name in ("max_file_bytes", "max_files_per_node", "max_bytes_per_node"):
            value = getattr(self, name)
            # bool is an int subclass; True would read as a limit of 1.
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class StagedUpload:
    """Bytes written to the staging area, not yet visible to any reader."""

    namespace: str
    node_id: str
    path: Path
    size: int
    sha256: str

    def open(self) -> BinaryIO:
        return open(self.path, "rb")


class AttachmentStore(Protocol):
    def list(self, namespace: str, node_id: str) -> List[AttachmentRecord]: ...

    def get_record(
        self, namespace: str, node_id: str, attachment_id: str
    ) -> Optional[AttachmentRecord]: ...

    def open_read(
        self, namespace: str, node_id: str, attachment_id: str
    ) -> BinaryIO: ...

    async def stage(
        self,
        namespace: str,
        node_id: str,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
    ) -> StagedUpload: ...

    async def commit(
        self,
        staged: StagedUpload,
        record: NewAttachment,
        limits: AttachmentLimits,
        precondition: Callable[[], None],
    ) -> AttachmentRecord: ...

    def discard(self, staged: StagedUpload) -> None: ...

    def delete(self, namespace: str, node_id: str, attachment_id: str) -> bool: ...

    def delete_node(self, namespace: str, node_id: str) -> int: ...

    def iter_nodes(self, namespace: str) -> Iterator[str]: ...


def hashed_key(value: str) -> str:
    """The fixed, path-safe segment for a namespace or a node id."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_valid_attachment_id(attachment_id: str) -> bool:
    return bool(ATTACHMENT_ID_PATTERN.fullmatch(attachment_id or ""))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_RECORD_FIELDS = {
    "id": str,
    "filename": str,
    "size": int,
    "content_type": str,
    "declared_content_type": str,
    "sha256": str,
    "uploaded_at": str,
}


def _record_from_json(node_id: str, raw: object) -> AttachmentRecord:
    if not isinstance(raw, dict):
        raise AttachmentIndexCorrupt("index record is not an object")
    for key, kind in _RECORD_FIELDS.items():
        value = raw.get(key)
        if not isinstance(value, kind) or isinstance(value, bool):
            raise AttachmentIndexCorrupt(f"index record field {key!r} is invalid")
    if not is_valid_attachment_id(raw["id"]):
        raise AttachmentIndexCorrupt("index record id is malformed")
    uploaded_by = raw.get("uploaded_by")
    if uploaded_by is not None and not isinstance(uploaded_by, str):
        raise AttachmentIndexCorrupt("index record field 'uploaded_by' is invalid")
    return AttachmentRecord(
        id=raw["id"],
        node_id=node_id,
        filename=raw["filename"],
        size=raw["size"],
        content_type=raw["content_type"],
        declared_content_type=raw["declared_content_type"],
        sha256=raw["sha256"],
        uploaded_at=raw["uploaded_at"],
        uploaded_by=uploaded_by,
    )


def _record_to_json(record: AttachmentRecord) -> Dict[str, object]:
    data = record.to_dict()
    data.pop("node_id")
    return data


class FileAttachmentStore:
    """The file-backed default store (ADR 0008 section 11).

    Layout under ``root``::

        <namespace_key>/<node_key>/index.json
        <namespace_key>/<node_key>/blobs/<attachment_id>
        <namespace_key>/.locks/<node_key>.lock
        .tmp/                       # staged uploads

    ``.tmp`` sits under the same root, so committing a staged file is an
    atomic rename on one filesystem.
    """

    def __init__(self, root: os.PathLike | str):
        self.root = Path(root)
        self._thread_locks: Dict[str, threading.Lock] = {}
        self._thread_locks_guard = threading.Lock()

    # -- paths ---------------------------------------------------------------

    def _namespace_dir(self, namespace: str) -> Path:
        return self.root / hashed_key(namespace)

    def _node_dir(self, namespace: str, node_id: str) -> Path:
        return self._namespace_dir(namespace) / hashed_key(node_id)

    def _index_path(self, namespace: str, node_id: str) -> Path:
        return self._node_dir(namespace, node_id) / _INDEX_FILE

    def _blob_path(self, namespace: str, node_id: str, attachment_id: str) -> Path:
        # Callers pass only ids that came out of the index (or were just
        # minted); the pattern check keeps that a structural guarantee.
        if not is_valid_attachment_id(attachment_id):
            raise AttachmentNotFound(attachment_id)
        return self._node_dir(namespace, node_id) / _BLOBS_DIR / attachment_id

    def _tmp_dir(self) -> Path:
        return self.root / _TMP_DIR

    # -- locking -------------------------------------------------------------

    @contextmanager
    def _node_lock(self, namespace: str, node_id: str) -> Iterator[None]:
        """In-process lock plus an OS lock on a file outside the node dir.

        The lock file is never deleted, so ``delete_node`` removing the node
        directory cannot split the lock between two holders.
        """
        key = hashed_key(node_id)
        lock_dir = self._namespace_dir(namespace) / _LOCKS_DIR
        lock_id = f"{hashed_key(namespace)}/{key}"
        with self._thread_locks_guard:
            thread_lock = self._thread_locks.setdefault(lock_id, threading.Lock())
        with thread_lock:
            lock_dir.mkdir(parents=True, exist_ok=True)
            with open(lock_dir / f"{key}.lock", "a+b") as handle:
                _lock_file(handle, exclusive=True)
                try:
                    yield
                finally:
                    _unlock_file(handle)

    # -- index ---------------------------------------------------------------

    def _read_index(self, namespace: str, node_id: str) -> List[AttachmentRecord]:
        path = self._index_path(namespace, node_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise AttachmentIndexCorrupt(f"index for node is not JSON: {exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != INDEX_SCHEMA_VERSION
            or data.get("node_id") != node_id
            or not isinstance(data.get("attachments"), list)
        ):
            raise AttachmentIndexCorrupt("index document is not schema-valid")
        return [_record_from_json(node_id, item) for item in data["attachments"]]

    def _write_index(
        self, namespace: str, node_id: str, records: List[AttachmentRecord]
    ) -> None:
        node_dir = self._node_dir(namespace, node_id)
        node_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "namespace": namespace,
            "node_id": node_id,
            "attachments": [_record_to_json(r) for r in records],
        }
        fd, temp_path = tempfile.mkstemp(prefix=".index_", suffix=".json", dir=node_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, node_dir / _INDEX_FILE)
        except BaseException:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

    # -- reads ---------------------------------------------------------------

    def list(self, namespace: str, node_id: str) -> List[AttachmentRecord]:
        return self._read_index(namespace, node_id)

    def get_record(
        self, namespace: str, node_id: str, attachment_id: str
    ) -> Optional[AttachmentRecord]:
        if not is_valid_attachment_id(attachment_id):
            return None
        for record in self._read_index(namespace, node_id):
            if record.id == attachment_id:
                return record
        return None

    def open_read(self, namespace: str, node_id: str, attachment_id: str) -> BinaryIO:
        record = self.get_record(namespace, node_id, attachment_id)
        if record is None:
            raise AttachmentNotFound(attachment_id)
        try:
            return open(self._blob_path(namespace, node_id, record.id), "rb")
        except FileNotFoundError as exc:
            # Deleted between the index read and the open: delete rewrites the
            # index first, so this is the same as "not found".
            raise AttachmentNotFound(attachment_id) from exc

    def iter_nodes(self, namespace: str) -> Iterator[str]:
        namespace_dir = self._namespace_dir(namespace)
        if not namespace_dir.is_dir():
            return
        for child in sorted(namespace_dir.iterdir()):
            index_path = child / _INDEX_FILE
            if not index_path.is_file():
                continue
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            node_id = data.get("node_id") if isinstance(data, dict) else None
            if isinstance(node_id, str) and hashed_key(node_id) == child.name:
                yield node_id

    # -- upload --------------------------------------------------------------

    async def stage(
        self,
        namespace: str,
        node_id: str,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
    ) -> StagedUpload:
        tmp_dir = self._tmp_dir()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="upload_", dir=tmp_dir)
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise AttachmentTooLarge(
                            f"upload exceeds the {max_bytes}-byte cap"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                await asyncio.to_thread(os.fsync, handle.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return StagedUpload(
            namespace=namespace,
            node_id=node_id,
            path=temp_path,
            size=size,
            sha256=digest.hexdigest(),
        )

    def discard(self, staged: StagedUpload) -> None:
        staged.path.unlink(missing_ok=True)

    async def commit(
        self,
        staged: StagedUpload,
        record: NewAttachment,
        limits: AttachmentLimits,
        precondition: Callable[[], None],
    ) -> AttachmentRecord:
        # The OS lock blocks, so the whole critical section runs off the loop.
        return await asyncio.to_thread(
            self._commit_sync, staged, record, limits, precondition
        )

    def _commit_sync(
        self,
        staged: StagedUpload,
        record: NewAttachment,
        limits: AttachmentLimits,
        precondition: Callable[[], None],
    ) -> AttachmentRecord:
        namespace, node_id = staged.namespace, staged.node_id
        try:
            with self._node_lock(namespace, node_id):
                precondition()
                records = self._read_index(namespace, node_id)
                if staged.size > limits.max_file_bytes:
                    raise AttachmentTooLarge(
                        f"upload exceeds the {limits.max_file_bytes}-byte file limit"
                    )
                # Re-checked against the index as it stands now, under the
                # lock: two uploads that each passed an earlier check cannot
                # exceed the limit together.
                if len(records) >= limits.max_files_per_node:
                    raise AttachmentLimitExceeded("node file-count limit reached")
                used = sum(r.size for r in records)
                if used + staged.size > limits.max_bytes_per_node:
                    raise AttachmentLimitExceeded("node total-bytes limit reached")

                new_record = AttachmentRecord(
                    id=uuid.uuid4().hex,
                    node_id=node_id,
                    filename=record.filename,
                    size=staged.size,
                    content_type=record.content_type,
                    declared_content_type=record.declared_content_type,
                    sha256=staged.sha256,
                    uploaded_at=_utc_now_iso(),
                    uploaded_by=record.uploaded_by,
                )
                blob_path = self._blob_path(namespace, node_id, new_record.id)
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                # Blob first, then index: a crash in between leaves an
                # unindexed blob, never a record without a blob.
                os.replace(staged.path, blob_path)
                try:
                    self._write_index(namespace, node_id, records + [new_record])
                except BaseException:
                    blob_path.unlink(missing_ok=True)
                    raise
                return new_record
        finally:
            self.discard(staged)

    # -- delete --------------------------------------------------------------

    def delete(self, namespace: str, node_id: str, attachment_id: str) -> bool:
        if not is_valid_attachment_id(attachment_id):
            return False
        with self._node_lock(namespace, node_id):
            records = self._read_index(namespace, node_id)
            remaining = [r for r in records if r.id != attachment_id]
            if len(remaining) == len(records):
                return False
            # Index first, then blob: a record with no blob is never observable.
            self._write_index(namespace, node_id, remaining)
            self._blob_path(namespace, node_id, attachment_id).unlink(missing_ok=True)
            return True

    def delete_node(self, namespace: str, node_id: str) -> int:
        with self._node_lock(namespace, node_id):
            node_dir = self._node_dir(namespace, node_id)
            if not node_dir.exists():
                return 0
            try:
                count = len(self._read_index(namespace, node_id))
            except AttachmentIndexCorrupt:
                count = 0
            shutil.rmtree(node_dir)
            return count

    # -- maintenance ---------------------------------------------------------

    def sweep_stale_staged(
        self, max_age_seconds: float = STALE_STAGED_UPLOAD_SECONDS
    ) -> int:
        """Remove staged uploads whose mtime is older than ``max_age_seconds``."""
        tmp_dir = self._tmp_dir()
        if not tmp_dir.is_dir():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for entry in tmp_dir.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed


def copy_stream(source: BinaryIO, chunk_size: int = _COPY_CHUNK) -> Iterator[bytes]:
    """Yield ``source`` in chunks and close it, for streamed downloads."""
    try:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        source.close()
