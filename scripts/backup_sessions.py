#!/usr/bin/env python3
"""Run a deployment-provided, read-only session backup integration."""

import argparse
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.runtime.authorization import GRAPH_ACTION_ADMIN  # noqa: E402
from backend.service.session_backup import (  # noqa: E402
    BackupRefused,
    _authorize,
    canonical_bytes,
    create_backup,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--integration", required=True, help="trusted deployment integration module"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    integration = importlib.import_module(args.integration)
    hook = integration.authorization_hook()
    _authorize(hook, target="backup_sessions", action=GRAPH_ACTION_ADMIN)
    # The integration must open read-only stores and provide a layer snapshot
    # coordinated with every writer. Never construct a default policy or load
    # graph storage here: graph loading can perform merge recovery.
    with integration.open_backup(hook) as (service, sessions, modes):
        if service.authorization_hook is not hook:
            raise BackupRefused("invalid authorization integration")
        document = create_backup(
            service, sessions, modes, template_backend=service.backup_template_backend
        )
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
