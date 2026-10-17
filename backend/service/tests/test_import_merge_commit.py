"""
REST and service tests for the merge import commit (``POST /import/merge``)
of docs/adr/0013-graph-import-merge-mode.md, slice 2.

The commit is bound to the previewed plan by its digest; it backs the graph
up, then recomputes the plan under the storage lock and applies it
all-or-nothing. The window cases below land a live write after the
commit's first plan, as the backup is written, so only the locked plan can
see it.
"""

import json
import os
import uuid
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.execution import ExecutionState, InMemoryExecutionStore
from backend.core import (
    BackendCapabilities,
    Edge,
    GraphStorage,
    GraphWriteFailed,
    GraphWriteOutcomeUnknown,
    Node,
)
from backend.core.events.models import EventType
from backend.core.tests.persistence_contract import InMemoryGraphPersistenceBackend
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    GraphAccessNarrowing,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
)
from backend.service import GraphService, create_rest_router, graph_merge_plan
from backend.service import import_service

URL = "/api/graph/import/merge"
PREVIEW_URL = "/api/graph/import/merge/preview"


class _Hook:
    """Decides every action alike unless ``read`` overrides the read action."""

    def __init__(self, *, allowed=True, narrowed=False, read=None):
        self.allowed = allowed
        self.narrowed = narrowed
        self.read = read  # None, or (allowed, narrowed) for GRAPH_ACTION_READ
        self.actions = []

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        self.actions.append(context.action)
        allowed, narrowed = self.allowed, self.narrowed
        if self.read is not None and context.action == GRAPH_ACTION_READ:
            allowed, narrowed = self.read
        return GraphAuthorizationDecision(
            allowed=allowed,
            mode="test",
            source="test",
            reason=None if allowed else "nope",
            graph_access=GraphAccessNarrowing(
                enabled=narrowed,
                allow_local_graph=False,
                include_graph_ids=("graph-a",) if narrowed else (),
            ),
        )


@pytest.fixture
def storage(temp_dir) -> GraphStorage:
    store = GraphStorage(json_path=os.path.join(temp_dir, "graph.json"))
    store.add_nodes(
        [
            Node(id="a1", type="Actor", name="Alice"),
            Node(id="a2", type="Actor", name="Bob"),
            Node(id="i1", type="Initiative", name="Project X"),
        ],
        [Edge(id="e1", source="a1", target="i1", type="BELONGS_TO")],
    )
    yield store
    store.flush()
    store.shutdown_events()


@pytest.fixture
def job_store() -> InMemoryExecutionStore:
    return InMemoryExecutionStore()


@pytest.fixture(autouse=True)
def _no_embedding_drain(monkeypatch):
    # The drain thread would race the assertions on the job store.
    monkeypatch.setattr(import_service, "_start_embedding_drain", lambda *a: None)


def _client(storage, job_store, hook=None):
    service = GraphService(storage, authorization_hook=hook)
    router = create_rest_router(service, import_job_store=job_store)
    app = FastAPI()
    app.include_router(router, prefix="/api/graph")
    return TestClient(app)


def _document(**extra) -> Dict[str, Any]:
    document = {
        "nodes": [
            {"id": "a1", "type": "Actor", "name": "Alice B"},
            {"id": "n1", "type": "Actor", "name": "New"},
        ],
        "edges": [{"id": "e2", "source": "n1", "target": "i1", "type": "BELONGS_TO"}],
    }
    document.update(extra)
    return document


def _body(document=None, **parameters) -> Dict[str, Any]:
    parameters.setdefault("policy", {"default": "overwrite"})
    return {"document": document or _document(), **parameters}


def _preview(client, body) -> Dict[str, Any]:
    response = client.post(PREVIEW_URL, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client, body, **headers):
    digest = _preview(client, body)["plan_digest"]
    return client.post(URL, json={**body, "plan_digest": digest}, headers=headers)


def _state(storage) -> Dict[str, Any]:
    return {
        "nodes": {k: v.to_dict() for k, v in storage.nodes.items()},
        "edges": {k: v.to_dict() for k, v in storage.edges.items()},
    }


def _events(storage) -> List[Any]:
    seen: List[Any] = []
    storage.add_system_listener(seen.append)
    return seen


def _backups(storage) -> List[str]:
    directory = storage.json_path.parent / import_service.BACKUP_SUBDIR
    return sorted(os.listdir(directory)) if directory.exists() else []


class TestCommit:
    def test_a_previewed_plan_is_applied_and_reported(self, storage, job_store):
        client = _client(storage, job_store)
        response = _commit(client, _body())
        assert response.status_code == 200, response.text
        result = response.json()

        assert storage.get_node("a1").name == "Alice B"
        assert storage.get_node("n1").name == "New"
        assert storage.graph.has_edge("n1", "i1", key="e2")
        assert result["success"] is True
        assert result["counts"] == {
            "nodes_created": 1,
            "nodes_overwritten": 1,
            "nodes_renamed": 0,
            "edges_created": 1,
            "edges_overwritten": 0,
            "edges_renamed": 0,
            "events": 3,
        }
        assert result["plan"]["committable"] is True
        assert result["import_id"].startswith("merge-")
        assert "replace" in result["backup_note"]

    def test_the_backup_is_the_graph_before_the_merge(self, storage, job_store):
        before = {n.id: n.name for n in storage.get_all_nodes()}
        result = _commit(_client(storage, job_store), _body()).json()

        with open(result["backup_path"], encoding="utf-8") as fh:
            backup = json.load(fh)
        assert {n["id"]: n["name"] for n in backup["nodes"]} == before
        assert {e["id"] for e in backup["edges"]} == {"e1"}

    def test_live_edges_absent_from_the_document_stay(self, storage, job_store):
        _commit(_client(storage, job_store), _body())
        assert "e1" in storage.edges

    def test_the_generation_is_not_bumped(self, storage, job_store):
        generation = storage.generation
        _commit(_client(storage, job_store), _body())
        assert storage.generation == generation

    def test_a_rename_mints_a_fresh_id_and_rewrites_the_documents_edges(
        self, storage, job_store
    ):
        document = {
            "nodes": [{"id": "a1", "type": "Actor", "name": "Other Alice"}],
            "edges": [
                {"id": "e1", "source": "a1", "target": "i1", "type": "BELONGS_TO"}
            ],
        }
        body = _body(document, policy={"default": "rename"})
        live_alice = storage.nodes["a1"].to_dict()
        result = _commit(_client(storage, job_store), body).json()

        (node_pair,) = result["renamed"]["nodes"]["items"]
        (edge_pair,) = result["renamed"]["edges"]["items"]
        assert node_pair["old_id"] == "a1" and edge_pair["old_id"] == "e1"
        new_node, new_edge = node_pair["new_id"], edge_pair["new_id"]
        uuid.UUID(new_node), uuid.UUID(new_edge)
        assert storage.nodes[new_node].name == "Other Alice"
        assert storage.edges[new_edge].source == new_node
        assert storage.nodes["a1"].to_dict() == live_alice
        assert storage.edges["e1"].source == "a1"

    def test_the_documents_revision_is_never_taken(self, storage, job_store):
        document = _document()
        document["nodes"][0]["revision"] = 999_999
        document["nodes"][1]["revision"] = 999_999
        _commit(_client(storage, job_store), _body(document))
        assert storage.nodes["a1"].revision < 999_999
        assert storage.nodes["n1"].revision < 999_999

    def test_an_edge_overwrite_retypes_and_announces_an_edge_update(
        self, storage, job_store
    ):
        document = {
            "nodes": [],
            "edges": [
                {"id": "e1", "source": "a1", "target": "i1", "type": "RELATES_TO"}
            ],
        }
        seen = _events(storage)
        response = _commit(_client(storage, job_store), _body(document))
        assert response.status_code == 200, response.text
        assert storage.edges["e1"].type_str == "RELATES_TO"
        assert [(e.event_type, e.entity.id) for e in seen] == [
            (EventType.EDGE_UPDATE, "e1")
        ]


class TestEvents:
    def test_events_take_the_callers_origin_and_correlation(self, storage, job_store):
        seen = _events(storage)
        _commit(
            _client(storage, job_store),
            _body(),
            **{"x-event-origin": "agent:importer", "x-correlation-id": "corr-7"},
        )
        assert [(e.event_type, e.entity.id) for e in seen] == [
            (EventType.NODE_CREATE, "n1"),
            (EventType.NODE_UPDATE, "a1"),
            (EventType.EDGE_CREATE, "e2"),
        ]
        assert {e.origin.event_origin for e in seen} == {"agent:importer"}
        assert {e.origin.event_correlation_id for e in seen} == {"corr-7"}

    def test_events_default_to_the_merge_origin_and_the_import_id(
        self, storage, job_store
    ):
        seen = _events(storage)
        result = _commit(_client(storage, job_store), _body()).json()
        assert {e.origin.event_origin for e in seen} == {"graph-import-merge"}
        assert {e.origin.event_correlation_id for e in seen} == {result["import_id"]}

    def test_a_document_cannot_supply_the_event_origin(self, storage, job_store):
        body = {**_body(), "event_origin": "agent:spoofed"}
        response = _client(storage, job_store).post(URL, json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "invalid_parameters"


class TestEmbeddingsAndAudit:
    def test_changed_text_queues_an_embeddings_job_at_the_live_generation(
        self, storage, job_store
    ):
        result = _commit(_client(storage, job_store), _body()).json()
        assert result["embeddings_status"] == "queued"
        job = job_store.get(result["job_id"])
        assert job.state == ExecutionState.PENDING
        assert job.payload["graph_generation"] == storage.generation
        assert job.payload["node_count"] == 2
        assert job.payload["import_id"] == result["import_id"]

    def test_the_merged_nodes_lose_their_vectors_until_the_job_runs(
        self, storage, job_store
    ):
        storage.vector_store.absorb_embeddings(
            {"a1": [1.0, 0.0], "a2": [0.0, 1.0], "i1": [1.0, 1.0]}
        )
        _commit(_client(storage, job_store), _body())
        assert not storage.vector_store.has_embedding("a1")
        assert storage.vector_store.has_embedding("a2")
        assert storage.vector_store.has_embedding("i1")

    def test_a_metadata_only_merge_needs_no_embeddings_job(self, storage, job_store):
        document = {
            "nodes": [
                {"id": "a1", "type": "Actor", "name": "Alice", "metadata": {"k": 1}}
            ],
            "edges": [],
        }
        result = _commit(_client(storage, job_store), _body(document)).json()
        assert result["embeddings_status"] == "not_needed"
        assert result["job_id"] is None

    def test_a_job_that_cannot_start_leaves_the_merge_committed(
        self, storage, job_store, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("queue down")

        monkeypatch.setattr(import_service, "_enqueue_embedding_job", _boom)
        response = _commit(_client(storage, job_store), _body())
        assert response.status_code == 200
        assert response.json()["embeddings_status"] == "not_started"
        assert storage.get_node("a1").name == "Alice B"

    def test_the_audit_record_carries_every_outcome_and_revision(
        self, storage, job_store
    ):
        client = _client(storage, job_store)
        result = _commit(client, _body()).json()

        record = job_store.get(result["import_id"])
        assert record.state == ExecutionState.SUCCEEDED
        payload = record.payload
        assert payload["plan_digest"] == result["plan_digest"]
        assert payload["parameters"]["policy"]["default"] == "overwrite"
        assert payload["backup_path"] == result["backup_path"]
        assert payload["embeddings_job_id"] == result["job_id"]
        assert payload["counts"] == result["counts"]
        nodes = payload["outcomes"]["nodes"]
        assert nodes["created"] == [["n1", storage.nodes["n1"].revision]]
        assert nodes["overwritten"] == [["a1", storage.nodes["a1"].revision]]
        assert payload["outcomes"]["edges"]["created"] == [
            ["e2", storage.edges["e2"].revision]
        ]

        rows = client.get("/api/graph/import").json()
        merge_row = next(r for r in rows if r["id"] == result["import_id"])
        assert merge_row["mode"] == "merge"
        assert merge_row["plan_digest"] == result["plan_digest"]
        assert merge_row["embeddings_status"] is None
        job_row = next(r for r in rows if r["id"] == result["job_id"])
        assert job_row["mode"] == "merge"
        assert job_row["import_id"] == result["import_id"]
        assert client.get(f"/api/graph/import/{result['import_id']}").status_code == 200

    def test_a_replace_row_keeps_its_mode(self, storage, job_store):
        client = _client(storage, job_store)
        client.post("/api/graph/import", json={"nodes": [], "edges": []})
        (row,) = client.get("/api/graph/import").json()
        assert row["mode"] == "replace"
        assert "plan_digest" not in row

    def test_an_audit_record_that_cannot_be_written_is_reported_not_fatal(
        self, storage, job_store, monkeypatch
    ):
        real = job_store.enqueue

        def _enqueue(job):
            if job.payload.get("phase") == "merge":
                raise RuntimeError("store down")
            return real(job)

        monkeypatch.setattr(job_store, "enqueue", _enqueue)
        result = _commit(_client(storage, job_store), _body()).json()
        assert result["success"] is True
        assert result["audit_recorded"] is False


class TestBinding:
    def test_an_overwrite_without_a_digest_is_refused(self, storage, job_store):
        before = _state(storage)
        response = _client(storage, job_store).post(URL, json=_body())
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "plan_digest_required"
        assert _state(storage) == before
        assert _backups(storage) == []

    @pytest.mark.parametrize(
        "parameters",
        [
            {"policy": {"default": "rename"}},
            {"policy": {"default": "skip", "by_type": {"Actor": "overwrite"}}},
            {"policy": {"default": "skip"}, "include_system_types": True},
        ],
    )
    def test_a_digest_is_required_unless_the_merge_only_adds(
        self, storage, job_store, parameters
    ):
        response = _client(storage, job_store).post(
            URL, json={"document": _document(), **parameters}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "plan_digest_required"

    def test_a_skip_only_merge_may_omit_the_digest(self, storage, job_store):
        body = _body(policy={"default": "skip"})
        response = _client(storage, job_store).post(URL, json=body)
        assert response.status_code == 200, response.text
        assert storage.get_node("a1").name == "Alice"
        assert storage.get_node("n1").name == "New"

    def test_a_stale_digest_changes_nothing_and_returns_the_current_plan(
        self, storage, job_store
    ):
        client = _client(storage, job_store)
        body = _body()
        digest = _preview(client, body)["plan_digest"]
        storage.update_node("a1", {"description": "edited after the preview"})
        before = _state(storage)
        seen = _events(storage)

        response = client.post(URL, json={**body, "plan_digest": digest})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error_code"] == "import_plan_stale"
        assert detail["plan_digest"] != digest
        assert detail["plan"]["overwrites"]["nodes"]["count"] == 1
        assert _state(storage) == before
        assert seen == []
        assert _backups(storage) == []

    def test_a_digest_for_another_request_is_stale(self, storage, job_store):
        client = _client(storage, job_store)
        digest = _preview(client, _body())["plan_digest"]
        other = _body(policy={"default": "overwrite", "by_type": {"Actor": "skip"}})
        response = client.post(URL, json={**other, "plan_digest": digest})
        assert response.status_code == 409

    def test_a_malformed_digest_is_refused(self, storage, job_store):
        response = _client(storage, job_store).post(
            URL, json={**_body(), "plan_digest": ["sha256:x"]}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "invalid_parameters"

    def test_a_blocked_plan_is_refused_before_any_backup(self, storage, job_store):
        body = _body(policy={"default": "fail"})
        client = _client(storage, job_store)
        digest = _preview(client, body)["plan_digest"]
        before = _state(storage)
        response = client.post(URL, json={**body, "plan_digest": digest})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error_code"] == "import_merge_blocked"
        assert detail["plan"]["conflicts"]["count"] == 1
        assert _state(storage) == before
        assert _backups(storage) == []

    def test_a_retried_commit_is_stale_not_reapplied(self, storage, job_store):
        client = _client(storage, job_store)
        body = _body()
        digest = _preview(client, body)["plan_digest"]
        assert client.post(URL, json={**body, "plan_digest": digest}).status_code == 200
        again = client.post(URL, json={**body, "plan_digest": digest})
        assert again.status_code == 409


def _land_during_backup(monkeypatch, write):
    """Run ``write`` after the commit's first plan, as its backup is written:
    only the plan recomputed under the storage lock can see it."""
    real = import_service._write_backup

    def _backup(storage):
        write(storage)
        return real(storage)

    monkeypatch.setattr(import_service, "_write_backup", _backup)


class TestWindowBetweenPlanAndLock:
    """Every live read the plan relied on is checked under the lock."""

    def _assert_stale(self, storage, job_store, body, monkeypatch, write):
        client = _client(storage, job_store)
        digest = _preview(client, body)["plan_digest"]
        _land_during_backup(monkeypatch, write)
        holder: Dict[str, Any] = {}
        real_apply = storage.apply_import_merge

        def _apply(prepare, event_context=None):
            holder["before"] = _state(storage)
            holder["events"] = _events(storage)
            return real_apply(prepare, event_context=event_context)

        monkeypatch.setattr(storage, "apply_import_merge", _apply)
        response = client.post(URL, json={**body, "plan_digest": digest})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error_code"] == "import_plan_stale"
        assert _state(storage) == holder["before"]
        assert holder["events"] == []
        assert job_store.list_jobs() == []

    def test_an_overwrite_target_edited_in_the_window(
        self, storage, job_store, monkeypatch
    ):
        self._assert_stale(
            storage,
            job_store,
            _body(),
            monkeypatch,
            lambda s: s.update_node("a1", {"description": "late edit"}),
        )

    def test_a_create_id_taken_in_the_window(self, storage, job_store, monkeypatch):
        self._assert_stale(
            storage,
            job_store,
            _body(),
            monkeypatch,
            lambda s: s.add_nodes([Node(id="n1", type="Actor", name="Late")], []),
        )

    def test_a_kept_live_node_deleted_in_the_window(
        self, storage, job_store, monkeypatch
    ):
        body = _body(policy={"default": "skip"})
        self._assert_stale(
            storage,
            job_store,
            body,
            monkeypatch,
            lambda s: s.delete_nodes(["a1"], confirmed=True),
        )

    def test_an_attach_endpoint_edited_in_the_window(
        self, storage, job_store, monkeypatch
    ):
        # e2 attaches to the live i1; its subtypes decided applicability.
        self._assert_stale(
            storage,
            job_store,
            _body(),
            monkeypatch,
            lambda s: s.update_node("i1", {"subtypes": ["Programme"]}),
        )

    def test_a_duplicate_triple_created_in_the_window(
        self, storage, job_store, monkeypatch
    ):
        # The plan found no live a2 -> i1 BELONGS_TO and so creates e3; one
        # appearing in the window would make e3 a duplicate.
        document = {
            "nodes": [],
            "edges": [
                {"id": "e3", "source": "a2", "target": "i1", "type": "BELONGS_TO"}
            ],
        }
        self._assert_stale(
            storage,
            job_store,
            _body(document),
            monkeypatch,
            lambda s: s.add_edge(
                Edge(id="late", source="a2", target="i1", type="BELONGS_TO")
            ),
        )

    def test_the_live_edge_a_duplicate_deferred_to_deleted_in_the_window(
        self, storage, job_store, monkeypatch
    ):
        document = {
            "nodes": [],
            "edges": [
                {"id": "dup", "source": "a1", "target": "i1", "type": "BELONGS_TO"}
            ],
        }
        self._assert_stale(
            storage,
            job_store,
            _body(document),
            monkeypatch,
            lambda s: s.delete_edge("e1"),
        )

    def test_an_incident_edge_added_to_a_rejudged_node_in_the_window(
        self, storage, job_store, monkeypatch
    ):
        document = {
            "nodes": [
                {"id": "a1", "type": "Actor", "name": "Alice", "subtypes": ["Agency"]}
            ],
            "edges": [],
        }
        self._assert_stale(
            storage,
            job_store,
            _body(document),
            monkeypatch,
            lambda s: s.add_edge(
                Edge(id="late", source="a1", target="a2", type="RELATES_TO")
            ),
        )

    def test_a_replace_in_the_window(self, storage, job_store, monkeypatch):
        def _replace(s):
            s.replace_all_nodes_and_edges(
                [Node.from_dict(n.to_dict()) for n in s.get_all_nodes()],
                [Edge.from_dict(e.to_dict()) for e in s.get_all_edges()],
            )

        self._assert_stale(storage, job_store, _body(), monkeypatch, _replace)


class TestAccess:
    @pytest.mark.parametrize(
        "hook, status",
        [
            (_Hook(allowed=False), 403),
            (_Hook(read=(False, False)), 403),
            (_Hook(narrowed=True), 409),
            (_Hook(read=(True, True)), 409),
        ],
    )
    def test_a_caller_without_full_mutate_and_read_writes_nothing(
        self, storage, job_store, hook, status
    ):
        before = _state(storage)
        # Refused before the body is even parsed: a malformed one too.
        for body in (_body(), {"nonsense": True}):
            response = _client(storage, job_store, hook).post(URL, json=body)
            assert response.status_code == status
        assert _state(storage) == before
        assert _backups(storage) == []
        assert job_store.list_jobs() == []
        assert GRAPH_ACTION_MUTATE in hook.actions

    def test_the_commit_checks_mutate_then_read(self, storage, job_store):
        hook = _Hook()
        body = _body(policy={"default": "skip"})
        assert _client(storage, job_store, hook).post(URL, json=body).status_code == 200
        assert hook.actions[:2] == [GRAPH_ACTION_MUTATE, GRAPH_ACTION_READ]


class _EnforcingBackend(InMemoryGraphPersistenceBackend):
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            change_notification=True,
            revision_enforcement=True,
        )

    def read_write_evidence(self, keys):
        return {}


class TestRefusals:
    def test_a_store_that_assigns_revisions_is_refused_before_any_backup(
        self, job_store
    ):
        shared = GraphStorage(persistence_backend=_EnforcingBackend({}))
        response = _client(shared, job_store).post(
            URL, json=_body(policy={"default": "skip"})
        )
        assert response.status_code == 501
        assert response.json()["detail"]["error_code"] == "import_merge_not_supported"
        assert shared.nodes == {}
        assert job_store.list_jobs() == []
        shared.shutdown_events()

    def test_a_backup_that_cannot_be_written_refuses_the_merge(
        self, storage, job_store, monkeypatch
    ):
        monkeypatch.setattr(import_service, "_write_backup", lambda s: None)
        before = _state(storage)
        client = _client(storage, job_store)
        body = _body()
        digest = _preview(client, body)["plan_digest"]
        response = client.post(URL, json={**body, "plan_digest": digest})
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "backup_failed"
        assert _state(storage) == before

    @pytest.mark.parametrize(
        "error, status",
        [
            (GraphWriteFailed("did not land"), 502),
            (GraphWriteOutcomeUnknown("unknown"), 503),
        ],
    )
    def test_a_store_failure_answers_with_its_code_and_the_backup(
        self, storage, job_store, monkeypatch, error, status
    ):
        def _fail(prepare, event_context=None):
            raise error

        monkeypatch.setattr(storage, "apply_import_merge", _fail)
        response = _commit(_client(storage, job_store), _body())
        assert response.status_code == status
        detail = response.json()["detail"]
        assert detail["error_code"] == error.code
        assert os.path.exists(detail["backup_path"])
        assert job_store.list_jobs() == []

    def test_one_commit_at_a_time(self, storage, job_store):
        assert import_service._COMMIT_SLOT.acquire(blocking=False)
        try:
            response = _client(storage, job_store).post(
                URL, json=_body(policy={"default": "skip"})
            )
        finally:
            import_service._COMMIT_SLOT.release()
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "import_merge_busy"

    def test_a_failed_commit_still_frees_the_slot(
        self, storage, job_store, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("planner bug")

        monkeypatch.setattr(graph_merge_plan, "compute_merge_plan", _boom)
        client = _client(storage, job_store)
        with pytest.raises(RuntimeError):
            client.post(URL, json=_body(policy={"default": "skip"}))
        monkeypatch.undo()
        response = client.post(URL, json=_body(policy={"default": "skip"}))
        assert response.status_code == 200

    def test_a_document_over_the_v1_cap_is_refused(
        self, storage, job_store, monkeypatch
    ):
        from backend.service import graph_import

        monkeypatch.setattr(graph_import, "MAX_IMPORT_EDGES", 0)
        response = _client(storage, job_store).post(
            URL, json=_body(policy={"default": "skip"})
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "validation_failed"

    def test_stamp_provenance_is_still_refused(self, storage, job_store):
        response = _client(storage, job_store).post(
            URL, json=_body(policy={"default": "skip"}, stamp_provenance=True)
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "provenance_not_supported"
