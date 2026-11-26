"""Restore to a new directory from a coordinated offline graph snapshot."""

import ctypes
import errno
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from backend.core.template_store import validate_template
from backend.runtime.config_context import resolve_schema_config_path_info
from scripts.session_backup_file_snapshot import open_backup


def _publish(source, target):
    # Linux renameat2 makes the existence check and publication indivisible.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        error = ctypes.get_errno()
        raise OSError(
            error, os.strerror(error) if error != errno.EEXIST else "target exists"
        )


def profile_template_ids():
    profile_dir = (
        Path(
            resolve_schema_config_path_info("config/default/schema_config.json")["path"]
        ).parent
        / "templates"
    )
    ids = set()
    for path in sorted(profile_dir.glob("*.json")):
        doc = validate_template(json.loads(path.read_text(encoding="utf-8")))
        if doc["template_id"] in ids:
            raise ValueError("duplicate profile template")
        ids.add(doc["template_id"])
    return ids


@contextmanager
def open_restore(hook, output):
    output = Path(output).absolute()
    # open_backup copies and verifies the graph and journal without touching the
    # source. The temporary empty session directory cannot contain live data.
    with tempfile.TemporaryDirectory(
        prefix="session-restore-", dir=output.parent
    ) as temp:
        root = Path(temp)
        empty = root / "empty"
        empty.mkdir()
        templates = root / "empty-templates"
        templates.mkdir()
        previous_templates = os.environ.get("SESSION_BACKUP_TEMPLATES")
        os.environ["SESSION_BACKUP_TEMPLATES"] = str(templates)
        previous = os.environ.get("SESSION_BACKUP_SESSIONS")
        os.environ["SESSION_BACKUP_SESSIONS"] = str(empty)
        try:
            with open_backup(hook) as (service, sessions, modes):
                service.backup_profile_template_ids = profile_template_ids

                def publish():
                    destination = root / "result"
                    shutil.copytree(sessions.directory.parent, destination)
                    _publish(destination, output)

                yield service, sessions, modes, publish
        finally:
            if previous_templates is None:
                os.environ.pop("SESSION_BACKUP_TEMPLATES", None)
            else:
                os.environ["SESSION_BACKUP_TEMPLATES"] = previous_templates
            if previous is None:
                os.environ.pop("SESSION_BACKUP_SESSIONS", None)
            else:
                os.environ["SESSION_BACKUP_SESSIONS"] = previous
