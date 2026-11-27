# covers: backend/functions/scheduler.py, backend/functions/graph_change.py, backend/functions/schedule_slots.py
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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
    other.origin.event_origin = "external-change"
    scheduler.on_graph_event(other)
    other.origin.event_origin = "web-ui"
    other.entity.after["metadata"]["status"] = "open"
    scheduler.on_graph_event(other)
    scheduler.tick(datetime.now(timezone.utc))
    assert service.invoke.call_count == 1
    call = service.invoke.call_args.kwargs
    assert call["trigger"] == "graph_change"
    assert call["entity"] == {
        "kind": "node",
        "id": "n",
        "type": "Task",
        "changed_fields": ["metadata.status"],
        "data": {
            "before": {"metadata": {"status": "open"}},
            "after": {"metadata": {"status": "done"}},
            "patch": {"metadata": {"status": "done"}},
        },
    }
    assert call["chain_depth"] == 3 and call["correlation_id"] == "corr"
    scheduler.stop()
    scheduler = FunctionScheduler(service, ScheduleSlots(path), {"example": "g"})
    scheduler.on_graph_event(event)
    scheduler.tick(datetime.now(timezone.utc))
    assert service.invoke.call_count == 1
    scheduler.stop()


@pytest.mark.parametrize(
    "before,after,predicate,expected",
    [
        (False, 0, {}, True),
        (True, 1, {}, True),
        ({"x": [False]}, {"x": [0]}, {}, True),
        (1, 1.0, {}, False),
        (0, 1, {"before": {"value": False}}, False),
        (False, 1, {"before": {"value": 0}}, False),
        (None, False, {"after": {"value": False}}, True),
    ],
)
def test_graph_json_transitions(before, after, predicate, expected):
    from backend.functions.graph_change import matches

    event = Mock()
    event.to_webhook_payload.return_value = {
        "entity": {
            "kind": "node",
            "type": "Task",
            "data": {"before": {"value": before}, "after": {"value": after}},
        }
    }
    trigger = {"node_types": ["Task"], "fields": ["value"], **predicate}
    assert matches(trigger, event) is expected


@pytest.mark.parametrize(
    "before_graph,after_graph,local,expected",
    [
        ("g", "other", "local", False),
        ("other", "g", "local", False),
        (None, None, "g", False),
        (None, "g", "g", False),
        (None, "g", "other", False),
        (None, None, None, False),
        ("g", "g", "other", True),
    ],
)
def test_graph_scope_and_sensitive_entity(
    tmp_path, monkeypatch, before_graph, after_graph, local, expected
):
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )

    trigger = {"kind": "graph_change", "node_types": ["Task"], "fields": ["name"]}
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest",
        lambda *args: ({"triggers": [trigger]}, None),
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    slots = ScheduleSlots(tmp_path / "scope.db")
    scheduler = FunctionScheduler(
        service, slots, {"example": "g"}, local_graph_id=local
    )

    def snapshot(name, graph):
        return {
            "name": name,
            "metadata": {
                "origin_graph_id": graph,
                "delivery": {"webhook_url": "https://example.test/hook?token=secret"},
                "nested": [{"api_key": "secret"}],
            },
        }

    event = Event(
        event_type=EventType.NODE_UPDATE,
        origin=EventContext(),
        entity=EntityData(
            kind=EntityKind.NODE,
            id="n",
            type="Task",
            before=snapshot("old", before_graph),
            after=snapshot("new", after_graph),
            patch={"url": "https://example.test/secret"},
        ),
    )
    scheduler.on_graph_event(event)
    rows = slots._store._conn.execute("SELECT payload FROM schedule_jobs").fetchall()
    assert bool(rows) is expected
    if expected:
        assert "secret" not in rows[0][0]
        scheduler.tick(datetime.now(timezone.utc))
        entity = service.invoke.call_args.kwargs["entity"]
        assert entity["data"]["before"]["name"] == "old"
        assert entity["data"]["after"] == {"name": "new"}
        assert entity["data"]["patch"] == {}
    scheduler.stop()


@pytest.mark.parametrize("sensitive_type", ["Task", "Legacy"])
def test_graph_entity_projects_declared_fields_and_excludes_schema_sensitive(
    tmp_path, monkeypatch, sensitive_type
):
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )

    trigger = {
        "kind": "graph_change",
        "node_types": ["Task"],
        "fields": ["metadata", "metadata.private"],
        "before": {"name": "old"},
    }
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest",
        lambda *args: ({"triggers": [trigger]}, None),
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    slots = ScheduleSlots(tmp_path / "projection.db")
    scheduler = FunctionScheduler(
        service,
        slots,
        {"example": "g"},
        sensitive_fields={sensitive_type: ["metadata.private"]},
    )
    event = Event(
        event_type=EventType.NODE_UPDATE,
        origin=EventContext(),
        entity=EntityData(
            kind=EntityKind.NODE,
            id="n",
            type="Task",
            before={
                "type": "Legacy",
                "name": "old",
                "metadata": {
                    "origin_graph_id": "g",
                    "url": "https://example.test",
                    "private": "hidden",
                },
                "other": "unlisted",
            },
            after={
                "type": "Task",
                "name": "new",
                "metadata": {
                    "origin_graph_id": "g",
                    "url": "https://example.test/new",
                    "private": "hidden2",
                },
                "other": "unlisted2",
            },
            patch={
                "metadata": {"url": "https://example.test/new", "private": "hidden2"},
                "other": "unlisted2",
            },
        ),
    )
    scheduler.on_graph_event(event)
    row = slots._store._conn.execute("SELECT payload FROM schedule_jobs").fetchone()[0]
    assert "hidden" not in row and "unlisted" not in row
    scheduler.tick(datetime.now(timezone.utc))
    entity = service.invoke.call_args.kwargs["entity"]
    assert entity["data"]["before"] == {
        "name": "old",
        "metadata": {"origin_graph_id": "g", "url": "https://example.test"},
    }
    assert entity["data"]["patch"] == {"metadata": {"url": "https://example.test/new"}}
    assert entity["changed_fields"] == [
        "metadata.private",
        "metadata.url",
        "name",
        "other",
        "type",
    ]
    scheduler.stop()


def test_untagged_graph_nodes_never_reach_bound_function(tmp_path, monkeypatch):
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )

    trigger = {"kind": "graph_change", "node_types": ["Task"], "fields": ["name"]}
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest",
        lambda *args: ({"triggers": [trigger]}, None),
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
    )
    slots = ScheduleSlots(tmp_path / "untagged.db")
    scheduler = FunctionScheduler(service, slots, {"example": "g"}, local_graph_id="g")
    for before, after in [
        ({"name": "a"}, {"name": "b"}),
        ({"name": "a"}, {"name": "b", "metadata": {"origin_graph_id": "g"}}),
    ]:
        scheduler.on_graph_event(
            Event(
                event_type=EventType.NODE_UPDATE,
                origin=EventContext(),
                entity=EntityData(
                    kind=EntityKind.NODE,
                    id="n",
                    type="Task",
                    before=before,
                    after=after,
                ),
            )
        )
    assert (
        slots._store._conn.execute("SELECT COUNT(*) FROM schedule_jobs").fetchone()[0]
        == 0
    )
    scheduler.stop()


def test_queued_graph_event_is_skipped_when_binding_changes(tmp_path, monkeypatch):
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )

    manifest = {
        "triggers": [
            {"kind": "graph_change", "node_types": ["Task"], "fields": ["name"]}
        ]
    }
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest", lambda *args: (manifest, None)
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
        invoke=Mock(),
    )
    bindings = {"example": "g"}
    slots = ScheduleSlots(tmp_path / "binding.db")
    scheduler = FunctionScheduler(service, slots, bindings)
    event = Event(
        event_type=EventType.NODE_UPDATE,
        origin=EventContext(),
        entity=EntityData(
            kind=EntityKind.NODE,
            id="n",
            type="Task",
            before={"name": "old", "metadata": {"origin_graph_id": "g"}},
            after={"name": "new", "metadata": {"origin_graph_id": "g"}},
        ),
    )
    scheduler.on_graph_event(event)
    bindings["example"] = "other"
    scheduler.tick(datetime.now(timezone.utc))
    row = slots._store._conn.execute(
        "SELECT state, dead_letter_reason FROM schedule_jobs"
    ).fetchone()
    assert (row["state"], row["dead_letter_reason"]) == (
        ExecutionState.CANCELLED.value,
        "binding_changed",
    )
    service.invoke.assert_not_called()
    scheduler.stop()


def test_dispatch_rechecks_manifest_and_current_sensitive_fields(tmp_path, monkeypatch):
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )

    trigger = {
        "kind": "graph_change",
        "node_types": ["Task"],
        "fields": ["name", "metadata"],
    }
    manifest = {"triggers": [trigger]}
    monkeypatch.setattr(
        "backend.functions.scheduler.load_manifest", lambda *args: (manifest, None)
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        store=SimpleNamespace(list_jobs=lambda **kwargs: []),
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    schema = {"Task": []}
    slots = ScheduleSlots(tmp_path / "manifest.db")
    scheduler = FunctionScheduler(
        service, slots, {"example": "g"}, sensitive_fields=schema
    )
    event = Event(
        event_type=EventType.NODE_UPDATE,
        origin=EventContext(),
        entity=EntityData(
            kind=EntityKind.NODE,
            id="n",
            type="Task",
            before={
                "name": "old",
                "metadata": {"origin_graph_id": "g", "secret": "a", "visible": "x"},
            },
            after={
                "name": "new",
                "metadata": {"origin_graph_id": "g", "secret": "b", "visible": "y"},
            },
            patch={"name": "new", "metadata": {"secret": "b", "visible": "y"}},
        ),
    )
    scheduler.on_graph_event(event)
    event2 = event.model_copy(deep=True)
    event2.event_id = "second"
    scheduler.on_graph_event(event2)
    trigger["fields"] = ["name", "metadata.secret"]
    schema["Task"] = ["metadata.secret"]
    scheduler.tick(datetime.now(timezone.utc))
    entity = service.invoke.call_args.kwargs["entity"]
    assert entity["data"] == {
        "before": {"name": "old"},
        "after": {"name": "new"},
        "patch": {"name": "new"},
    }
    assert service.invoke.call_count == 2
    event3 = event.model_copy(deep=True)
    event3.event_id = "third"
    scheduler.on_graph_event(event3)
    trigger["after"] = {"name": "different"}
    scheduler.tick(datetime.now(timezone.utc))
    assert service.invoke.call_count == 2
    row = slots._store._conn.execute(
        "SELECT state, dead_letter_reason FROM schedule_jobs WHERE dead_letter_reason = 'trigger_changed'"
    ).fetchone()
    assert row["state"] == ExecutionState.CANCELLED.value
    # Matching must survive removal of the very values used by the trigger.
    trigger["fields"] = ["metadata.secret"]
    trigger["before"] = {"metadata.secret": "a"}
    trigger["after"] = {"metadata.secret": "b"}
    event4 = event.model_copy(deep=True)
    event4.event_id = "fourth"
    scheduler.on_graph_event(event4)
    row = slots._store._conn.execute(
        "SELECT payload FROM schedule_jobs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    assert '"secret": "a"' not in row and '"secret": "b"' not in row
    scheduler.stop()
    scheduler = FunctionScheduler(
        service,
        ScheduleSlots(tmp_path / "manifest.db"),
        {"example": "g"},
        sensitive_fields=schema,
    )
    scheduler.tick(datetime.now(timezone.utc))
    assert service.invoke.call_count == 3
    assert service.invoke.call_args.kwargs["entity"]["data"] == {
        "before": {},
        "after": {},
        "patch": {},
    }
    scheduler.stop()
