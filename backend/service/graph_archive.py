"""
Vector-aware export/import archive: a ZIP containing ``graph.json`` (the same
shape ``GET /export`` returns), ``embeddings.bin`` (the embedding sidecar's own
binary format, verbatim — see ``backend/core/embedding_sidecar.py``) when the
live graph has vectors, and a ``manifest.json`` naming per-member SHA-256
checksums plus the embedding model/dimension the vectors were generated with.

See docs/adr/0007-vector-aware-export-archive.md for the design and
docs/DATA_MANAGEMENT.md's "Importing a vector-aware archive" for the format
and outcome table.

Nothing here touches ``GraphStorage``. Building an archive is a pure function
of an export document plus a vector dict; extracting one is a pure function of
its bytes. ``backend/service/import_service.py`` is what wires extraction into
the same validate -> backup -> replace pipeline ``POST /import`` uses.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.core.embedding_sidecar import (
    EmbeddingSidecarError,
    parse_sidecar_bytes,
    serialize_sidecar_bytes,
)

ARCHIVE_SCHEMA_VERSION = "1.0"
GRAPH_MEMBER = "graph.json"
EMBEDDINGS_MEMBER = "embeddings.bin"
MANIFEST_MEMBER = "manifest.json"


class ArchiveIntegrityError(Exception):
    """The archive itself cannot be trusted: the ZIP is unreadable, a member's
    bytes do not match the checksum ``manifest.json`` declares for it, the
    manifest names a member the archive does not have, or the archive has no
    ``graph.json`` at all. Always raised before anything is read out of the
    archive for use — see ``backend.service.import_service.import_graph_archive``,
    which never writes to the live graph once this has been raised.
    """


@dataclass(frozen=True)
class ExtractedArchive:
    """What ``extract_archive`` found, decoupled from what the caller then
    does about it (that decision lives in ``import_service``, which is also
    what needs the ``GraphAuthorizationHook``/``ExecutionStore`` this module
    has no business depending on).
    """

    graph_document: Any
    embedding_vectors: Optional[Dict[str, Any]]
    embedding_model: Optional[str]
    embedding_dimension: Optional[int]
    compatible: bool
    regeneration_reason: Optional[str]
    manifest: Optional[Dict[str, Any]]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_archive_bytes(
    export_document: Dict[str, Any],
    *,
    embedding_vectors: Dict[str, Any],
    embedding_model: str,
) -> bytes:
    """Build the ZIP bytes for ``GET /export/archive``.

    ``export_document`` is exactly what ``views.export_graph`` returns —
    nothing about its shape is changed here. ``embedding_vectors`` is expected
    to already be narrowed to the node ids present in that document (the
    caller, ``views.export_graph_archive``, does that narrowing so an
    archive never carries a vector for a node the export itself excluded).

    ``embeddings.bin`` is omitted entirely when ``embedding_vectors`` is
    empty — there is nothing to restore, and the resulting archive is exactly
    the "someone zipped a plain graph.json" shape ``extract_archive`` already
    has to handle gracefully on import.
    """
    graph_bytes = json.dumps(export_document, ensure_ascii=False).encode("utf-8")

    embeddings_bytes: Optional[bytes] = None
    embedding_dimension: Optional[int] = None
    if embedding_vectors:
        embeddings_bytes = serialize_sidecar_bytes(embedding_vectors)
        embedding_dimension = len(next(iter(embedding_vectors.values())))

    checksums = {GRAPH_MEMBER: f"sha256:{_sha256_hex(graph_bytes)}"}
    if embeddings_bytes is not None:
        checksums[EMBEDDINGS_MEMBER] = f"sha256:{_sha256_hex(embeddings_bytes)}"

    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "export_date": datetime.now(timezone.utc).isoformat(),
        "embedding_model": embedding_model,
        "embedding_dimension": embedding_dimension,
        "node_count": len(export_document.get("nodes") or []),
        "edge_count": len(export_document.get("edges") or []),
        "checksums": checksums,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(GRAPH_MEMBER, graph_bytes)
        if embeddings_bytes is not None:
            zf.writestr(EMBEDDINGS_MEMBER, embeddings_bytes)
        zf.writestr(MANIFEST_MEMBER, manifest_bytes)
    return buffer.getvalue()


def _validate_manifest_shape(parsed: Any) -> Dict[str, Any]:
    """Check that a parsed ``manifest.json`` has the fields this module
    actually relies on. Raises ``ValueError`` — caught by ``extract_archive``
    and folded into "unreadable manifest", never a hard rejection on its own:
    a manifest that fails to parse or fails this shape check carries no
    checksum promise, so there is nothing for it to have broken (see the ADR,
    "Why an unreadable manifest degrades instead of rejecting").
    """
    if not isinstance(parsed, dict):
        raise ValueError("manifest.json is not a JSON object")
    if not isinstance(parsed.get("schema_version"), str):
        raise ValueError("manifest.json has no string 'schema_version'")
    checksums = parsed.get("checksums")
    if not isinstance(checksums, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in checksums.items()
    ):
        raise ValueError(
            "manifest.json 'checksums' must be an object of string -> string"
        )
    embedding_model = parsed.get("embedding_model")
    if embedding_model is not None and not isinstance(embedding_model, str):
        raise ValueError("manifest.json 'embedding_model' must be a string or null")
    embedding_dimension = parsed.get("embedding_dimension")
    if embedding_dimension is not None and (
        isinstance(embedding_dimension, bool)
        or not isinstance(embedding_dimension, int)
    ):
        raise ValueError(
            "manifest.json 'embedding_dimension' must be an integer or null"
        )
    return parsed


def extract_archive(data: bytes, *, live_model_name: str) -> ExtractedArchive:
    """Open, checksum-verify and interpret an import archive.

    Order of operations matters: every checksum the manifest declares is
    verified before anything is decoded for use, so a tampered or corrupted
    member is always caught here rather than reaching the graph importer.
    Only members the manifest actually lists a checksum for are checked —
    see the ADR for why an archive missing a manifest, or missing an entry
    for one member, degrades instead of failing outright: there is no
    checksum promise for it to have broken.

    Raises:
        ArchiveIntegrityError: the ZIP cannot be opened, ``graph.json`` is
            missing, or a member's bytes disagree with a checksum the
            manifest declared for it. Nothing has been written anywhere when
            this is raised.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveIntegrityError(f"not a valid ZIP archive: {exc}") from exc

    bad_member = zf.testzip()
    if bad_member is not None:
        raise ArchiveIntegrityError(
            f"archive member '{bad_member}' failed its own CRC check — the "
            f"archive is corrupted"
        )

    names = set(zf.namelist())
    if GRAPH_MEMBER not in names:
        raise ArchiveIntegrityError(
            f"archive is missing required member '{GRAPH_MEMBER}'"
        )

    manifest: Optional[Dict[str, Any]] = None
    manifest_problem: Optional[str] = None
    if MANIFEST_MEMBER in names:
        try:
            manifest = _validate_manifest_shape(
                json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            manifest_problem = f"{MANIFEST_MEMBER} is not usable: {exc}"
    else:
        manifest_problem = f"archive has no {MANIFEST_MEMBER}"

    if manifest is not None:
        for member_name, expected in manifest["checksums"].items():
            if member_name not in names:
                raise ArchiveIntegrityError(
                    f"{MANIFEST_MEMBER} declares a checksum for '{member_name}', "
                    f"but the archive has no such member"
                )
            actual = f"sha256:{_sha256_hex(zf.read(member_name))}"
            if actual != expected:
                raise ArchiveIntegrityError(
                    f"'{member_name}' failed its checksum declared in "
                    f"{MANIFEST_MEMBER} — the archive is corrupted or was "
                    f"tampered with"
                )

    try:
        graph_document = json.loads(zf.read(GRAPH_MEMBER).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ArchiveIntegrityError(
            f"'{GRAPH_MEMBER}' is not readable JSON: {exc}"
        ) from exc

    declared_model = manifest.get("embedding_model") if manifest else None
    declared_dimension = manifest.get("embedding_dimension") if manifest else None

    if EMBEDDINGS_MEMBER not in names:
        return ExtractedArchive(
            graph_document=graph_document,
            embedding_vectors=None,
            embedding_model=declared_model,
            embedding_dimension=declared_dimension,
            compatible=False,
            regeneration_reason=(
                manifest_problem
                or f"archive has no {EMBEDDINGS_MEMBER} (graph-only export)"
            ),
            manifest=manifest,
        )

    try:
        vectors = parse_sidecar_bytes(zf.read(EMBEDDINGS_MEMBER), EMBEDDINGS_MEMBER)
    except EmbeddingSidecarError as exc:
        # A checksum-verified embeddings.bin can never land here: the loop
        # above already raised for it. Reaching here means either there was
        # no manifest to check against, or the manifest simply did not list
        # this member — either way the graph content is unaffected, so this
        # degrades to "regenerate" rather than failing the whole import.
        return ExtractedArchive(
            graph_document=graph_document,
            embedding_vectors=None,
            embedding_model=declared_model,
            embedding_dimension=declared_dimension,
            compatible=False,
            regeneration_reason=f"{EMBEDDINGS_MEMBER} could not be read: {exc}",
            manifest=manifest,
        )

    if not vectors:
        return ExtractedArchive(
            graph_document=graph_document,
            embedding_vectors=None,
            embedding_model=declared_model,
            embedding_dimension=declared_dimension,
            compatible=False,
            regeneration_reason=f"{EMBEDDINGS_MEMBER} contains no vectors",
            manifest=manifest,
        )

    if manifest is None:
        return ExtractedArchive(
            graph_document=graph_document,
            embedding_vectors=None,
            embedding_model=None,
            embedding_dimension=None,
            compatible=False,
            regeneration_reason=manifest_problem,
            manifest=None,
        )

    if declared_model != live_model_name:
        return ExtractedArchive(
            graph_document=graph_document,
            embedding_vectors=None,
            embedding_model=declared_model,
            embedding_dimension=declared_dimension,
            compatible=False,
            regeneration_reason=(
                f"archive embedding_model '{declared_model}' does not match "
                f"this instance's model '{live_model_name}'"
            ),
            manifest=manifest,
        )

    return ExtractedArchive(
        graph_document=graph_document,
        embedding_vectors=vectors,
        embedding_model=declared_model,
        embedding_dimension=declared_dimension,
        compatible=True,
        regeneration_reason=None,
        manifest=manifest,
    )
