#!/usr/bin/env python3
"""Restore a backup into a new offline workspace using a trusted integration."""

import argparse
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.runtime.authorization import GRAPH_ACTION_ADMIN  # noqa: E402
from backend.service.session_backup import _authorize  # noqa: E402
from backend.service.session_restore import (  # noqa: E402
    MAX_BACKUP_BYTES,
    RestoreRefused,
    restore_backup,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    authorized = False
    restoring = False
    try:
        integration = importlib.import_module(args.integration)
        hook = integration.authorization_hook()
        _authorize(hook, target="restore_sessions", action=GRAPH_ACTION_ADMIN)
        with args.input.open("rb") as source:
            if source.seek(0, 2) > MAX_BACKUP_BYTES:
                raise RestoreRefused("input limit exceeded")
            source.seek(0)
            data = source.read(MAX_BACKUP_BYTES + 1)
            if len(data) > MAX_BACKUP_BYTES:
                raise RestoreRefused("input limit exceeded")
            document = json.loads(data)
        with integration.open_restore(hook, args.output) as (
            service,
            sessions,
            modes,
            publish,
        ):
            if service.authorization_hook is not hook:
                raise RestoreRefused("access denied")
            restoring = True
            template_options = (
                {
                    "template_backend": service.backup_template_backend,
                    "profile_template_ids": service.backup_profile_template_ids,
                }
                if document.get("format_version") == 2
                else {}
            )
            report = restore_backup(
                service,
                sessions,
                modes,
                document,
                **template_options,
            )
            authorized = True
            publish()
    except RestoreRefused:
        if restoring:
            raise
        raise RestoreRefused("access denied") from None
    except Exception:
        raise RestoreRefused(
            "restore failed" if authorized else "access denied"
        ) from None
    print(json.dumps(report))


if __name__ == "__main__":
    main()
