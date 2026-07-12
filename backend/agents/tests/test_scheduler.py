"""
Tests for time-based agent scheduler.
"""

import unittest.mock
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from backend.agents.config import AgentConfig, AgentSchedule, AgentPrompts
from backend.agents.scheduler import AgentScheduler, _build_payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    agent_id: str = "agent-1",
    schedule: AgentSchedule | None = None,
) -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        name="Test Agent",
        enabled=True,
        prompts=AgentPrompts(task_prompt="do stuff"),
        schedule=schedule,
    )


def make_worker(agent_id: str = "agent-1") -> MagicMock:
    worker = MagicMock()
    worker.agent_id = agent_id
    worker.enqueued: List[Dict[str, Any]] = []
    worker.enqueue.side_effect = lambda payload: worker.enqueued.append(payload)
    return worker


# ---------------------------------------------------------------------------
# AgentSchedule.from_dict
# ---------------------------------------------------------------------------


class TestAgentScheduleFromDict:
    def test_parse_integer_day_and_time_string(self):
        sched = AgentSchedule.from_dict(
            {"day_of_week": 1, "time": "14:00", "timezone": "Europe/Stockholm"}
        )
        assert sched is not None
        assert sched.day_of_week == 1
        assert sched.hour == 14
        assert sched.minute == 0
        assert sched.timezone == "Europe/Stockholm"

    def test_parse_weekday_name(self):
        sched = AgentSchedule.from_dict({"day_of_week": "tuesday", "time": "09:30"})
        assert sched is not None
        assert sched.day_of_week == 1
        assert sched.hour == 9
        assert sched.minute == 30

    def test_parse_hour_minute_separately(self):
        sched = AgentSchedule.from_dict({"day_of_week": 0, "hour": 8, "minute": 15})
        assert sched is not None
        assert sched.hour == 8
        assert sched.minute == 15

    def test_weekday_name_case_insensitive(self):
        assert (
            AgentSchedule.from_dict({"day_of_week": "MONDAY", "time": "10:00"})
            is not None
        )
        assert (
            AgentSchedule.from_dict({"day_of_week": "Monday", "time": "10:00"})
            is not None
        )

    def test_default_timezone_is_utc(self):
        sched = AgentSchedule.from_dict({"day_of_week": 0, "time": "10:00"})
        assert sched is not None
        assert sched.timezone == "UTC"

    def test_returns_none_for_empty_dict(self):
        assert AgentSchedule.from_dict({}) is None

    def test_returns_none_when_day_missing(self):
        assert AgentSchedule.from_dict({"time": "10:00"}) is None

    def test_returns_none_for_invalid_day_name(self):
        assert (
            AgentSchedule.from_dict({"day_of_week": "funday", "time": "10:00"}) is None
        )

    def test_returns_none_for_out_of_range_day(self):
        assert AgentSchedule.from_dict({"day_of_week": 7, "time": "10:00"}) is None

    def test_returns_none_for_invalid_hour(self):
        assert (
            AgentSchedule.from_dict({"day_of_week": 0, "hour": 25, "minute": 0}) is None
        )

    def test_returns_none_for_invalid_minute(self):
        assert (
            AgentSchedule.from_dict({"day_of_week": 0, "hour": 10, "minute": 60})
            is None
        )

    def test_returns_none_for_invalid_timezone(self):
        assert (
            AgentSchedule.from_dict(
                {"day_of_week": 0, "time": "10:00", "timezone": "Not/AZone"}
            )
            is None
        )

    def test_valid_iana_timezone_accepted(self):
        sched = AgentSchedule.from_dict(
            {"day_of_week": 0, "time": "10:00", "timezone": "Europe/Stockholm"}
        )
        assert sched is not None
        assert sched.timezone == "Europe/Stockholm"

    def test_sunday_is_day_6(self):
        sched = AgentSchedule.from_dict({"day_of_week": "sunday", "time": "00:00"})
        assert sched is not None
        assert sched.day_of_week == 6

    def test_day_name_property(self):
        sched = AgentSchedule.from_dict({"day_of_week": 2, "time": "12:00"})
        assert sched.day_name == "Wednesday"

    def test_to_dict_round_trip(self):
        original = {"day_of_week": 3, "time": "07:45", "timezone": "UTC"}
        sched = AgentSchedule.from_dict(original)
        d = sched.to_dict()
        assert d["day_of_week"] == 3
        assert d["day_name"] == "Thursday"
        assert d["hour"] == 7
        assert d["minute"] == 45
        assert d["timezone"] == "UTC"
        assert d["cron"] == "45 7 * * 4"  # Thursday=4 in cron (0=Sun)

    def test_to_cron_monday(self):
        # Python Monday=0, cron Monday=1
        sched = AgentSchedule(day_of_week=0, hour=9, minute=0)
        assert sched.to_cron() == "0 9 * * 1"

    def test_to_cron_sunday(self):
        # Python Sunday=6, cron Sunday=0
        sched = AgentSchedule(day_of_week=6, hour=23, minute=30)
        assert sched.to_cron() == "30 23 * * 0"

    def test_to_cron_tuesday(self):
        # Python Tuesday=1, cron Tuesday=2
        sched = AgentSchedule(day_of_week=1, hour=14, minute=0)
        assert sched.to_cron() == "0 14 * * 2"


# ---------------------------------------------------------------------------
# AgentConfig.schedule field
# ---------------------------------------------------------------------------


class TestAgentConfigSchedule:
    def test_schedule_is_none_when_not_in_metadata(self, sample_agent_node):
        from backend.agents.config import AgentConfig

        config = AgentConfig.from_node(sample_agent_node)
        assert config.schedule is None

    def test_schedule_parsed_from_metadata(self, sample_agent_node):
        sample_agent_node.metadata["schedule"] = {
            "day_of_week": "friday",
            "time": "16:00",
            "timezone": "Europe/Stockholm",
        }
        from backend.agents.config import AgentConfig

        config = AgentConfig.from_node(sample_agent_node)
        assert config.schedule is not None
        assert config.schedule.day_of_week == 4
        assert config.schedule.hour == 16
        assert config.schedule.day_name == "Friday"

    def test_to_dict_includes_schedule(self, sample_agent_node):
        sample_agent_node.metadata["schedule"] = {"day_of_week": 0, "time": "10:00"}
        from backend.agents.config import AgentConfig

        config = AgentConfig.from_node(sample_agent_node)
        d = config.to_dict()
        assert d["schedule"] is not None
        assert d["schedule"]["day_of_week"] == 0

    def test_to_dict_schedule_none_when_not_set(self, sample_agent_node):
        from backend.agents.config import AgentConfig

        config = AgentConfig.from_node(sample_agent_node)
        assert config.to_dict()["schedule"] is None


# ---------------------------------------------------------------------------
# AgentScheduler
# ---------------------------------------------------------------------------


class TestAgentSchedulerRegistration:
    def test_register_agent_with_schedule(self):
        scheduler = AgentScheduler()
        config = make_config(schedule=AgentSchedule(day_of_week=0, hour=9, minute=0))
        worker = make_worker()
        scheduler.register("agent-1", config, worker)
        assert "agent-1" in scheduler._entries

    def test_register_agent_without_schedule_is_noop(self):
        scheduler = AgentScheduler()
        config = make_config(schedule=None)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)
        assert "agent-1" not in scheduler._entries

    def test_unregister_removes_entry(self):
        scheduler = AgentScheduler()
        config = make_config(schedule=AgentSchedule(day_of_week=0, hour=9, minute=0))
        worker = make_worker()
        scheduler.register("agent-1", config, worker)
        scheduler.unregister("agent-1")
        assert "agent-1" not in scheduler._entries

    def test_unregister_unknown_agent_is_noop(self):
        scheduler = AgentScheduler()
        scheduler.unregister("no-such-agent")  # must not raise


# ---------------------------------------------------------------------------
# AgentScheduler._check_and_fire
# ---------------------------------------------------------------------------


class TestAgentSchedulerFiring:
    def test_fires_when_time_matches(self):
        """Fires the worker when current time matches the schedule."""
        scheduler = AgentScheduler()
        sched = AgentSchedule(day_of_week=0, hour=10, minute=30, timezone="UTC")
        config = make_config(schedule=sched)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)

        # 2026-06-29 is a Monday
        monday_1030 = datetime(2026, 6, 29, 10, 30, 0, tzinfo=ZoneInfo("UTC"))
        assert monday_1030.weekday() == 0

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_1030.astimezone(tz_arg) if tz_arg else monday_1030
            )
            scheduler._check_and_fire()

        assert len(worker.enqueued) == 1
        payload = worker.enqueued[0]
        assert payload["event_type"] == "scheduled_trigger"
        assert payload["origin"]["event_origin"] == "scheduler"
        assert payload["schedule"]["day_name"] == "Monday"

    def test_does_not_fire_when_time_does_not_match(self):
        scheduler = AgentScheduler()
        sched = AgentSchedule(day_of_week=0, hour=10, minute=30, timezone="UTC")
        config = make_config(schedule=sched)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)

        # Tuesday 10:30 — right time, wrong day
        tuesday_1030 = datetime(2026, 6, 30, 10, 30, 0, tzinfo=ZoneInfo("UTC"))
        assert tuesday_1030.weekday() == 1

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                tuesday_1030.astimezone(tz_arg) if tz_arg else tuesday_1030
            )
            scheduler._check_and_fire()

        assert len(worker.enqueued) == 0

    def test_does_not_fire_twice_in_same_minute(self):
        scheduler = AgentScheduler()
        sched = AgentSchedule(day_of_week=0, hour=10, minute=30, timezone="UTC")
        config = make_config(schedule=sched)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)

        monday_1030 = datetime(2026, 6, 29, 10, 30, 0, tzinfo=ZoneInfo("UTC"))

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_1030.astimezone(tz_arg) if tz_arg else monday_1030
            )
            scheduler._check_and_fire()
            scheduler._check_and_fire()  # second call in same minute

        assert len(worker.enqueued) == 1  # fired only once

    def test_does_not_fire_on_non_matching_minute(self):
        """Scheduler fires once at the scheduled minute then stays quiet that hour."""
        scheduler = AgentScheduler()
        sched = AgentSchedule(day_of_week=0, hour=10, minute=30, timezone="UTC")
        config = make_config(schedule=sched)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)

        monday_1030 = datetime(2026, 6, 29, 10, 30, 0, tzinfo=ZoneInfo("UTC"))
        monday_1031 = datetime(2026, 6, 29, 10, 31, 0, tzinfo=ZoneInfo("UTC"))

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_1030.astimezone(tz_arg) if tz_arg else monday_1030
            )
            scheduler._check_and_fire()

        assert len(worker.enqueued) == 1

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_1031.astimezone(tz_arg) if tz_arg else monday_1031
            )
            scheduler._check_and_fire()

        assert len(worker.enqueued) == 1  # minute=31 does not match

    def test_fires_again_following_week(self):
        """Deduplication key is date-specific, so the agent re-fires the next Monday."""
        scheduler = AgentScheduler()
        sched = AgentSchedule(day_of_week=0, hour=10, minute=30, timezone="UTC")
        config = make_config(schedule=sched)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)

        monday_week1 = datetime(2026, 6, 29, 10, 30, 0, tzinfo=ZoneInfo("UTC"))
        monday_week2 = datetime(2026, 7, 6, 10, 30, 0, tzinfo=ZoneInfo("UTC"))
        assert monday_week1.weekday() == monday_week2.weekday() == 0

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_week1.astimezone(tz_arg) if tz_arg else monday_week1
            )
            scheduler._check_and_fire()

        assert len(worker.enqueued) == 1

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_week2.astimezone(tz_arg) if tz_arg else monday_week2
            )
            scheduler._check_and_fire()

        assert len(worker.enqueued) == 2  # fires again the following Monday

    def test_unregister_prevents_firing(self):
        scheduler = AgentScheduler()
        sched = AgentSchedule(day_of_week=0, hour=10, minute=30, timezone="UTC")
        config = make_config(schedule=sched)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)
        scheduler.unregister("agent-1")

        monday_1030 = datetime(2026, 6, 29, 10, 30, 0, tzinfo=ZoneInfo("UTC"))

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_1030.astimezone(tz_arg) if tz_arg else monday_1030
            )
            scheduler._check_and_fire()

        assert len(worker.enqueued) == 0

    def test_unknown_timezone_falls_back_to_utc(self):
        scheduler = AgentScheduler()
        sched = AgentSchedule(
            day_of_week=0, hour=10, minute=30, timezone="Invalid/Zone"
        )
        config = make_config(schedule=sched)
        worker = make_worker()
        scheduler.register("agent-1", config, worker)

        monday_1030_utc = datetime(2026, 6, 29, 10, 30, 0, tzinfo=ZoneInfo("UTC"))

        with unittest.mock.patch("backend.agents.scheduler.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz_arg=None: (
                monday_1030_utc.astimezone(tz_arg)
                if tz_arg and str(tz_arg) != "Invalid/Zone"
                else monday_1030_utc
            )
            # ZoneInfoNotFoundError is raised inside scheduler; it should catch and use UTC
            scheduler._check_and_fire()

        # It fires because fallback to UTC matches
        assert len(worker.enqueued) == 1


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_payload_structure(self):
        sched = AgentSchedule(
            day_of_week=1, hour=14, minute=0, timezone="Europe/Stockholm"
        )
        config = make_config(schedule=sched)
        fired_at = datetime(2026, 7, 7, 14, 0, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
        payload = _build_payload(config, fired_at)

        assert payload["event_type"] == "scheduled_trigger"
        assert payload["origin"]["event_origin"] == "scheduler"
        assert payload["entity"] is None
        assert payload["subscription"] is None
        assert payload["schedule"]["day_name"] == "Tuesday"
        assert payload["schedule"]["hour"] == 14
        assert payload["schedule"]["timezone"] == "Europe/Stockholm"
        assert payload["event_id"].startswith("sched-")
        assert payload["occurred_at"].endswith("Z")
