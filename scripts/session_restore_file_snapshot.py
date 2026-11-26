"""Restore to a new directory from a coordinated offline graph snapshot."""

import ctypes
import errno
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

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
        previous = os.environ.get("SESSION_BACKUP_SESSIONS")
        os.environ["SESSION_BACKUP_SESSIONS"] = str(empty)
        try:
            with open_backup(hook) as (service, sessions, modes):

                def publish():
                    destination = root / "result"
                    shutil.copytree(sessions.directory.parent, destination)
                    _publish(destination, output)

                yield service, sessions, modes, publish
        finally:
            if previous is None:
                os.environ.pop("SESSION_BACKUP_SESSIONS", None)
            else:
                os.environ["SESSION_BACKUP_SESSIONS"] = previous
