# covers: backend/service/session_backup.py
import base64
import hashlib

import pytest

from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    Session,
)
from backend.core.write_modes import WriteModes
from backend.runtime.authorization import GraphAuthorizationDecision
from backend.service.session_backup import BackupRefused, canonical_bytes, create_backup


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

    def authorize_capture_refs(self, nodes, edges):
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


def test_backup_keeps_persistent_state_and_checks_integrity(backend):
    session = Session("1234-5678", name="Map", description="Details", seq=7)
    payload = b"pixels"
    url = "data:image/webp;base64," + base64.b64encode(payload).decode()
    session.state.update(
        node_refs=["a"],
        positions={"a": {"x": 2, "y": 3}},
        hidden_edge_ids=["edge"],
        annotations=[{"id": "image", "url": url}],
        shape_revisions={"node:old": 2},
    )
    session.activity_log = [{"actor": "private"}]
    backend.save(session.to_dict())
    service = Service()
    backup = create_backup(service, backend, WriteModes(Layers()))
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
    assert service.references == [(["a", "old"], ["edge"])]
    assert backup["import_mappings"] == backup["import_reports"] == []
    digest = hashlib.sha256(payload).hexdigest()
    assert backup["assets"] == [{"sha256": digest, "data_url": url}]
    integrity = backup.pop("integrity")
    assert integrity == {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(canonical_bytes(backup)).hexdigest(),
    }
    stored["state"]["positions"]["a"]["x"] = 4
    assert integrity["sha256"] != hashlib.sha256(canonical_bytes(backup)).hexdigest()


@pytest.mark.parametrize(
    "failure", ["session", "references", "staged", "asset", "mode"]
)
def test_backup_refuses_the_whole_snapshot(backend, failure):
    first = Session("1234-5678")
    second = Session("1234-5679")
    if failure == "asset":
        second.state["annotations"] = [{"url": "data:image/webp;base64,%%%"}]
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
    ):
        session.state.update(annotations=[], shape_revisions={})
        session.state.update(state)
        backend.save(session.to_dict())
        with pytest.raises(BackupRefused):
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
