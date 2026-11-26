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
from backend.service.session_restore import RestoreRefused, restore_backup  # noqa: E402


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
        with args.input.open(encoding="utf-8") as source:
            document = json.load(source)
        with integration.open_restore(hook, args.output) as (
            service,
            sessions,
            modes,
            publish,
        ):
            if service.authorization_hook is not hook:
                raise RestoreRefused("access denied")
            restoring = True
            report = restore_backup(service, sessions, modes, document)
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
