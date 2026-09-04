"""
Binary sidecar for node embedding vectors.

Vectors used to be ordinary ``Node`` fields, so every graph mutation serialised
all of them into ``graph.json`` as JSON text — measured at 8.9 MB of a 12.2 MB
graph, a 7.5x inflation over the same vectors as float32. They live in their own
file instead, written only when a vector actually changes.

File layout, little-endian throughout:

    magic    7 bytes   b"CKGEMB\\x01"
    hdr_len  4 bytes   uint32, byte length of the JSON header
    header   hdr_len   {"dtype": "float32", "rows": N, "dim": D, "ids": [...]}
    payload  N*D*4     float32 matrix, row i belonging to ids[i]

The sidecar is derived data: it can always be regenerated from node text by the
embedding model, so a missing or unreadable one degrades semantic search rather
than failing a load.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any, Dict, List

MAGIC = b"CKGEMB\x01"
_HEADER_LEN_STRUCT = struct.Struct("<I")
# A header is ids plus three small scalars. The bound is redundant with the
# "does the header fit in the file" check below, but states the intent: a
# plausible header is small, and nothing is sized from the length field alone.
MAX_HEADER_BYTES = 64 * 1024 * 1024


class EmbeddingSidecarError(Exception):
    """Raised when a sidecar file cannot be read as a valid vector index."""


def _ensure_numpy():
    import numpy as np

    return np


def resolve_sidecar_path(
    graph_path: str | Path, embeddings_file: str | None = None
) -> Path | None:
    """Where the sidecar for this graph lives, honouring EMBEDDINGS_FILE.

    Returns None to mean "let GraphStorage derive it from the graph path".
    The maintenance scripts call this so they act on the same file the running
    app reads; without it a deployment that sets EMBEDDINGS_FILE would have
    them write to a sidecar nothing ever loads.
    """
    value = (
        embeddings_file if embeddings_file is not None else os.getenv("EMBEDDINGS_FILE")
    )
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(graph_path).parent / path


class FileEmbeddingSidecar:
    """Reads and writes the embedding matrix for a file-backed graph."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Dict[str, Any]:
        """Return ``{node_id: float32 vector}``.

        Raises:
            EmbeddingSidecarError: the file is not a readable sidecar. Callers
                treat that as "no vectors" — never as a fatal load error.
        """
        # The checks in _read below name what is wrong, which is what an
        # operator needs. This wrapper is what makes the promise above true
        # whatever they miss: three separate malformed shapes have each reached
        # a caller as some other exception type — a truncated payload as
        # ValueError from reshape, a bool row count as TypeError, a deeply
        # nested header as RecursionError out of the JSON parser — and each
        # took down a whole graph load, because the callers catch this class
        # and nothing else. Enumerating the ways a parser can fail is a losing
        # game; converting them is not.
        try:
            return self._read()
        except EmbeddingSidecarError:
            raise
        except Exception as exc:
            raise EmbeddingSidecarError(
                f"{self.path} could not be read as a sidecar: {exc!r}"
            ) from exc

    def _read(self) -> Dict[str, Any]:
        np = _ensure_numpy()

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise EmbeddingSidecarError(f"cannot read {self.path}: {exc}") from exc

        prefix = len(MAGIC) + _HEADER_LEN_STRUCT.size
        if len(raw) < prefix or raw[: len(MAGIC)] != MAGIC:
            raise EmbeddingSidecarError(f"{self.path} is not an embedding sidecar")

        (header_len,) = _HEADER_LEN_STRUCT.unpack_from(raw, len(MAGIC))
        if header_len > MAX_HEADER_BYTES or prefix + header_len > len(raw):
            raise EmbeddingSidecarError(f"{self.path} has a truncated header")

        try:
            header = json.loads(raw[prefix : prefix + header_len].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EmbeddingSidecarError(
                f"{self.path} has an unreadable header: {exc}"
            ) from exc

        if not isinstance(header, dict):
            raise EmbeddingSidecarError(f"{self.path} header is not an object")

        ids = header.get("ids")
        rows = header.get("rows")
        dim = header.get("dim")
        # bool is a subclass of int, and True == 1, so a header carrying
        # `true` for rows and dim satisfies every check below — including the
        # exact payload length, since True * True * 4 is 4. It then reaches
        # reshape(), which raises TypeError rather than anything this module
        # declares, and a caller that only catches EmbeddingSidecarError loses
        # the whole graph load to a damaged derived file.
        if (
            not isinstance(ids, list)
            or isinstance(rows, bool)
            or isinstance(dim, bool)
            or not isinstance(rows, int)
            or not isinstance(dim, int)
            or rows < 0
            or dim < 0
            or len(ids) != rows
        ):
            raise EmbeddingSidecarError(f"{self.path} has an inconsistent header")
        if not all(isinstance(node_id, str) for node_id in ids):
            raise EmbeddingSidecarError(f"{self.path} has a non-string node id")
        if header.get("dtype") != "float32":
            raise EmbeddingSidecarError(
                f"{self.path} has unsupported dtype {header.get('dtype')!r}"
            )

        payload = raw[prefix + header_len :]
        expected = rows * dim * 4
        if len(payload) != expected:
            raise EmbeddingSidecarError(
                f"{self.path} payload is {len(payload)} bytes, expected {expected}"
            )

        if rows == 0 or dim == 0:
            return {}

        matrix = np.frombuffer(payload, dtype="<f4").reshape(rows, dim)
        # frombuffer views the bytes object, so rows would stay read-only and
        # pinned to the whole file. Copy each row out instead.
        return {node_id: matrix[i].astype(np.float32) for i, node_id in enumerate(ids)}

    def save(self, vectors: Dict[str, Any]) -> None:
        """Write ``{node_id: vector}`` atomically.

        Raises:
            EmbeddingSidecarError: the vectors are not a uniform 2-D matrix.
        """
        np = _ensure_numpy()

        ids: List[str] = list(vectors.keys())
        if ids:
            rows_list = [np.asarray(vectors[i], dtype=np.float32).ravel() for i in ids]
            dims = {row.shape[0] for row in rows_list}
            if len(dims) > 1:
                raise EmbeddingSidecarError(
                    f"embeddings have mixed dimensions {sorted(dims)}"
                )
            matrix = np.vstack(rows_list).astype("<f4", copy=False)
            dim = int(matrix.shape[1])
        else:
            matrix = None
            dim = 0

        header = json.dumps(
            {"dtype": "float32", "rows": len(ids), "dim": dim, "ids": ids},
            ensure_ascii=False,
        ).encode("utf-8")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".bin", prefix="embeddings_", dir=self.path.parent
        )
        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(MAGIC)
                f.write(_HEADER_LEN_STRUCT.pack(len(header)))
                f.write(header)
                if matrix is not None:
                    f.write(matrix.tobytes(order="C"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
