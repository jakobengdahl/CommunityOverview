"""
Retention, redaction and purge for durable AgentRun history.

An operator bounds how long run history is kept with four optional settings:

* ``AGENTS_RUN_HISTORY_RETENTION_DAYS`` — terminal runs older than this are
  purged (deleted). Never below ``RETENTION_FLOOR_DAYS``; a smaller value is
  raised to the floor.
* ``AGENTS_RUN_HISTORY_REDACT_AFTER_DAYS`` — terminal runs older than this have
  their content stripped (see ``execution.models.redact_job``). Clamped to the
  retention, so a run is always redacted no later than it is purged.
* ``AGENTS_RUN_HISTORY_EXPORT_MAX_DAYS`` — the read window of
  ``GET /agents/runs``. Clamped to the retention; defaults to it.
* ``AGENTS_RUN_HISTORY_SWEEP_INTERVAL_HOURS`` — how often the sweep runs
  (default 24).

All unset means history is kept forever and nothing is ever swept. A value that
is not an integer from 1 to ``MAX_DAYS`` (``MAX_SWEEP_INTERVAL_HOURS`` for the
interval) is a configuration error raised at startup.

The sweep redacts and purges only terminal runs. A run still ``RUNNING`` whose
last update is older than the retention (one its process crashed out of, so
nothing will ever finalize it) is first abandoned — dead-lettered with a fixed,
content-free reason — and then redacted and purged like any other terminal run
in the same sweep. A run younger than the retention, or any run while retention
is unset, is never abandoned. It runs once when the agent registry starts and
then every interval; a failed sweep is logged and never breaks the agent
runtime. Each sweep writes one audit record carrying only the policy, cutoffs
and counts — never a run's id or content.

The same windows apply to agent governance proposals when the sweep is given a
proposal store: decided proposals older than the redact window have their
``input_args``, ``apply_result`` and ``apply_error`` stripped, and decided
proposals older than the retention are purged, both aged by when they were
decided. A pending proposal is never touched, whatever its age.

Run a sweep by hand with::

    python -m backend.agents.run_history_retention [--dry-run]

It sweeps ``AGENTS_RUN_HISTORY_DB`` and ``AGENTS_GOVERNANCE_DB``, whichever are
set.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Mapping, Optional

from .execution import ExecutionStore, utcnow
from .governance.store import ProposalStore

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger(f"{__name__}.audit")

RETENTION_FLOOR_DAYS = 7
DEFAULT_SWEEP_INTERVAL_HOURS = 24
# Upper bounds (100 years / 10 years) keep date arithmetic and thread waits in
# range; leave a window unset rather than very large to keep history forever.
MAX_DAYS = 36_500
MAX_SWEEP_INTERVAL_HOURS = 87_600

ENV_RETENTION_DAYS = "AGENTS_RUN_HISTORY_RETENTION_DAYS"
ENV_REDACT_AFTER_DAYS = "AGENTS_RUN_HISTORY_REDACT_AFTER_DAYS"
ENV_EXPORT_MAX_DAYS = "AGENTS_RUN_HISTORY_EXPORT_MAX_DAYS"
ENV_SWEEP_INTERVAL_HOURS = "AGENTS_RUN_HISTORY_SWEEP_INTERVAL_HOURS"


def _positive_int(name: str, raw: Optional[str]) -> Optional[int]:
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from None


@dataclass(frozen=True)
class RetentionPolicy:
    """Effective (already clamped) run-history retention windows, in days."""

    retention_days: Optional[int] = None
    redact_after_days: Optional[int] = None
    export_max_days: Optional[int] = None
    sweep_interval_hours: int = DEFAULT_SWEEP_INTERVAL_HOURS

    @classmethod
    def from_values(
        cls,
        *,
        retention_days: Optional[int] = None,
        redact_after_days: Optional[int] = None,
        export_max_days: Optional[int] = None,
        sweep_interval_hours: Optional[int] = None,
    ) -> "RetentionPolicy":
        """Apply the floor and clamps to configured values."""
        for name, value, ceiling in (
            (ENV_RETENTION_DAYS, retention_days, MAX_DAYS),
            (ENV_REDACT_AFTER_DAYS, redact_after_days, MAX_DAYS),
            (ENV_EXPORT_MAX_DAYS, export_max_days, MAX_DAYS),
            (ENV_SWEEP_INTERVAL_HOURS, sweep_interval_hours, MAX_SWEEP_INTERVAL_HOURS),
        ):
            if value is not None and not 1 <= value <= ceiling:
                raise ValueError(
                    f"{name} must be an integer from 1 to {ceiling}, got {value!r}"
                )

        if retention_days is not None and retention_days < RETENTION_FLOOR_DAYS:
            logger.warning(
                "Run-history retention of %d day(s) is below the %d-day floor; "
                "using %d",
                retention_days,
                RETENTION_FLOOR_DAYS,
                RETENTION_FLOOR_DAYS,
            )
            retention_days = RETENTION_FLOOR_DAYS
        if retention_days is not None:
            if redact_after_days is not None and redact_after_days > retention_days:
                redact_after_days = retention_days
            if export_max_days is not None and export_max_days > retention_days:
                export_max_days = retention_days
        return cls(
            retention_days=retention_days,
            redact_after_days=redact_after_days,
            export_max_days=export_max_days,
            sweep_interval_hours=sweep_interval_hours or DEFAULT_SWEEP_INTERVAL_HOURS,
        )

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "RetentionPolicy":
        """Read the policy from the environment; raise ValueError if invalid."""
        env = os.environ if environ is None else environ
        return cls.from_values(
            retention_days=_positive_int(
                ENV_RETENTION_DAYS, env.get(ENV_RETENTION_DAYS)
            ),
            redact_after_days=_positive_int(
                ENV_REDACT_AFTER_DAYS, env.get(ENV_REDACT_AFTER_DAYS)
            ),
            export_max_days=_positive_int(
                ENV_EXPORT_MAX_DAYS, env.get(ENV_EXPORT_MAX_DAYS)
            ),
            sweep_interval_hours=_positive_int(
                ENV_SWEEP_INTERVAL_HOURS, env.get(ENV_SWEEP_INTERVAL_HOURS)
            ),
        )

    @property
    def sweeps(self) -> bool:
        """Whether the policy ever redacts or purges anything."""
        return self.retention_days is not None or self.redact_after_days is not None

    @property
    def read_window_days(self) -> Optional[int]:
        """How far back the read API reaches; None means unbounded."""
        if self.export_max_days is not None:
            return self.export_max_days
        return self.retention_days

    def read_cutoff(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """Runs created at or before this instant are outside the read window."""
        days = self.read_window_days
        if days is None:
            return None
        return (now or utcnow()) - timedelta(days=days)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "retention_days": self.retention_days,
            "redact_after_days": self.redact_after_days,
            "export_max_days": self.read_window_days,
            "sweep_interval_hours": self.sweep_interval_hours,
            "retention_floor_days": RETENTION_FLOOR_DAYS,
            "sweeps": self.sweeps,
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat().replace("+00:00", "Z") if dt is not None else None


@dataclass(frozen=True)
class SweepReport:
    """The content-free audit record of one sweep: policy, cutoffs and counts."""

    swept_at: datetime
    dry_run: bool
    retention_days: Optional[int]
    redact_after_days: Optional[int]
    redact_cutoff: Optional[datetime]
    purge_cutoff: Optional[datetime]
    abandoned: int
    redacted: int
    purged: int
    proposals_redacted: int = 0
    proposals_purged: int = 0
    proposals_failed: bool = False
    run_history_failed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "swept_at": _iso(self.swept_at),
            "dry_run": self.dry_run,
            "retention_days": self.retention_days,
            "redact_after_days": self.redact_after_days,
            "redact_cutoff": _iso(self.redact_cutoff),
            "purge_cutoff": _iso(self.purge_cutoff),
            "abandoned": self.abandoned,
            "redacted": self.redacted,
            "purged": self.purged,
            "proposals_redacted": self.proposals_redacted,
            "proposals_purged": self.proposals_purged,
            "proposals_failed": self.proposals_failed,
            "run_history_failed": self.run_history_failed,
        }


def sweep(
    store: Optional[ExecutionStore],
    policy: RetentionPolicy,
    *,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    proposal_store: Optional[ProposalStore] = None,
) -> SweepReport:
    """
    Abandon runs left running past the retention, then redact, then purge the
    terminal runs the policy has aged out; then redact and purge the decided
    governance proposals in ``proposal_store`` on the same cutoffs. Either
    store may be None to skip it.

    Idempotent: a second sweep at the same ``now`` changes nothing. With
    ``dry_run`` the counts are what a real sweep would change, and the store is
    left untouched. A run-store error is logged and reported as
    ``run_history_failed``, a proposal-store error as ``proposals_failed``; each
    phase runs whatever the other did.
    """
    now = now or utcnow()
    redact_cutoff = (
        now - timedelta(days=policy.redact_after_days)
        if policy.redact_after_days is not None
        else None
    )
    purge_cutoff = (
        now - timedelta(days=policy.retention_days)
        if policy.retention_days is not None
        else None
    )

    # Each phase is contained on its own: a failing store is logged and flagged
    # in the report, and neither costs the other phase its work or the sweep its
    # audit record. Counts of steps that finished before a failure stay in.
    abandoned = redacted = purged = 0
    run_history_failed = False
    try:
        if store is not None and purge_cutoff is not None:
            ids = [job.id for job in store.list_running_older_than(purge_cutoff)]
            abandoned = (
                len(ids)
                if dry_run
                else store.abandon(ids, cutoff=purge_cutoff, now=now)
            )
        # An abandoned run is unredacted and finished before the purge cutoff, which
        # is never later than the redact cutoff, so a real sweep redacts and purges
        # it below; a dry run, which abandons nothing, counts it in by hand.
        if store is not None and redact_cutoff is not None:
            ids = [
                job.id
                for job in store.list_terminal_older_than(redact_cutoff, redacted=False)
            ]
            redacted = len(ids) if dry_run else store.redact(ids, now=now)
            if dry_run:
                redacted += abandoned
        if store is not None and purge_cutoff is not None:
            ids = [job.id for job in store.list_terminal_older_than(purge_cutoff)]
            purged = len(ids) if dry_run else store.purge(ids)
            if dry_run:
                purged += abandoned
    except Exception:
        logger.exception("Run-history retention sweep failed")
        run_history_failed = True

    proposals_redacted = proposals_purged = 0
    proposals_failed = False
    try:
        if proposal_store is not None and redact_cutoff is not None:
            ids = [
                p.id
                for p in proposal_store.list_decided_older_than(
                    redact_cutoff, redacted=False
                )
            ]
            proposals_redacted = len(ids) if dry_run else proposal_store.redact(ids)
        if proposal_store is not None and purge_cutoff is not None:
            ids = [p.id for p in proposal_store.list_decided_older_than(purge_cutoff)]
            proposals_purged = len(ids) if dry_run else proposal_store.purge(ids)
    except Exception:
        logger.exception("Governance-proposal retention sweep failed")
        proposals_failed = True

    report = SweepReport(
        swept_at=now,
        dry_run=dry_run,
        retention_days=policy.retention_days,
        redact_after_days=policy.redact_after_days,
        redact_cutoff=redact_cutoff,
        purge_cutoff=purge_cutoff,
        abandoned=abandoned,
        redacted=redacted,
        purged=purged,
        proposals_redacted=proposals_redacted,
        proposals_purged=proposals_purged,
        proposals_failed=proposals_failed,
        run_history_failed=run_history_failed,
    )
    audit_logger.info("run-history retention sweep %s", json.dumps(report.to_dict()))
    return report


class RetentionSweeper:
    """
    Runs ``sweep`` on a daemon thread: once at start, then every interval.

    A sweep that raises, or a phase that fails inside one, is logged and the
    loop carries on, so retention can never take the agent runtime down. With a policy that never sweeps, ``start``
    does nothing.
    """

    def __init__(
        self,
        store: ExecutionStore,
        policy: RetentionPolicy,
        *,
        clock: Callable[[], datetime] = utcnow,
        proposal_store: Optional[ProposalStore] = None,
    ) -> None:
        self._store = store
        self._proposal_store = proposal_store
        self._policy = policy
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[SweepReport] = None

    @property
    def last_report(self) -> Optional[SweepReport]:
        return self._last_report

    def run_once(self) -> Optional[SweepReport]:
        """Sweep once; return the report, or None if the sweep itself raised."""
        try:
            report = sweep(
                self._store,
                self._policy,
                now=self._clock(),
                proposal_store=self._proposal_store,
            )
        except Exception:
            logger.exception("Run-history retention sweep failed")
            return None
        self._last_report = report
        return report

    def start(self) -> None:
        if not self._policy.sweeps or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="run-history-retention", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        interval = self._policy.sweep_interval_hours * 3600
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(interval)


def main(argv: Optional[list] = None) -> int:
    """CLI: sweep the AGENTS_RUN_HISTORY_DB and AGENTS_GOVERNANCE_DB databases."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.agents.run_history_retention",
        description=(
            "Apply the configured retention to durable AgentRun history and "
            "agent governance proposals."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "report what would be abandoned, redacted and purged without "
            "changing anything"
        ),
    )
    args = parser.parse_args(argv)

    try:
        policy = RetentionPolicy.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    db_path = os.environ.get("AGENTS_RUN_HISTORY_DB")
    governance_db = os.environ.get("AGENTS_GOVERNANCE_DB")
    if not db_path and not governance_db:
        print(
            "error: neither AGENTS_RUN_HISTORY_DB nor AGENTS_GOVERNANCE_DB is set",
            file=sys.stderr,
        )
        return 2
    for path, what in ((db_path, "run-history"), (governance_db, "governance")):
        if path and not os.path.isfile(path):
            print(f"error: no {what} database at {path}", file=sys.stderr)
            return 2
    if not policy.sweeps:
        print("Retention is not configured; run history is kept forever.")
        return 0

    from .execution import SqliteExecutionStore
    from .governance.store import SqliteProposalStore

    # A dry run opens read-only so it creates no WAL file and no schema.
    store = proposal_store = None
    try:
        if db_path:
            store = SqliteExecutionStore(db_path, read_only=args.dry_run)
        if governance_db:
            proposal_store = SqliteProposalStore(governance_db, read_only=args.dry_run)
        report = sweep(
            store, policy, dry_run=args.dry_run, proposal_store=proposal_store
        )
    finally:
        if store is not None:
            store.close()
        if proposal_store is not None:
            proposal_store.close()
    print(json.dumps(report.to_dict(), indent=2))
    return 1 if report.proposals_failed or report.run_history_failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
