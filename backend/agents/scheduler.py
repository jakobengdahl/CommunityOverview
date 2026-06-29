"""
Time-based agent scheduler.

Runs a background thread that fires agents according to a configured day+time schedule.
Complements the event-subscription trigger path.
"""

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AgentConfig

if TYPE_CHECKING:
    from .worker import AgentWorker

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 30  # seconds between schedule checks


class AgentScheduler:
    """
    Background scheduler that enqueues time-triggered events to agent workers.

    Each agent may have one schedule (day_of_week + hour:minute + timezone).
    The scheduler wakes every _CHECK_INTERVAL seconds and fires any agent whose
    schedule matches the current wall-clock time.  A per-agent deduplication key
    prevents double-firing within the same calendar minute.
    """

    def __init__(self) -> None:
        # agent_id -> (config, worker)
        self._entries: Dict[str, Tuple["AgentConfig", "AgentWorker"]] = {}
        # agent_id -> (year, month, day, hour, minute) of last fire in schedule tz
        self._last_fired: Dict[str, Tuple[int, int, int, int, int]] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent_id: str, config: "AgentConfig", worker: "AgentWorker") -> None:
        """Register an agent for scheduled firing.  No-op when no schedule is set."""
        if not config.schedule:
            return
        with self._lock:
            self._entries[agent_id] = (config, worker)
        logger.info(
            "Scheduler: registered agent '%s' (%s %02d:%02d %s)",
            config.name,
            config.schedule.day_name,
            config.schedule.hour,
            config.schedule.minute,
            config.schedule.timezone,
        )

    def unregister(self, agent_id: str) -> None:
        """Remove an agent from the scheduler."""
        with self._lock:
            self._entries.pop(agent_id, None)
            self._last_fired.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background thread.  Idempotent: no-op if already running."""
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run,
                name="agent-scheduler",
                daemon=True,
            )
            self._thread.start()
        logger.info("Agent scheduler started (check interval: %ds)", _CHECK_INTERVAL)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background thread, waking it immediately via the stop event."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("Agent scheduler stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            try:
                self._check_and_fire()
            except Exception as exc:
                logger.error("Scheduler error: %s", exc)
            # Use event.wait instead of sleep so stop() can wake the thread early
            self._stop_event.wait(_CHECK_INTERVAL)

    def _check_and_fire(self) -> None:
        """Check all registered agents and fire those whose schedule matches now."""
        with self._lock:
            snapshot = dict(self._entries)

        for agent_id, (config, worker) in snapshot.items():
            schedule = config.schedule
            if not schedule:
                continue

            try:
                tz = ZoneInfo(schedule.timezone)
            except ZoneInfoNotFoundError:
                logger.warning(
                    "Scheduler: unknown timezone '%s' for agent '%s', falling back to UTC",
                    schedule.timezone,
                    config.name,
                )
                tz = ZoneInfo("UTC")

            now = datetime.now(tz)

            if not (
                now.weekday() == schedule.day_of_week
                and now.hour == schedule.hour
                and now.minute == schedule.minute
            ):
                continue

            minute_key = (now.year, now.month, now.day, now.hour, now.minute)
            with self._lock:
                if self._last_fired.get(agent_id) == minute_key:
                    continue
                self._last_fired[agent_id] = minute_key

            payload = _build_payload(config, now)
            worker.enqueue(payload)
            logger.info(
                "Scheduler: fired agent '%s' (scheduled %s %02d:%02d %s)",
                config.name,
                schedule.day_name,
                schedule.hour,
                schedule.minute,
                schedule.timezone,
            )


def _build_payload(config: "AgentConfig", fired_at: datetime) -> Dict:
    """Build the event payload for a scheduled trigger."""
    schedule = config.schedule
    return {
        "event_id": f"sched-{uuid.uuid4()}",
        "event_type": "scheduled_trigger",
        "occurred_at": fired_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "origin": {
            "event_origin": "scheduler",
            "event_session_id": None,
            "event_correlation_id": None,
        },
        "schedule": schedule.to_dict() if schedule else {},
        "entity": None,
        "subscription": None,
    }
