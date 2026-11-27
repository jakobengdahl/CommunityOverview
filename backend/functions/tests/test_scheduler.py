# covers: backend/functions/scheduler.py, backend/functions/graph_change.py, backend/functions/schedule_slots.py
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


def test_scheduler_thread_binds_application_admission(tmp_path):
    import threading
    from backend.runtime.extensions import (
        RuntimeExtensions,
        bind_runtime_extensions,
        get_runtime_extensions,
    )

    extensions = RuntimeExtensions()
    service = SimpleNamespace(store=SimpleNamespace(list_jobs=lambda **kwargs: []))
    slots = ScheduleSlots(tmp_path / "slots.db")
    with bind_runtime_extensions(extensions):
        scheduler = FunctionScheduler(service, slots, {})
    seen = []
    done = threading.Event()

    def tick(now):
        seen.append(get_runtime_extensions())
        done.set()

    scheduler.tick = tick
    scheduler.start()
    assert done.wait(5)
    scheduler.stop()
    assert seen == [extensions]


def test_graph_events_match_exclude_origin_and_survive_restart(tmp_path, monkeypatch):
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )

    manifest = {
        "triggers": [
            {
                "kind": "graph_change",
                "node_types": ["Task"],
                "fields": ["metadata.status"],
                "before": {"metadata.status": "open"},
                "after": {"metadata.status": "done"},
            }
        ]
    }
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest", lambda *args: (manifest, None)
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    path = tmp_path / "slots.db"
    scheduler = FunctionScheduler(service, ScheduleSlots(path), {"example": "g"})
    event = Event(
        event_type=EventType.NODE_UPDATE,
        origin=EventContext(
            event_origin="function:other@1.0.0",
            event_correlation_id="corr",
            event_causation_depth=2,
        ),
        entity=EntityData(
            kind=EntityKind.NODE,
            id="n",
            type="Task",
            before={"metadata": {"origin_graph_id": "g", "status": "open"}},
            after={"metadata": {"origin_graph_id": "g", "status": "done"}},
            patch={"metadata": {"origin_graph_id": "g", "status": "done"}},
        ),
    )
    scheduler.on_graph_event(event)
    scheduler.on_graph_event(event)
    other = event.model_copy(deep=True)
    other.event_id = "ignored"
    other.origin.event_origin = "function:example@9.0.0"
    scheduler.on_graph_event(other)
    other.origin.event_origin = "web-ui"
    other.entity.after["metadata"]["status"] = "open"
    scheduler.on_graph_event(other)
    scheduler.tick(datetime.now(timezone.utc))
    assert service.invoke.call_count == 1
    call = service.invoke.call_args.kwargs
    assert call["trigger"] == "graph_change"
    assert call["entity"] == event.to_webhook_payload()["entity"]
    assert call["chain_depth"] == 3 and call["correlation_id"] == "corr"
    scheduler.stop()
    scheduler = FunctionScheduler(service, ScheduleSlots(path), {"example": "g"})
    scheduler.on_graph_event(event)
    scheduler.tick(datetime.now(timezone.utc))
    assert service.invoke.call_count == 1
    scheduler.stop()
