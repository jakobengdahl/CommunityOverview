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
from contextlib import ExitStack
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
    ):
        self._stores = {
            "mutation_history": mutation_history,
            "run_history": run_history,
            "proposals": proposals,
            "session_activity": session_activity,
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
    parser.add_argument("--run-history-db", default=os.getenv("AGENTS_RUN_HISTORY_DB"))
    parser.add_argument("--governance-db", default=os.getenv("AGENTS_GOVERNANCE_DB"))
    parser.add_argument(
        "--sessions-dir", help="Directory containing session JSON files"
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

        stores = {}
        failed = []
        with ExitStack() as stack:
            for name, path, factory in (
                ("mutation_history", args.history_path, GraphHistoryStore),
                ("run_history", args.run_history_db, SqliteExecutionStore),
                ("proposals", args.governance_db, SqliteProposalStore),
                (
                    "session_activity",
                    args.sessions_dir,
                    lambda p: SessionStore(FileSessionPersistenceBackend(p)),
                ),
            ):
                if path is None:
                    continue
                try:
                    if not Path(path).exists():
                        continue
                    if name == "session_activity":
                        if not Path(path).is_dir():
                            raise ValueError("Expected a session directory")
                    elif not Path(path).is_file():
                        raise ValueError("Expected a store file")
                    store = factory(path)
                    if hasattr(store, "close"):
                        stack.callback(store.close)
                    stores[name] = store
                except Exception:
                    failed.append(name)
            report = ActorErasureService(**stores).erase_actor(
                args.actor_id, args.replacement_token
            )
            for name in failed:
                report["counts"][name] = None
                report["failed_stores"].append(name)
        print(json.dumps(report))
        return 1 if report["failed_stores"] else 0
    except Exception:
        print("error: actor attribution erasure failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
