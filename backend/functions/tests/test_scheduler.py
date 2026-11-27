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
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
        enabled={"example": []},
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    path = tmp_path / "slots.db"
    slots = ScheduleSlots(path)
    slots.enqueue("example", now - timedelta(minutes=3))
    scheduler = FunctionScheduler(service, slots, {"example": "graph"})
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr("backend.functions.scheduler.datetime", clock)
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
    clock.now.return_value = now + timedelta(minutes=20)
    scheduler.tick(now + timedelta(minutes=20))
    assert service.invoke.call_count == 2
    scheduler.stop()


def test_rate_limit_uses_each_dispatch_time(tmp_path, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr("backend.functions.scheduler.datetime", clock)
    manifest = {"triggers": []}
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest", lambda *args: (manifest, None)
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"a": [], "b": []},
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    slots = ScheduleSlots(tmp_path / "slots.db", rate_limit=1)
    slots.enqueue("a", now)
    slots.enqueue("b", now)
    scheduler = FunctionScheduler(service, slots, {"a": "graph", "b": "graph"})

    def slow_first(*args, **kwargs):
        clock.now.return_value = now + timedelta(seconds=40)
        return SimpleNamespace(state=ExecutionState.SUCCEEDED)

    service.invoke.side_effect = slow_first
    scheduler.tick(now)
    clock.now.return_value = now + timedelta(seconds=61)
    slots.enqueue("b", now + timedelta(seconds=61))
    scheduler.tick(now + timedelta(seconds=61))
    assert [call.args[0] for call in service.invoke.call_args_list] == ["a", "b"]
    scheduler.stop()
