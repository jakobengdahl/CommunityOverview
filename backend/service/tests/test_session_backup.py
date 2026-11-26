# covers: backend/service/session_backup.py, backend/service/session_capture.py, backend/service/views.py, backend/service/service.py, scripts/backup_sessions.py, scripts/session_backup_file_snapshot.py
import base64
import hashlib
from contextlib import contextmanager

import pytest

from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    Session,
)
from backend.core.write_modes import WriteModes as BaseWriteModes
from backend.runtime.authorization import GraphAuthorizationDecision
from backend.service.session_backup import BackupRefused, canonical_bytes, create_backup


class WriteModes(BaseWriteModes):
    @contextmanager
    def backup_snapshot(self):
        yield self


class Hook:
    def __init__(self, denied=None):
        self.denied = denied

    def evaluate(self, context):
        return GraphAuthorizationDecision(allowed=context.session_id != self.denied)


class Service:
    def __init__(self, denied=None, refs_allowed=True):
        self.authorization_hook = Hook(denied)
        self.refs_allowed = refs_allowed
        self.references = []

    def authorize_capture_refs(self, nodes, edges, *, target):
        assert target == "backup_sessions"
        self.references.append((nodes, edges))
        return {"success": self.refs_allowed}


class Layers:
    def __init__(self, entries=()):
        self.entries = entries

    def staged_status(self, session_id):
        return "active"

    def load_layer(self, session_id):
        return self.entries


@pytest.fixture(params=["memory", "file"])
def backend(request, tmp_path):
    if request.param == "file":
        return FileSessionPersistenceBackend(tmp_path)
    return InMemorySessionPersistenceBackend()


def test_backup_keeps_persistent_state_and_checks_integrity(backend, monkeypatch):
    session = Session("1234-5678", name="Map", description="Details", seq=7)
    payload = b"pixels"
    url = "data:image/webp;base64," + base64.b64encode(payload).decode()
    legacy_payload = b"legacy pixels"
    legacy_url = "data:image/png;base64," + base64.b64encode(legacy_payload).decode()
    session.state.update(
        node_refs=["a"],
        positions={"a": {"x": 2, "y": 3}},
        hidden_edge_ids=["edge"],
        annotations=[
            {"id": "image", "type": "image", "image": {"url": url}},
            {"id": "legacy-image", "kind": "image", "image": {"url": legacy_url}},
            {"id": "note", "type": "text", "text": "data:image/ ordinary text"},
            {"id": "note2", "type": "text", "text": "data:image/png;base64,YQ=="},
        ],
        shape_revisions={"node:old": 2, "node:group-local": 1},
        shape_overrides={"group-local": "circle"},
    )
    session.activity_log = [{"actor": "private"}]
    backend.save(session.to_dict())

    def unexpected_save(*args, **kwargs):
        pytest.fail("backup saved a session")

    monkeypatch.setattr(backend, "save", unexpected_save)
    service = Service()
    backup = create_backup(service, backend, WriteModes(Layers()))
    assert backup["format_version"] == 1
    stored = backup["sessions"][0]
    assert (
        stored["id"],
        stored["name"],
        stored["description"],
        stored["revision"],
    ) == (session.id, session.name, session.description, session.seq)
    assert stored["state"] == session.state
    assert stored["write_mode"] == "staged"
    assert "activity_log" not in stored
    assert service.references == [(["a", "group-local", "old"], ["edge"])]
    assert backup["import_mappings"] == backup["import_reports"] == []
    expected_assets = [
        {"sha256": hashlib.sha256(data).hexdigest(), "data_url": data_url}
        for data, data_url in ((payload, url), (legacy_payload, legacy_url))
    ]
    assert backup["assets"] == sorted(
        expected_assets, key=lambda asset: asset["sha256"]
    )
    integrity = backup.pop("integrity")
    assert integrity == {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(canonical_bytes(backup)).hexdigest(),
    }
    stored["state"]["positions"]["a"]["x"] = 4
    assert integrity["sha256"] != hashlib.sha256(canonical_bytes(backup)).hexdigest()


@pytest.mark.parametrize("problem", ["record", "staged"])
def test_denied_session_is_authorized_before_record_or_layer_checks(
    backend, monkeypatch, problem
):
    session = Session("1234-5678")
    backend.save(session.to_dict())
    modes = WriteModes(Layers([{"id": "change"}]))
    if problem == "record":
        monkeypatch.setattr(backend, "load", lambda _: None)
    with pytest.raises(BackupRefused, match="^access denied$"):
        create_backup(Service(denied=session.id), backend, modes)


def test_backup_refuses_session_list_changes_before_reading_records(backend):
    first = Session("1234-5678")
    second = Session("1234-5679")
    backend.save(first.to_dict())

    class ChangingHook(Hook):
        def evaluate(self, context):
            if context.session_id == first.id:
                backend.save(second.to_dict())
            return GraphAuthorizationDecision(allowed=True)

    service = Service()
    service.authorization_hook = ChangingHook()
    with pytest.raises(BackupRefused, match="session list changed"):
        create_backup(service, backend, WriteModes())


@pytest.mark.parametrize(
    "failure", ["session", "references", "staged", "asset", "mode"]
)
def test_backup_refuses_the_whole_snapshot(backend, failure):
    first = Session("1234-5678")
    second = Session("1234-5679")
    if failure == "asset":
        second.state["annotations"] = [
            {"type": "image", "image": {"url": "data:image/webp;base64,%%%"}}
        ]
    if failure == "mode":
        second.write_mode = "staged"
    backend.save(first.to_dict())
    backend.save(second.to_dict())
    service = Service(
        denied=second.id if failure == "session" else None,
        refs_allowed=failure != "references",
    )
    modes = (
        WriteModes(Layers([{"id": "change"}])) if failure == "staged" else WriteModes()
    )
    with pytest.raises(BackupRefused):
        create_backup(service, backend, modes)


def test_graph_access_checks_nested_targets_and_shape_history(tmp_path):
    from backend.core import Node
    from backend.core.storage import GraphStorage
    from backend.runtime.authorization import GraphAccessNarrowing
    from backend.service import GraphService

    class NarrowHook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True,
                    allow_local_graph=False,
                    include_graph_ids=("allowed",),
                ),
            )

    storage = GraphStorage(str(tmp_path / "graph.json"))
    storage.add_nodes(
        [
            Node(
                id="group-secret",
                type="Actor",
                name="Private",
                metadata={"origin_graph_id": "other"},
            ),
            Node(
                id="secret",
                type="Actor",
                name="Private",
                metadata={"origin_graph_id": "other"},
            ),
        ],
        [],
    )
    service = GraphService(storage, authorization_hook=NarrowHook())
    backend = InMemorySessionPersistenceBackend()
    session = Session("1234-5678")
    for state in (
        {
            "annotations": [
                {
                    "id": "ann",
                    "type": "reference",
                    "target_kind": "resource",
                    "target": "secret",
                }
            ]
        },
        {"shape_revisions": {"node:secret": 1}},
        {"node_refs": ["group-secret"]},
        {"positions": {"group-secret": {"x": 1, "y": 1}}},
        {"shape_overrides": {"group-secret": "circle"}},
        {"shape_revisions": {"node:group-secret": 1}},
        {"annotations": [{"id": "group-secret", "member_node_ids": ["group-secret"]}]},
        {"annotations": [{"id": "ann", "member_node_ids": ["group-secret"]}]},
    ):
        session.state.update(
            annotations=[],
            shape_revisions={},
            node_refs=[],
            positions={},
            shape_overrides={},
        )
        session.state.update(state)
        backend.save(session.to_dict())
        with pytest.raises(BackupRefused):
            create_backup(service, backend, WriteModes())


def test_backup_denies_node_attachment_colliding_with_annotation(tmp_path):
    from backend.core import Node
    from backend.core.storage import GraphStorage
    from backend.runtime.authorization import GraphAccessNarrowing
    from backend.service import GraphService

    class NarrowHook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True,
                    allow_local_graph=False,
                    include_graph_ids=("allowed",),
                ),
            )

    storage = GraphStorage(str(tmp_path / "graph.json"))
    storage.add_nodes(
        [
            Node(
                id="secret",
                type="Actor",
                name="Private",
                metadata={"origin_graph_id": "other"},
            )
        ],
        [],
    )
    service = GraphService(storage, authorization_hook=NarrowHook())
    backend = InMemorySessionPersistenceBackend()
    session = Session("1234-5678")
    session.state["annotations"] = [
        {"id": "secret", "type": "text"},
        {
            "id": "note",
            "type": "text",
            "attachment": {"target_type": "node", "target_id": "secret"},
        },
    ]
    backend.save(session.to_dict())
    with pytest.raises(BackupRefused, match="^access denied$"):
        create_backup(service, backend, WriteModes())


def test_file_layer_is_checked_after_reopening(tmp_path):
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from backend.core.tests.layer_store_contract import entry

    graph = tmp_path / "graph.json"
    layer = FileGraphPersistenceBackend(graph, staged_sessions=True)
    layer.save_graph_data({"nodes": [], "edges": [], "metadata": {}})
    layer.record_staged("1234-5678")
    layer.stage("1234-5678", [entry("node", "a")])
    reopened = FileGraphPersistenceBackend(graph, staged_sessions=True)
    backend = FileSessionPersistenceBackend(tmp_path / "sessions")
    backend.save(Session("1234-5678").to_dict())
    with pytest.raises(BackupRefused):
        create_backup(Service(), backend, WriteModes(reopened))


def test_backup_uses_its_own_reference_scope(tmp_path):
    from backend.core import Node
    from backend.core.storage import GraphStorage
    from backend.runtime.authorization import GraphAccessNarrowing
    from backend.service import GraphService

    class TargetHook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=(
                        context.target == "backup_sessions" and bool(context.session_id)
                    ),
                    allow_local_graph=False,
                    include_graph_ids=("allowed",),
                ),
            )

    storage = GraphStorage(str(tmp_path / "graph.json"))
    storage.add_nodes(
        [
            Node(
                id="secret",
                type="Actor",
                name="Private",
                metadata={"origin_graph_id": "other"},
            )
        ],
        [],
    )
    service = GraphService(storage, authorization_hook=TargetHook())
    backend = InMemorySessionPersistenceBackend()
    session = Session("1234-5678")
    session.state["node_refs"] = ["secret"]
    backend.save(session.to_dict())
    assert service.authorize_capture_refs(["secret"], [])["success"]
    with pytest.raises(BackupRefused, match="access denied"):
        create_backup(service, backend, WriteModes())


def test_backup_requires_coordinated_layer_snapshot(backend):
    backend.save(Session("1234-5678").to_dict())
    with pytest.raises(BackupRefused, match="layer snapshot unavailable"):
        create_backup(Service(), backend, BaseWriteModes())


def test_command_authorizes_before_opening_stores(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from scripts import backup_sessions

    class Deny:
        def evaluate(self, context):
            return GraphAuthorizationDecision(allowed=context.action != "admin")

    def forbidden(*args):
        pytest.fail("stores opened before authorization")

    integration = SimpleNamespace(authorization_hook=Deny, open_backup=forbidden)
    monkeypatch.setattr(
        backup_sessions.importlib, "import_module", lambda _: integration
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup_sessions",
            "--integration",
            "deployment",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(BackupRefused, match="access denied"):
        backup_sessions.main()
    assert not (tmp_path / "out").exists()


def test_layer_write_is_ordered_with_snapshot(backend):
    import threading

    entered = threading.Event()
    attempted = threading.Event()
    finished = threading.Event()
    lock = threading.Lock()
    layer = Layers()

    class Coordinated(WriteModes):
        @contextmanager
        def backup_snapshot(self):
            with lock:
                entered.set()
                assert attempted.wait(3)
                yield self
                assert not finished.is_set()

    modes = Coordinated(layer)
    backend.save(Session("1234-5678").to_dict())

    def stage():
        assert entered.wait(3)
        attempted.set()
        with lock:
            layer.entries = [{"id": "change"}]
        finished.set()

    writer = threading.Thread(target=stage)
    writer.start()
    try:
        assert len(create_backup(Service(), backend, modes)["sessions"]) == 1
    finally:
        writer.join(3)
    assert finished.is_set()
    with pytest.raises(BackupRefused, match="unmerged staged changes"):
        create_backup(Service(), backend, modes)


def test_authorization_runs_outside_session_transaction(backend):
    import threading

    backend.save(Session("1234-5678").to_dict())

    class LockCheckingService(Service):
        def authorize_capture_refs(self, nodes, edges, *, target):
            done = threading.Event()

            def read():
                with backend.transaction():
                    done.set()

            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            assert done.wait(2), "session transaction held during authorization"
            thread.join()
            return super().authorize_capture_refs(nodes, edges, target=target)

    assert (
        len(create_backup(LockCheckingService(), backend, WriteModes())["sessions"])
        == 1
    )


@pytest.mark.parametrize("custom_journal", [False, True])
def test_file_snapshot_integration(tmp_path, monkeypatch, custom_journal):
    from backend.core import Node
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from backend.runtime.authorization import GraphAccessNarrowing
    from scripts import session_backup_file_snapshot as integration

    graph = tmp_path / "graph.json"
    journal = tmp_path / "custom" / "writes.ndjson" if custom_journal else None
    layer = FileGraphPersistenceBackend(
        graph, journal_path=journal, staged_sessions=True
    )
    layer.save_graph_data({"nodes": [], "edges": [], "metadata": {}})
    layer.record_staged("1234-5678")
    if custom_journal:
        journal.parent.mkdir()
        journal.touch()
    sessions = tmp_path / "sessions"
    FileSessionPersistenceBackend(sessions).save(Session("1234-5678").to_dict())
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setenv("SESSION_BACKUP_GRAPH", str(graph))
    monkeypatch.setenv("SESSION_BACKUP_SESSIONS", str(sessions))
    if custom_journal:
        monkeypatch.setenv("SESSION_BACKUP_GRAPH_JOURNAL", str(journal))
    with integration.open_backup(Hook()) as (service, backend, modes):
        backup = create_backup(service, backend, modes)
    assert backup["sessions"][0]["id"] == "1234-5678"
    assert {p: p.read_bytes() for p in before} == before
    session = Session("1234-5678")
    session.state["node_refs"] = ["secret"]
    FileSessionPersistenceBackend(sessions).save(session.to_dict())
    layer.upsert_node(
        Node(
            id="secret",
            type="Actor",
            name="Secret",
            metadata={"origin_graph_id": "other"},
        ).to_dict()
    )

    class NarrowHook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True,
                    allow_local_graph=False,
                    include_graph_ids=("allowed",),
                ),
            )

    with integration.open_backup(NarrowHook()) as (service, backend, modes):
        with pytest.raises(BackupRefused, match="^access denied$"):
            create_backup(service, backend, modes)
    original = layer.journal_path.read_bytes()
    for tail in (b"partial", b"malformed\n", b"{}\n"):
        layer.journal_path.write_bytes(original + tail)
        with pytest.raises(BackupRefused, match="incomplete snapshot journal"):
            with integration.open_backup(Hook()):
                pass
        assert layer.journal_path.read_bytes() == original + tail
    if custom_journal:
        layer.journal_path.unlink()
        with pytest.raises(BackupRefused, match="snapshot unavailable"):
            with integration.open_backup(Hook()):
                pass
