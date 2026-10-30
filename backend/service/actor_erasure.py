"""Operator-only actor attribution erasure across optional local stores.

Stop writers before use, including workers and other application processes.
Stores are independent: failures are reported by store name without exception
text, and a retry completes any work left after a partial failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional, Protocol

from backend.core.actor_attribution import validate_erasure


class ActorErasureStore(Protocol):
    def erase_actor(self, actor_id: str, replacement_token: str) -> int: ...


class ActorErasureService:
    """General actor-attribution maintenance; never mutates the graph."""

    def __init__(
        self,
        *,
        mutation_history: Optional[ActorErasureStore] = None,
        run_history: Optional[ActorErasureStore] = None,
        proposals: Optional[ActorErasureStore] = None,
        session_activity: Optional[ActorErasureStore] = None,
        staged_layers: Optional[ActorErasureStore] = None,
    ):
        self._stores = {
            "mutation_history": mutation_history,
            "run_history": run_history,
            "proposals": proposals,
            "session_activity": session_activity,
            "staged_layers": staged_layers,
        }

    def erase_actor(self, actor_id: str, replacement_token: str) -> dict:
        """Return record counts and failed store names, containing no inputs."""
        validate_erasure(actor_id, replacement_token)
        counts = {}
        failed = []
        for name, store in self._stores.items():
            try:
                counts[name] = (
                    store.erase_actor(actor_id, replacement_token)
                    if store is not None
                    else 0
                )
            except Exception:
                # Neither exception text nor a traceback is safe to log: a
                # failed decoder or adapter can include entire source records.
                counts[name] = None
                failed.append(name)
        return {"counts": counts, "failed_stores": failed}


def _sqlite_schema_matches(path: str, table: str, version: int) -> bool:
    """Check that *path* is a current-schema store owning *table*.

    Nothing next to *path* is created or changed. immutable=1 keeps SQLite
    from creating -shm/-wal sidecars, but it also ignores a pending WAL,
    which may hold the schema; such a store is probed as a private copy.
    """
    with tempfile.TemporaryDirectory() as scratch:
        if Path(f"{path}-wal").exists():
            probe = Path(scratch) / "probe.db"
            shutil.copyfile(path, probe)
            shutil.copyfile(f"{path}-wal", f"{probe}-wal")
            # The copy is private, so SQLite may recover the WAL into it.
            uri = probe.as_uri()
        else:
            uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
        try:
            conn = sqlite3.connect(uri, uri=True)
            try:
                found = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                current = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return False
    return found is not None and current == version


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid arguments, including actor inputs.
        self.exit(2, "error: invalid arguments; use --help for usage\n")


def main(argv: Optional[list[str]] = None) -> int:
    parser = _Parser(
        description="Replace actor attribution in local stores. Stop writers first."
    )
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--replacement-token", required=True)
    parser.add_argument("--history-path", help="Mutation-history NDJSON sidecar")
    # Empty env values mean unset, as in AgentsConfig.from_env.
    parser.add_argument(
        "--run-history-db", default=os.getenv("AGENTS_RUN_HISTORY_DB") or None
    )
    parser.add_argument(
        "--governance-db", default=os.getenv("AGENTS_GOVERNANCE_DB") or None
    )
    parser.add_argument(
        "--sessions-dir", help="Directory containing session JSON files"
    )
    parser.add_argument(
        "--layers-path", help="Staged-session layers sidecar (<graph>.layers.json)"
    )
    args = parser.parse_args(argv)
    try:
        validate_erasure(args.actor_id, args.replacement_token)
        from backend.agents.execution.sqlite_store import (
            _SCHEMA_VERSION as execution_schema_version,
        )
        from backend.agents.execution.sqlite_store import SqliteExecutionStore
        from backend.agents.governance.store import (
            _SCHEMA_VERSION as governance_schema_version,
        )
        from backend.agents.governance.store import SqliteProposalStore
        from backend.core.history_store import GraphHistoryStore
        from backend.core.session_store import (
            FileSessionPersistenceBackend,
            SessionStore,
        )
        from backend.core.storage_backends import StagedLayersFile
    except Exception:
        print("error: actor attribution erasure failed", file=sys.stderr)
        return 2

    sqlite_schemas = {
        "run_history": ("execution_jobs", execution_schema_version),
        "proposals": ("proposals", governance_schema_version),
    }
    targets = []
    for name, path, factory in (
        ("mutation_history", args.history_path, GraphHistoryStore),
        ("run_history", args.run_history_db, SqliteExecutionStore),
        ("proposals", args.governance_db, SqliteProposalStore),
        (
            "session_activity",
            args.sessions_dir,
            lambda p: SessionStore(FileSessionPersistenceBackend(p)),
        ),
        ("staged_layers", args.layers_path, StagedLayersFile),
    ):
        # An empty path would resolve to the working directory, not a store.
        if not path:
            continue
        try:
            if not Path(path).exists():
                continue
            is_dir = name == "session_activity"
            valid = Path(path).is_dir() if is_dir else Path(path).is_file()
            if valid and name in sqlite_schemas:
                valid = _sqlite_schema_matches(path, *sqlite_schemas[name])
        except OSError:
            # An unreadable path is refused like a mis-pointed one; the
            # exception would print the path.
            valid = False
        if not valid:
            # A mis-pointed path is an invocation error: refuse before any
            # store is opened, because the read-write SQLite constructors
            # create and migrate schema in whatever file they are given.
            print("error: a store path is not a store of its kind", file=sys.stderr)
            return 2
        targets.append((name, path, factory))

    # From here on stores may be modified, so every failure is reported per
    # store with exit 1; exit 2 is reserved for runs that touched nothing.
    stores = {}
    failed = []
    try:
        for name, path, factory in targets:
            try:
                stores[name] = factory(path)
            except Exception:
                failed.append(name)
        report = ActorErasureService(**stores).erase_actor(
            args.actor_id, args.replacement_token
        )
    finally:
        for name, store in stores.items():
            try:
                if hasattr(store, "close"):
                    store.close()
            except Exception:
                failed.append(name)
    for name in failed:
        report["counts"][name] = None
        if name not in report["failed_stores"]:
            report["failed_stores"].append(name)
    print(json.dumps(report))
    return 1 if report["failed_stores"] else 0


if __name__ == "__main__":
    sys.exit(main())
