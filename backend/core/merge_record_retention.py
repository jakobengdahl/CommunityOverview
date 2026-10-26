"""Merge-record retention: purge the records of long-deleted staged sessions.

docs/SESSION_OVERLAY_CONTRACT.md §11.5. A merge record is kept while its
session exists and for the retention period after the session is deleted
(its staged-session record retired), then removed. The layer store decides
which records qualify (`purge_merge_records`); this module only runs it: once
at start, then every interval, on a daemon thread, in bounded batches, so it
never runs inside a merge unit and never holds the store for long.

A run that fails is logged and the loop carries on, so retention can never
take the server down; the records it did not reach are purged by a later run.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# One batch is one unit of the store; a run purges at most
# PURGE_BATCH_SIZE * PURGE_MAX_BATCHES records, and a backlog beyond that is
# left to the next run.
PURGE_BATCH_SIZE = 500
PURGE_MAX_BATCHES = 20
PURGE_INTERVAL_SECONDS = 6 * 3600


def purge_expired_merge_records(
    store: Any,
    retention: timedelta,
    *,
    batch_size: int = PURGE_BATCH_SIZE,
    max_batches: int = PURGE_MAX_BATCHES,
) -> int:
    """Purge in batches until one comes back short or the bound is reached;
    return how many records went."""
    purged = 0
    for _ in range(max_batches):
        count = store.purge_merge_records(retention, limit=batch_size)
        purged += count
        if count < batch_size:
            break
    return purged


class MergeRecordRetention:
    """Runs `purge_expired_merge_records` on a daemon thread."""

    def __init__(
        self,
        store: Any,
        retention: timedelta,
        *,
        interval_seconds: float = PURGE_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._retention = retention
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> Optional[int]:
        """Purge once; the count, or None if the run raised."""
        try:
            purged = purge_expired_merge_records(self._store, self._retention)
        except Exception:
            logger.exception("Merge-record retention purge failed")
            return None
        if purged:
            logger.info(
                "merge-record retention purged %d records of sessions deleted "
                "more than %d days ago",
                purged,
                self._retention.days,
            )
        return purged

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="merge-record-retention", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self._interval)
