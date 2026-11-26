#!/usr/bin/env python3
"""Back up standalone file sessions while all graph and session writers are stopped."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.session_store import FileSessionPersistenceBackend  # noqa: E402
from backend.core.storage import GraphStorage  # noqa: E402
from backend.core.storage_backends import FileGraphPersistenceBackend  # noqa: E402
from backend.core.write_modes import WriteModes  # noqa: E402
from backend.service import GraphService  # noqa: E402
from backend.service.session_backup import canonical_bytes, create_backup  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--writers-stopped", action="store_true", required=True)
    args = parser.parse_args()
    if not args.graph.is_file() or not args.sessions.is_dir():
        parser.error("graph file and session directory must exist")
    graph_backend = FileGraphPersistenceBackend(args.graph, staged_sessions=True)
    storage = GraphStorage(persistence_backend=graph_backend)
    document = create_backup(
        GraphService(storage),
        FileSessionPersistenceBackend(args.sessions),
        WriteModes(graph_backend),
    )
    # Exclusive creation prevents replacing an earlier backup. Restrict the
    # artifact before writing any session data, independent of the user's umask.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(canonical_bytes(document) + b"\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        args.output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
