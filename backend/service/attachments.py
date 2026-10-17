"""
Node attachment operations (ADR 0008 section 7).

Authorization, node resolution, the per-route checks and the type policy all
live here, not in the REST layer, so a non-HTTP caller gets the same
behaviour: ``add_node_attachment`` takes bytes or an async iterator of bytes,
never a request object.

Every function returns the service-layer result-dict shape the rest of the
service uses: ``{"success": True, ...}``, or ``{"success": False,
"error_code": <code>, "error": <message>}`` where ``error_code`` is one of the
``ATTACHMENT_ERROR_*`` values below or ``access_denied``.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Optional,
    Union,
)

from backend.core.attachment_store import (
    AttachmentIndexCorrupt,
    AttachmentLimitExceeded,
    AttachmentLimits,
    AttachmentNodeArchived,
    AttachmentNodeGone,
    AttachmentNotFound,
    AttachmentRecord,
    AttachmentStore,
    AttachmentTooLarge,
    NewAttachment,
    StagedUpload,
    is_valid_attachment_id,
)
from backend.core.attachment_types import (
    CSV,
    DOCX,
    JPEG,
    ODP,
    ODS,
    ODT,
    PDF,
    PNG,
    PPTX,
    SUPPORTED_TYPES,
    TEXT,
    WEBP,
    XLSX,
    UnsupportedAttachmentType,
    check_allowed,
    detect_attachment_type,
)
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    GraphAccessNarrowing,
    build_graph_authorization_context,
)

from . import access

if TYPE_CHECKING:
    from backend.core import GraphStorage, Node
    from backend.runtime.authorization import GraphAuthorizationHook

logger = logging.getLogger(__name__)

FEDERATED_NODE_PREFIX = "federated::"

ATTACHMENT_ERROR_NOT_FOUND = "not_found"
ATTACHMENT_ERROR_UNAVAILABLE = "attachments_unavailable"
ATTACHMENT_ERROR_FEDERATED = "federated_node"
ATTACHMENT_ERROR_NOT_ALLOWED = "attachments_not_allowed"
ATTACHMENT_ERROR_ARCHIVED = "node_archived"
ATTACHMENT_ERROR_TOO_LARGE = "attachment_too_large"
ATTACHMENT_ERROR_NODE_LIMIT = "node_attachment_limit"
ATTACHMENT_ERROR_UNSUPPORTED_TYPE = "unsupported_attachment_type"
ATTACHMENT_ERROR_INDEX_CORRUPT = "attachment_index_corrupt"

MAX_FILENAME_BYTES = 255
DEFAULT_FILENAME = "attachment"
MAX_DECLARED_CONTENT_TYPE_CHARS = 255
_BYTES_CHUNK = 1024 * 1024

# Bidirectional formatting controls. They are not Cc characters, but they
# reorder how a name displays ("invoice<RLO>fdp.exe"), so they are removed with
# the other control characters.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


@dataclass(frozen=True)
class AttachmentSettings:
    """What the attachment routes need, fixed at boot."""

    store: AttachmentStore
    namespace: str
    limits: AttachmentLimits = AttachmentLimits()
    allowed_types: FrozenSet[str] = SUPPORTED_TYPES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"success": False, "error_code": code, "error": message}


def _not_found(node_id: str) -> Dict[str, Any]:
    return _error(ATTACHMENT_ERROR_NOT_FOUND, f"Node with ID {node_id} not found")


def _attachment_not_found() -> Dict[str, Any]:
    return _error(ATTACHMENT_ERROR_NOT_FOUND, "Attachment not found")


def _unavailable() -> Dict[str, Any]:
    return _error(
        ATTACHMENT_ERROR_UNAVAILABLE,
        "Node attachments are not configured on this instance.",
    )


def _index_corrupt() -> Dict[str, Any]:
    return _error(
        ATTACHMENT_ERROR_INDEX_CORRUPT,
        "The attachment index for this node could not be read.",
    )


def is_federated_node_id(node_id: str) -> bool:
    return node_id.startswith(FEDERATED_NODE_PREFIX)


def _node_type_name(node: "Node") -> str:
    return node.type.value if hasattr(node.type, "value") else str(node.type)


def type_allows_attachments(node: "Node") -> bool:
    from backend.config import config_loader

    cfg = config_loader.get_schema()["node_types"].get(_node_type_name(node))
    return bool(cfg) and cfg.get("allows_attachments") is True


def _uploads_allowed(node: "Node") -> bool:
    return (
        not is_federated_node_id(node.id)
        and type_allows_attachments(node)
        and not node.archived
    )


def serialize_attachment(record: AttachmentRecord) -> Dict[str, Any]:
    """A record as clients see it: the declared type is diagnostic only."""
    data = record.to_dict()
    data.pop("declared_content_type", None)
    return data


def sanitize_filename(raw: Optional[str]) -> str:
    """Turn the client's file name into the stored display name (ADR 0008 sec 7)."""
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        ch
        for ch in name
        if unicodedata.category(ch) != "Cc" and ch not in _BIDI_CONTROLS
    )
    name = name.strip()
    encoded = name.encode("utf-8")
    if len(encoded) > MAX_FILENAME_BYTES:
        name = encoded[:MAX_FILENAME_BYTES].decode("utf-8", errors="ignore")
    return name or DEFAULT_FILENAME


# The extensions a file of each detected type may keep. A name whose last
# extension is not one of these gets the first one appended, so what the
# browser saves cannot open as something else: "report.docm" (macros) is saved
# as "report.docm.docx", and Office does not run macros from a .docx.
_TYPE_EXTENSIONS = {
    PDF: (".pdf",),
    PNG: (".png",),
    JPEG: (".jpg", ".jpeg"),
    WEBP: (".webp",),
    DOCX: (".docx",),
    XLSX: (".xlsx",),
    PPTX: (".pptx",),
    ODT: (".odt",),
    ODS: (".ods",),
    ODP: (".odp",),
    CSV: (".csv",),
}
# Plain text keeps its extension only when it is one a desktop opens as inert
# text; anything else (.bat, .js, .rtf, .rdp, .py, ...) gets ".txt" appended.
# An allowlist rather than a denylist, because the set of extensions some
# program runs or renders has no end.
_INERT_TEXT_EXTENSIONS = frozenset(
    {
        "",
        ".adoc",
        ".bib",
        ".cfg",
        ".conf",
        ".ini",
        ".json",
        ".jsonl",
        ".log",
        ".markdown",
        ".md",
        ".ndjson",
        ".org",
        ".rst",
        ".srt",
        ".tab",
        ".tex",
        ".text",
        ".toml",
        ".tsv",
        ".txt",
        ".vtt",
        ".yaml",
        ".yml",
    }
)


def _with_suffix(name: str, suffix: str) -> str:
    base = name.rstrip(". ")
    encoded = base.encode("utf-8")
    room = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    if len(encoded) > room:
        base = encoded[:room].decode("utf-8", errors="ignore")
    return base + suffix


def align_extension(name: str, content_type: str) -> str:
    """Make the stored name's extension agree with the detected type.

    The bytes are never changed; only the display name, which is what a
    browser saves the download under. Trailing dots and spaces are ignored
    when reading the extension, because Windows drops them when saving.
    """
    _, dot, extension = name.rstrip(". ").rpartition(".")
    extension = f".{extension.lower()}" if dot else ""
    if content_type == TEXT:
        if extension in _INERT_TEXT_EXTENSIONS:
            return name
        return _with_suffix(name, ".txt")
    allowed = _TYPE_EXTENSIONS.get(content_type)
    if allowed is None or extension in allowed:
        return name
    return _with_suffix(name, allowed[0])


def _sanitize_declared_content_type(raw: Optional[str]) -> str:
    value = "".join(ch for ch in (raw or "") if ch.isprintable()).strip()
    return value[:MAX_DECLARED_CONTENT_TYPE_CHARS]


def _current_actor_id(target: str) -> Optional[str]:
    context = build_graph_authorization_context(
        action=GRAPH_ACTION_MUTATE, target=target
    )
    return context.actor.get("actor_id") or None


def _authorize_and_resolve(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    settings: Optional[AttachmentSettings],
    *,
    action: str,
    target: str,
    node_id: str,
) -> tuple[Optional[Dict[str, Any]], Optional["Node"], GraphAccessNarrowing]:
    """Run the checks every route shares, in the order ADR 0008 sec 7 lists."""
    decision = access.evaluate_graph_access(hook, action=action, target=target)
    if not decision.allowed:
        denied = access.build_access_denied_result(
            action=action, target=target, decision=decision
        )
        return denied, None, decision.graph_access
    if settings is None:
        return _unavailable(), None, decision.graph_access
    node = storage.get_node(node_id)
    if node is None or not access.is_node_visible(node, decision.graph_access):
        return _not_found(node_id), None, decision.graph_access
    return None, node, decision.graph_access


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_node_attachments(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    settings: Optional[AttachmentSettings],
    node_id: str,
) -> Dict[str, Any]:
    error, node, _ = _authorize_and_resolve(
        storage,
        hook,
        settings,
        action=GRAPH_ACTION_READ,
        target="list_node_attachments",
        node_id=node_id,
    )
    if error is not None:
        return error
    limits = settings.limits.to_dict()
    if is_federated_node_id(node.id):
        return {
            "success": True,
            "attachments": [],
            "limits": limits,
            "uploads_allowed": False,
        }
    try:
        records = settings.store.list(settings.namespace, node.id)
    except AttachmentIndexCorrupt:
        logger.exception("Attachment index unreadable for a node")
        return _index_corrupt()
    return {
        "success": True,
        "attachments": [serialize_attachment(r) for r in records],
        "limits": limits,
        "uploads_allowed": _uploads_allowed(node),
    }


def get_node_attachment_record(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    settings: Optional[AttachmentSettings],
    node_id: str,
    attachment_id: str,
) -> Dict[str, Any]:
    """Look up one attachment's record without opening its bytes."""
    error, node, _ = _authorize_and_resolve(
        storage,
        hook,
        settings,
        action=GRAPH_ACTION_READ,
        target="get_node_attachment",
        node_id=node_id,
    )
    if error is not None:
        return error
    if is_federated_node_id(node.id) or not is_valid_attachment_id(attachment_id):
        return _attachment_not_found()
    try:
        record = settings.store.get_record(settings.namespace, node.id, attachment_id)
    except AttachmentIndexCorrupt:
        logger.exception("Attachment index unreadable for a node")
        return _index_corrupt()
    if record is None:
        return _attachment_not_found()
    return {"success": True, "attachment": serialize_attachment(record)}


def open_node_attachment(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    settings: Optional[AttachmentSettings],
    node_id: str,
    attachment_id: str,
) -> Dict[str, Any]:
    """Authorize and open one attachment for reading.

    On success the result carries ``attachment`` (the record) and ``stream``,
    an open binary file the caller must close.
    """
    error, node, _ = _authorize_and_resolve(
        storage,
        hook,
        settings,
        action=GRAPH_ACTION_READ,
        target="get_node_attachment",
        node_id=node_id,
    )
    if error is not None:
        return error
    if is_federated_node_id(node.id) or not is_valid_attachment_id(attachment_id):
        return _attachment_not_found()
    try:
        record = settings.store.get_record(settings.namespace, node.id, attachment_id)
        if record is None:
            return _attachment_not_found()
        stream = settings.store.open_read(settings.namespace, node.id, attachment_id)
    except AttachmentNotFound:
        return _attachment_not_found()
    except AttachmentIndexCorrupt:
        logger.exception("Attachment index unreadable for a node")
        return _index_corrupt()
    return {
        "success": True,
        "attachment": serialize_attachment(record),
        "stream": stream,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def delete_node_attachment(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    settings: Optional[AttachmentSettings],
    node_id: str,
    attachment_id: str,
) -> Dict[str, Any]:
    error, node, _ = _authorize_and_resolve(
        storage,
        hook,
        settings,
        action=GRAPH_ACTION_MUTATE,
        target="delete_node_attachment",
        node_id=node_id,
    )
    if error is not None:
        return error
    if is_federated_node_id(node.id) or not is_valid_attachment_id(attachment_id):
        return _attachment_not_found()
    try:
        deleted = settings.store.delete(settings.namespace, node.id, attachment_id)
    except AttachmentIndexCorrupt:
        logger.exception("Attachment index unreadable for a node")
        return _index_corrupt()
    if not deleted:
        return _attachment_not_found()
    return {"success": True}


async def _iterate_bytes(content: bytes) -> AsyncIterator[bytes]:
    view = memoryview(content)
    for start in range(0, len(view), _BYTES_CHUNK):
        yield bytes(view[start : start + _BYTES_CHUNK])


def _cap_breach(cap_is_file_limit: bool, limits: AttachmentLimits) -> Dict[str, Any]:
    if cap_is_file_limit:
        return _error(
            ATTACHMENT_ERROR_TOO_LARGE,
            f"The file exceeds the {limits.max_file_bytes}-byte limit per file.",
        )
    return _error(
        ATTACHMENT_ERROR_NODE_LIMIT,
        f"The file would take the node past its {limits.max_bytes_per_node}-byte "
        "attachment total.",
    )


async def add_node_attachment(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    settings: Optional[AttachmentSettings],
    node_id: str,
    *,
    filename: Optional[str],
    declared_content_type: Optional[str],
    content: Union[bytes, AsyncIterator[bytes]],
    content_length: Optional[int] = None,
) -> Dict[str, Any]:
    """Store one file on a node.

    ``content`` is the whole file as bytes, or an async iterator of chunks
    that is not consumed until every pre-check has passed. ``content_length``
    is the size the caller declares, when it knows one; it only lets an
    oversized upload be refused before any byte is read, and the bytes
    actually received are capped either way.
    """
    target = "add_node_attachment"
    error, node, graph_access = _authorize_and_resolve(
        storage,
        hook,
        settings,
        action=GRAPH_ACTION_MUTATE,
        target=target,
        node_id=node_id,
    )
    if error is not None:
        return error
    if is_federated_node_id(node.id):
        return _error(
            ATTACHMENT_ERROR_FEDERATED,
            "Files cannot be attached to a node federated from another graph.",
        )
    if not type_allows_attachments(node):
        return _error(
            ATTACHMENT_ERROR_NOT_ALLOWED,
            f"Node type {_node_type_name(node)!r} does not accept attachments.",
        )
    if node.archived:
        return _error(
            ATTACHMENT_ERROR_ARCHIVED, "Files cannot be attached to an archived node."
        )

    limits = settings.limits
    store = settings.store
    namespace = settings.namespace
    try:
        records = store.list(namespace, node.id)
    except AttachmentIndexCorrupt:
        logger.exception("Attachment index unreadable for a node")
        return _index_corrupt()
    if len(records) >= limits.max_files_per_node:
        return _error(
            ATTACHMENT_ERROR_NODE_LIMIT,
            f"The node already has {limits.max_files_per_node} attachments.",
        )
    remaining = max(limits.max_bytes_per_node - sum(r.size for r in records), 0)
    # The smaller cap is the one reported; a tie reads as the per-file limit.
    cap_is_file_limit = limits.max_file_bytes <= remaining
    cap = limits.max_file_bytes if cap_is_file_limit else remaining

    if isinstance(content, (bytes, bytearray, memoryview)):
        content = bytes(content)
        if content_length is None:
            content_length = len(content)
        chunks: AsyncIterator[bytes] = _iterate_bytes(content)
    else:
        chunks = content
    if content_length is not None and content_length > cap:
        return _cap_breach(cap_is_file_limit, limits)

    stored_name = sanitize_filename(filename)
    uploaded_by = _current_actor_id(target)
    declared = _sanitize_declared_content_type(declared_content_type)
    try:
        staged = await store.stage(namespace, node.id, chunks, cap)
    except AttachmentTooLarge:
        return _cap_breach(cap_is_file_limit, limits)

    # Whatever happens from here - a refusal, an error, a cancelled request -
    # the staged file goes. After a successful commit it has already been
    # moved into place, and the discard is a no-op.
    try:
        try:
            content_type = await asyncio.to_thread(
                _sniff_staged, staged, stored_name, settings.allowed_types
            )
        except UnsupportedAttachmentType:
            return _error(
                ATTACHMENT_ERROR_UNSUPPORTED_TYPE,
                "This file type cannot be attached.",
            )
        record = await store.commit(
            staged,
            NewAttachment(
                filename=align_extension(stored_name, content_type),
                declared_content_type=declared,
                content_type=content_type,
                uploaded_by=uploaded_by,
            ),
            limits,
            _node_still_accepts_uploads(storage, node.id, graph_access),
        )
    except AttachmentNodeGone:
        return _not_found(node_id)
    except AttachmentNodeArchived:
        return _error(
            ATTACHMENT_ERROR_ARCHIVED, "Files cannot be attached to an archived node."
        )
    except AttachmentTooLarge:
        return _cap_breach(True, limits)
    except AttachmentLimitExceeded:
        return _error(
            ATTACHMENT_ERROR_NODE_LIMIT,
            "The file would take the node past its attachment limits.",
        )
    except AttachmentIndexCorrupt:
        logger.exception("Attachment index unreadable for a node")
        return _index_corrupt()
    finally:
        store.discard(staged)
    return {"success": True, "attachment": serialize_attachment(record)}


def _sniff_staged(
    staged: StagedUpload, stored_name: str, allowed_types: FrozenSet[str]
) -> str:
    with staged.open() as staged_file:
        content_type = detect_attachment_type(staged_file, stored_name)
    check_allowed(content_type, allowed_types)
    return content_type


def _node_still_accepts_uploads(
    storage: "GraphStorage", node_id: str, graph_access: GraphAccessNarrowing
) -> Callable[[], None]:
    """The commit precondition: re-read the node under the store's lock."""

    def precondition() -> None:
        node = storage.get_node(node_id)
        if node is None or not access.is_node_visible(node, graph_access):
            raise AttachmentNodeGone(node_id)
        if node.archived:
            raise AttachmentNodeArchived(node_id)

    return precondition


def leftover_attachments_of_absent_nodes(
    storage: "GraphStorage",
    settings: Optional[AttachmentSettings],
    node_ids: Iterable[str],
) -> Dict[str, FrozenSet[str]]:
    """Attachment ids already stored under node ids the graph does not hold.

    Taken just before those ids are created (ADR 0008 sec 8). Whatever is
    recorded under an id that has no node belongs to an earlier node with the
    same id, and must not surface as the new node's files.
    """
    leftovers: Dict[str, FrozenSet[str]] = {}
    if settings is None:
        return leftovers
    for node_id in node_ids:
        if storage.get_node(node_id) is not None:
            continue
        try:
            ids = frozenset(
                r.id for r in settings.store.list(settings.namespace, node_id)
            )
        except Exception:
            logger.exception("Reading a reused node id's leftover attachments failed")
            continue
        if ids:
            leftovers[node_id] = ids
    return leftovers


def quarantine_leftover_attachments(
    settings: Optional[AttachmentSettings],
    leftovers: Dict[str, FrozenSet[str]],
    created_node_ids: Iterable[str],
) -> None:
    """Move the leftovers of node ids that were just created into quarantine.

    Only after the create succeeded: a refused create means the id was taken
    (possibly by a node this instance has not seen yet), and its files stay.
    Only the snapshot's ids: a file uploaded to the new node in between is
    the new node's own. A failure is logged and never fails the create.
    """
    if settings is None:
        return
    for node_id in created_node_ids:
        ids = leftovers.get(node_id)
        if not ids:
            continue
        try:
            moved = settings.store.quarantine(settings.namespace, node_id, ids)
        except Exception:
            logger.exception("Quarantining a reused node id's attachments failed")
            continue
        if moved:
            logger.warning(
                "Quarantined %d attachment(s) left under a reused node id", moved
            )


def delete_attachments_of_nodes(
    settings: Optional[AttachmentSettings], node_ids: list[str]
) -> None:
    """Remove the files of nodes the graph has already deleted (ADR 0008 sec 8).

    Called only after the graph delete succeeded. A failure here leaves an
    orphan node directory for the validator to report; it never fails the
    graph delete that already happened.
    """
    if settings is None:
        return
    for node_id in node_ids:
        try:
            settings.store.delete_node(settings.namespace, node_id)
        except Exception:
            logger.exception("Removing a deleted node's attachments failed")
