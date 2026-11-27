# covers: backend/functions/scheduler.py
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from backend.agents.execution import ExecutionState
from backend.functions.schedule_slots import ScheduleSlots
from backend.functions.scheduler import FunctionScheduler


def test_discovery_and_restart_catchup(tmp_path, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    manifest = {"triggers": [{"kind": "schedule", "interval_seconds": 60}]}
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest", lambda *args: (manifest, None)
    )
    monkeypatch.setattr(
        "backend.functions.invocation.load_manifest", lambda *args: (manifest, None)
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    path = tmp_path / "slots.db"
    slots = ScheduleSlots(path)
    slots.enqueue("example", now - timedelta(minutes=3))
    scheduler = FunctionScheduler(service, slots, {"example": "graph"})
    scheduler.tick(now)
    assert service.invoke.call_count == 1
    assert service.invoke.call_args.kwargs["actor_binding"]["graph_id"] == "graph"
    assert (
        service.invoke.call_args.kwargs["actor_binding"]["actor_type"]
        == "function_schedule"
    )
    assert (
        slots.enqueue("example", now - timedelta(minutes=3)).state
        == ExecutionState.CANCELLED
    )
    scheduler.stop()
    scheduler = FunctionScheduler(service, ScheduleSlots(path), {"example": "graph"})
    scheduler.tick(now + timedelta(seconds=10))
    assert service.invoke.call_count == 1
    scheduler.tick(now + timedelta(minutes=20))
    assert service.invoke.call_count == 2
    scheduler.stop()
