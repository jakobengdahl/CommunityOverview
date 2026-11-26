# covers: backend/service/session_restore.py, scripts/session_restore_file_snapshot.py, scripts/restore_sessions.py
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
    assert report == [
        {
            "session_id": "1234-5678",
            "missing_node_ids": ["missing"],
            "missing_edge_ids": [],
        }
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("positions", {"missing": {"x": "bad", "y": [1]}}),
        ("edge_intensity", "bad"),
        ("edge_intensity", 2),
        ("annotation_schema_version", 999),
        (
            "shape_overrides",
            {"missing": {"inline": {"base": "unknown", "scale": 1e300}}},
        ),
        ("type_shapes", {"Actor": {"arbitrary": "object"}}),
        ("shape_revisions", {"node:missing": "bad"}),
        ("shape_operation_ids", {"receipt": "bad"}),
    ],
)
def test_invalid_state_values_refuse_the_entire_restore(field, value):
    document = backup()
    document["sessions"][0]["state"][field] = value
    sign(document)
    target = InMemorySessionPersistenceBackend()
    with pytest.raises(RestoreRefused):
        restore_backup(Service(), target, Modes(), document)
    assert target.list_ids() == []


def test_staged_mirror_with_direct_effective_mode_is_refused():
    document = backup()
    document["sessions"][0]["write_mode_mirror"]["write_mode"] = "staged"
    sign(document)
    target = InMemorySessionPersistenceBackend()
    with pytest.raises(RestoreRefused, match="invalid mode"):
        restore_backup(Service(), target, Modes(), document)
    assert target.list_ids() == []


def test_limits_precede_reference_authorization_and_file_read(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from scripts import restore_sessions

    service = Service()
    calls = []
    original_evaluate = service.authorization_hook.evaluate

    def evaluate(context):
        calls.append(context)
        return original_evaluate(context)

    service.authorization_hook.evaluate = evaluate
    document = backup()
    for kwargs in ({"max_sessions": 0}, {"max_annotations": 0}):
        document["sessions"][0]["state"]["annotations"] = [{}]
        calls.clear()
        with pytest.raises(RestoreRefused):
            restore_backup(
                service,
                InMemorySessionPersistenceBackend(),
                Modes(),
                document,
                **kwargs,
            )
        assert len(calls) == 1  # Admin only; no session or reference checks.

    from backend.service import session_restore

    monkeypatch.setattr(session_restore, "DEFAULT_MAX_SESSION_DOCUMENT_BYTES", 10)
    calls.clear()
    with pytest.raises(RestoreRefused):
        restore_backup(service, InMemorySessionPersistenceBackend(), Modes(), document)
    assert len(calls) == 1

    source = tmp_path / "backup.json"
    source.write_bytes(b"x" * 11)
    monkeypatch.setattr(restore_sessions, "MAX_BACKUP_BYTES", 10)
    parsed = []
    monkeypatch.setattr(
        restore_sessions.json, "loads", lambda data: parsed.append(data)
    )
    monkeypatch.setattr(
        restore_sessions.importlib,
        "import_module",
        lambda _: SimpleNamespace(
            authorization_hook=lambda: service.authorization_hook
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "restore_sessions",
            "--integration",
            "test",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(RestoreRefused):
        restore_sessions.main()
    assert not parsed


@pytest.mark.parametrize(
    "damage",
    [
        "version",
        "missing_version",
        "integrity",
        "duplicate",
        "unsafe",
        "limit",
        "assets",
        "node_type",
        "edge_type",
        "positions_type",
    ],
)
def test_invalid_backup_never_changes_target(damage):
    document = backup()
    if damage == "version":
        document["format_version"] = 2
    elif damage == "missing_version":
        del document["format_version"]
    elif damage in ("node_type", "edge_type", "positions_type"):
        key = {
            "node_type": "node_refs",
            "edge_type": "hidden_edge_ids",
            "positions_type": "positions",
        }[damage]
        document["sessions"][0]["state"][key] = [123]
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
    with pytest.raises(RestoreRefused) as error:
        restore_backup(
            Service(),
            target,
            Modes(),
            document,
            max_sessions=0 if damage == "limit" else 10000,
        )
    if damage == "missing_version":
        assert str(error.value) == "unsupported version"
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


@pytest.mark.parametrize("failure", ["admin", "input", "reference"])
def test_command_conceals_pre_authorization_failures(tmp_path, monkeypatch, failure):
    import json
    import sys
    from types import SimpleNamespace
    from scripts import restore_sessions

    service = Service()
    service.authorization_hook.allowed = failure != "admin"
    service.allowed = failure != "reference"
    source = tmp_path / "backup.json"
    if failure != "input":
        source.write_text(json.dumps(backup()))
    opened = []

    @contextmanager
    def open_restore(hook, output):
        opened.append(True)
        yield service, InMemorySessionPersistenceBackend(), Modes(), lambda: None

    integration = SimpleNamespace(
        authorization_hook=lambda: service.authorization_hook, open_restore=open_restore
    )
    monkeypatch.setattr(
        restore_sessions.importlib, "import_module", lambda name: integration
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "restore_sessions",
            "--integration",
            "test",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(RestoreRefused, match="^access denied$"):
        restore_sessions.main()
    assert bool(opened) == (failure == "reference")


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
    document["sessions"][0]["state"]["hidden_edge_ids"] = ["missing-edge"]
    if mode == "staged":
        FileGraphPersistenceBackend(graph, staged_sessions=True).record_staged(
            "1234-5678"
        )
    sign(document)
    with open_restore(hook, output) as (service, sessions, modes, publish):
        report = restore_backup(service, sessions, modes, document)
        assert not output.exists()
        publish()
    assert report[0]["missing_node_ids"] == ["missing"]
    assert report[0]["missing_edge_ids"] == ["missing-edge"]
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


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "invalid",
        "duplicate",
        "profile",
        "integrity",
        "denied",
        "tombstone_invalid",
        "tombstone_profile",
        "tombstone_duplicate",
        "tombstone_denied",
        "tombstone_integrity",
    ],
)
def test_templates_restore_atomically_with_sessions(damage):
    from backend.core.tests.test_template_store import document as template_document

    service = Service()
    source = InMemorySessionPersistenceBackend()
    doc = template_document()
    source.save({"id": doc["template_id"], "document": doc})
    tombstone = {"id": "9999-9999", "deleted": True, "revision": 2}
    source.save(tombstone)
    document = create_backup(
        service, InMemorySessionPersistenceBackend(), Modes(), template_backend=source
    )
    document["sessions"] = backup()["sessions"]
    assert document["template_tombstones"] == [tombstone]
    profiles = set()
    if damage == "tombstone_invalid":
        document["template_tombstones"][0]["revision"] = True
    elif damage == "tombstone_profile":
        profiles.add(tombstone["id"])
    elif damage == "tombstone_duplicate":
        document["template_tombstones"].append(copy.deepcopy(tombstone))
    elif damage == "invalid":
        document["templates"][0]["revision"] = -1
    elif damage == "duplicate":
        document["templates"].append(copy.deepcopy(doc))
    elif damage == "profile":
        profiles.add(doc["template_id"])
    if damage == "tombstone_integrity":
        document["template_tombstones"][0]["revision"] += 1
    if damage not in ("integrity", "tombstone_integrity"):
        sign(document)
    if damage in ("denied", "tombstone_denied"):
        original = service.authorization_hook.evaluate
        service.authorization_hook.evaluate = lambda context: (
            GraphAuthorizationDecision(allowed=False)
            if context.session_id
            == (tombstone["id"] if damage == "tombstone_denied" else doc["template_id"])
            else original(context)
        )
    sessions, templates = (
        InMemorySessionPersistenceBackend(),
        InMemorySessionPersistenceBackend(),
    )
    if damage:
        with pytest.raises(RestoreRefused):
            restore_backup(
                service,
                sessions,
                Modes(),
                document,
                template_backend=templates,
                profile_template_ids=profiles,
            )
        assert not sessions.list_ids() and not templates.list_ids()
    else:
        restore_backup(
            service,
            sessions,
            Modes(),
            document,
            template_backend=templates,
            profile_template_ids=profiles,
        )
        assert sessions.load("1234-5678") is not None
        assert templates.load(doc["template_id"])["document"] == doc
        assert templates.load(tombstone["id"]) == tombstone
        from backend.core.template_store import TemplateError, TemplateStore

        replacement = copy.deepcopy(doc)
        replacement["template_id"] = tombstone["id"]
        with pytest.raises(TemplateError, match="deleted"):
            TemplateStore(templates).save(replacement, expected_revision=0)


@pytest.mark.parametrize("kind", ["template", "tombstone"])
@pytest.mark.parametrize("collision", ["restored", "active", "retired"])
def test_template_ids_cannot_collide_with_session_namespace(kind, collision):
    from backend.core.tests.test_template_store import document as template_document

    doc = template_document()
    template_id = doc["template_id"]
    source = InMemorySessionPersistenceBackend()
    source.save(
        {"id": template_id, "document": doc}
        if kind == "template"
        else {"id": template_id, "deleted": True, "revision": 1}
    )
    document = create_backup(
        Service(), InMemorySessionPersistenceBackend(), Modes(), template_backend=source
    )
    if collision == "restored":
        document["sessions"] = backup()["sessions"]
        document["sessions"][0]["id"] = template_id
        sign(document)

    class CollisionModes(Modes):
        def staged_status(self, session_id):
            if session_id == template_id and collision != "restored":
                return collision
            return "none"

    sessions, templates = (
        InMemorySessionPersistenceBackend(),
        InMemorySessionPersistenceBackend(),
    )
    with pytest.raises(RestoreRefused, match="template collision"):
        restore_backup(
            Service(), sessions, CollisionModes(), document, template_backend=templates
        )
    assert not sessions.list_ids() and not templates.list_ids()


def test_file_workspace_restores_templates_and_rejects_profile_collision(
    tmp_path, monkeypatch
):
    import json
    from backend.core.session_store import FileSessionPersistenceBackend
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from backend.core.tests.test_template_store import document as template_document
    from scripts.session_restore_file_snapshot import open_restore

    graph = tmp_path / "graph.json"
    FileGraphPersistenceBackend(graph, staged_sessions=True).save_graph_data(
        {"nodes": [], "edges": [], "metadata": {}}
    )
    monkeypatch.setenv("SESSION_BACKUP_GRAPH", str(graph))
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("SCHEMA_FILE", str(profile / "schema_config.json"))
    doc = template_document()
    source = InMemorySessionPersistenceBackend()
    source.save({"id": doc["template_id"], "document": doc})
    tombstone = {"id": "9999-9999", "deleted": True, "revision": 2}
    source.save(tombstone)
    document = create_backup(
        Service(), InMemorySessionPersistenceBackend(), Modes(), template_backend=source
    )
    profile_templates = profile / "templates"
    profile_templates.mkdir()
    (profile_templates / "example.json").write_text(json.dumps(doc))
    output = tmp_path / "restored"
    with open_restore(Hook(), output) as (service, sessions, modes, publish):
        with pytest.raises(RestoreRefused):
            restore_backup(
                service,
                sessions,
                modes,
                document,
                template_backend=service.backup_template_backend,
                profile_template_ids=service.backup_profile_template_ids,
            )
    assert not output.exists()
    (profile_templates / "example.json").unlink()
    with open_restore(Hook(), output) as (service, sessions, modes, publish):
        restore_backup(
            service,
            sessions,
            modes,
            document,
            template_backend=service.backup_template_backend,
            profile_template_ids=service.backup_profile_template_ids,
        )
        publish()
    restored = FileSessionPersistenceBackend(output / "sessions" / "templates")
    assert restored.load(doc["template_id"])["document"] == doc
    assert restored.load(tombstone["id"]) == tombstone


def test_command_restores_version_one_with_legacy_integration(tmp_path, monkeypatch):
    import json
    import sys
    from types import SimpleNamespace
    from scripts import restore_sessions

    service = Service()
    target = InMemorySessionPersistenceBackend()
    published = []
    source = tmp_path / "backup.json"
    source.write_text(json.dumps(backup()))

    @contextmanager
    def open_restore(hook, output):
        yield service, target, Modes(), lambda: published.append(True)

    integration = SimpleNamespace(
        authorization_hook=lambda: service.authorization_hook, open_restore=open_restore
    )
    monkeypatch.setattr(
        restore_sessions.importlib, "import_module", lambda _: integration
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "restore_sessions",
            "--integration",
            "test",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "out"),
        ],
    )
    restore_sessions.main()
    assert published == [True]
    assert target.load("1234-5678") is not None
