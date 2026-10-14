"""Construct the node attachment store the configuration asks for (ADR 0008).

Every setting is validated here, at boot, with the variable named in the
message. A limit of 0 or less is refused rather than read as "unlimited", and
an allowlist entry the detector does not know is refused rather than ignored.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from backend.core.attachment_store import (
    DEFAULT_MAX_BYTES_PER_NODE,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES_PER_NODE,
    AttachmentLimits,
    FileAttachmentStore,
)
from backend.core.attachment_types import parse_allowed_types
from backend.service.attachments import AttachmentSettings

from .config import AppConfig
from .persistence import POSTGRES_BACKEND

if TYPE_CHECKING:
    from backend.core import GraphStorage

logger = logging.getLogger(__name__)

FILE_ATTACHMENT_BACKEND = "file"
SUPPORTED_ATTACHMENT_BACKENDS = (FILE_ATTACHMENT_BACKEND,)


class AttachmentConfigurationError(RuntimeError):
    """An attachment setting cannot be honoured; the message says which."""


def _positive_int(raw: Optional[str], variable: str, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise AttachmentConfigurationError(
            f"{variable}={raw!r} is not a whole number."
        ) from None
    if value <= 0:
        raise AttachmentConfigurationError(
            f"{variable}={value} is not usable: it must be at least 1. There is "
            "no 'unlimited' value."
        )
    return value


def build_attachment_settings(
    config: AppConfig, graph_storage: "GraphStorage"
) -> Optional[AttachmentSettings]:
    """The attachment settings ``config`` selects, or None when unavailable.

    None only for the PostgreSQL graph backend without ``ATTACHMENT_DIR``:
    there is no graph file to sit beside, and inventing a per-instance local
    directory would silently give each instance a different set of files. The
    attachment routes then answer ``503 attachments_unavailable`` while the
    rest of the instance runs as before.
    """
    backend = (config.attachment_backend or "").strip().lower() or (
        FILE_ATTACHMENT_BACKEND
    )
    if backend not in SUPPORTED_ATTACHMENT_BACKENDS:
        raise AttachmentConfigurationError(
            f"ATTACHMENT_BACKEND={backend!r} is not a backend this build knows. "
            f"Supported: {', '.join(SUPPORTED_ATTACHMENT_BACKENDS)}."
        )

    limits = AttachmentLimits(
        max_file_bytes=_positive_int(
            config.attachment_max_file_bytes,
            "ATTACHMENT_MAX_FILE_BYTES",
            DEFAULT_MAX_FILE_BYTES,
        ),
        max_files_per_node=_positive_int(
            config.attachment_max_files_per_node,
            "ATTACHMENT_MAX_FILES_PER_NODE",
            DEFAULT_MAX_FILES_PER_NODE,
        ),
        max_bytes_per_node=_positive_int(
            config.attachment_max_bytes_per_node,
            "ATTACHMENT_MAX_BYTES_PER_NODE",
            DEFAULT_MAX_BYTES_PER_NODE,
        ),
    )
    try:
        allowed_types = parse_allowed_types(config.attachment_allowed_types)
    except ValueError as exc:
        raise AttachmentConfigurationError(str(exc)) from None

    attachment_dir = (config.attachment_dir or "").strip()
    namespace = (config.attachment_namespace or "").strip()
    if config.graph_backend == POSTGRES_BACKEND:
        if not attachment_dir:
            logger.warning(
                "Node attachments are unavailable: GRAPH_BACKEND=postgres needs "
                "ATTACHMENT_DIR, a root shared by every instance serving this "
                "schema."
            )
            return None
        root = Path(attachment_dir)
        namespace = namespace or f"pg:{config.graph_postgres_schema}"
    else:
        graph_path = Path(graph_storage.json_path)
        root = (
            Path(attachment_dir)
            if attachment_dir
            else graph_path.parent / f"{graph_path.stem}.attachments"
        )
        namespace = namespace or graph_path.stem

    store = FileAttachmentStore(root)
    try:
        store.sweep_stale_staged()
    except OSError:
        logger.exception("Sweeping stale staged attachment uploads failed")
    return AttachmentSettings(
        store=store,
        namespace=namespace,
        limits=limits,
        allowed_types=allowed_types,
    )
