# covers: backend/service/session_restore.py, scripts/session_restore_file_snapshot.py
import copy
import hashlib
from contextlib import contextmanager

import pytest

from backend.core.session_store import InMemorySessionPersistenceBackend, Session
from backend.core.write_modes import WriteModes
from backend.runtime.authorization import GraphAuthorizationDecision
from backend.service.session_backup import canonical_bytes, create_backup
from backend.service.session_restore import RestoreRefused, restore_backup
from scripts.session_restore_file_snapshot import _publish


class Hook:
    def __init__(self):
        self.allowed = True

    def evaluate(self, context):
        return GraphAuthorizationDecision(allowed=self.allowed)


class Service:
    def __init__(self):
        self.authorization_hook = Hook()
        self.allowed = True

    def authorize_capture_refs(self, nodes, edges, *, target):
        return {"success": self.allowed, "missing_node_ids": nodes}


class Modes(WriteModes):
    @contextmanager
    def backup_snapshot(self):
        yield self


def backup():
    source = InMemorySessionPersistenceBackend()
    session = Session(id="1234-5678", name="Example", seq=7)
    session.state["node_refs"] = ["missing"]
    source.save(session.to_dict())
    return create_backup(Service(), source, Modes())


def sign(document):
    unsigned = {k: v for k, v in document.items() if k != "integrity"}
    document["integrity"]["sha256"] = hashlib.sha256(
        canonical_bytes(unsigned)
    ).hexdigest()


def test_round_trip_into_separate_store_preserves_identity_and_missing_references():
    document = backup()
    target = InMemorySessionPersistenceBackend()
    report = restore_backup(Service(), target, Modes(), document)
    record = target.load("1234-5678")
    assert record["seq"] == 7
    assert record["state"] == document["sessions"][0]["state"]
    assert report == [{"session_id": "1234-5678", "missing_node_ids": ["missing"]}]


@pytest.mark.parametrize(
    "damage", ["version", "integrity", "duplicate", "unsafe", "limit", "assets"]
)
def test_invalid_backup_never_changes_target(damage):
    document = backup()
    if damage == "version":
        document["format_version"] = 2
    elif damage == "integrity":
        document["sessions"][0]["revision"] = 9
    elif damage == "duplicate":
        document["sessions"].append(copy.deepcopy(document["sessions"][0]))
    elif damage == "unsafe":
        document["sessions"][0]["state"]["annotations"] = [
            {
                "id": "a",
                "type": "reference",
                "target_kind": "url",
                "target": "javascript:alert(1)",
            }
        ]
    elif damage == "assets":
        document["assets"] = [{"sha256": "bad", "data_url": "bad"}]
    if damage != "integrity":
        sign(document)
    target = InMemorySessionPersistenceBackend()
    with pytest.raises(RestoreRefused):
        restore_backup(
            Service(),
            target,
            Modes(),
            document,
            max_sessions=0 if damage == "limit" else 10000,
        )
    assert target.list_ids() == []


def test_denied_reference_precedes_validation_and_collision():
    document = backup()
    document["format_version"] = 2
    service = Service()
    service.allowed = False
    target = InMemorySessionPersistenceBackend()
    with pytest.raises(RestoreRefused) as denied:
        restore_backup(service, target, Modes(), document)
    service.allowed = True
    service.authorization_hook.allowed = False
    with pytest.raises(RestoreRefused) as admin_denied:
        restore_backup(service, target, Modes(), document)
    assert str(denied.value) == str(admin_denied.value)
    assert target.list_ids() == []


def test_existing_target_and_publication_collision_are_unchanged(tmp_path):
    target = InMemorySessionPersistenceBackend()
    target.save(Session(id="9999-9999").to_dict())
    before = target.load("9999-9999")
    with pytest.raises(RestoreRefused):
        restore_backup(Service(), target, Modes(), backup())
    assert target.list_ids() == ["9999-9999"]
    assert target.load("9999-9999") == before
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(OSError):
        _publish(source, output)
    assert source.is_dir() and output.is_dir()


@pytest.mark.parametrize("mode", ["direct", "staged"])
def test_offline_file_integration_publishes_only_complete_workspace(
    tmp_path, monkeypatch, mode
):
    from backend.core.session_store import FileSessionPersistenceBackend
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from scripts.session_restore_file_snapshot import open_restore

    graph = tmp_path / "graph.json"
    FileGraphPersistenceBackend(graph, staged_sessions=True).save_graph_data(
        {"nodes": [], "edges": [], "metadata": {}}
    )
    monkeypatch.setenv("SESSION_BACKUP_GRAPH", str(graph))
    output = tmp_path / "restored"
    hook = Hook()
    document = backup()
    document["sessions"][0]["write_mode"] = mode
    sign(document)
    with open_restore(hook, output) as (service, sessions, modes, publish):
        report = restore_backup(service, sessions, modes, document)
        assert not output.exists()
        publish()
    assert report[0]["missing_node_ids"] == ["missing"]
    restored = FileSessionPersistenceBackend(output / "sessions")
    assert restored.load("1234-5678")["seq"] == 7
    restored_graph = FileGraphPersistenceBackend(
        output / "graph.json", staged_sessions=True
    )
    assert restored_graph.staged_status("1234-5678") == (
        "active" if mode == "staged" else "none"
    )


def test_write_failure_discards_unpublished_workspace(tmp_path, monkeypatch):
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from scripts.session_restore_file_snapshot import open_restore

    graph = tmp_path / "graph.json"
    FileGraphPersistenceBackend(graph, staged_sessions=True).save_graph_data(
        {"nodes": [], "edges": [], "metadata": {}}
    )
    monkeypatch.setenv("SESSION_BACKUP_GRAPH", str(graph))
    output = tmp_path / "restored"
    with pytest.raises(OSError):
        with open_restore(Hook(), output) as (service, sessions, modes, publish):

            def fail(record):
                raise OSError()

            monkeypatch.setattr(sessions, "save", fail)
            restore_backup(service, sessions, modes, backup())
            publish()
    assert not output.exists()
    assert not list(tmp_path.glob("session-restore-*"))
