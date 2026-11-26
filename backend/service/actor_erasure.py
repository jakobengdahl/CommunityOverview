"""Operator-only actor attribution erasure across optional local stores.

Stop writers before use, including workers and other application processes.
Stores are independent: failures are reported by store name without exception
text, and a retry completes any work left after a partial failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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
        merge_records: Optional[ActorErasureStore] = None,
    ):
        self._stores = {
            "mutation_history": mutation_history,
            "run_history": run_history,
            "proposals": proposals,
            "session_activity": session_activity,
            "staged_layers": staged_layers,
            "merge_records": merge_records,
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
        "--layers-path",
        help="Staged-session layers sidecar (<graph>.layers.json); "
        "the merge log beside it is included",
    )
    parser.add_argument(
        "--postgres-schema",
        help="PostgreSQL graph store schema whose staged-merge records to "
        "include; connects with GRAPH_POSTGRES_DSN and GRAPH_POSTGRES_SCOPE",
    )
    args = parser.parse_args(argv)
    try:
        validate_erasure(args.actor_id, args.replacement_token)
        from backend.agents.execution.sqlite_store import SqliteExecutionStore
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

    targets = []
    try:
        # Every store is asked before any is opened: the read-write SQLite
        # and session constructors write, so a refusal must come first.
        for name, path, factory, preflight in (
            (
                "mutation_history",
                args.history_path,
                GraphHistoryStore,
                GraphHistoryStore.preflight,
            ),
            (
                "run_history",
                args.run_history_db,
                SqliteExecutionStore,
                SqliteExecutionStore.preflight,
            ),
            (
                "proposals",
                args.governance_db,
                SqliteProposalStore,
                SqliteProposalStore.preflight,
            ),
            (
                "session_activity",
                args.sessions_dir,
                lambda p: SessionStore(FileSessionPersistenceBackend(p)),
                FileSessionPersistenceBackend.preflight,
            ),
            (
                "staged_layers",
                args.layers_path,
                StagedLayersFile,
                lambda p: StagedLayersFile(p).preflight(),
            ),
        ):
            # An empty path would resolve to the working directory, not a store.
            if path and preflight(path):
                targets.append((name, path, factory))
    except Exception:
        # The exception would print the path.
        print("error: a store path is not a usable store of its kind", file=sys.stderr)
        return 2

    if args.postgres_schema:
        # Checked last, so an exit 2 above leaves no connection to close.
        # The DSN comes only from the environment: argv is visible to other
        # local users and it usually carries a password.
        dsn = (os.getenv("GRAPH_POSTGRES_DSN") or "").strip()
        merge_records = None
        try:
            if not dsn:
                raise ValueError("no DSN")
            from backend.core.postgres_backend import PostgresMergeRecords

            merge_records = PostgresMergeRecords(
                dsn,
                schema=args.postgres_schema,
                scope=os.getenv("GRAPH_POSTGRES_SCOPE"),
            )
            present = merge_records.preflight()
        except Exception:
            # Connection errors can quote the DSN, password included.
            if merge_records is not None:
                merge_records.close()
            print(
                "error: the PostgreSQL graph store is unavailable or not writable",
                file=sys.stderr,
            )
            return 2
        if present:
            targets.append(("merge_records", None, lambda _: merge_records))
        else:
            merge_records.close()

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
