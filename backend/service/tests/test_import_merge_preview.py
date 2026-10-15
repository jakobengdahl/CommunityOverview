"""
REST, service and MCP tests for the read-only merge preview
(``POST /import/merge/preview`` and the ``preview_graph_merge`` tool) of
docs/adr/0013-graph-import-merge-mode.md, slice 1.
"""

import json
import os
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.execution import InMemoryExecutionStore
from backend.core import Edge, GraphStorage, Node
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAccessNarrowing,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
)
from backend.service import GraphService, create_rest_router
from backend.service.mcp_tools import register_mcp_tools

URL = "/api/graph/import/merge/preview"


class _Hook:
    def __init__(self, *, allowed=True, narrowed=False):
        self.allowed = allowed
        self.narrowed = narrowed
        self.contexts = []

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        self.contexts.append(context)
        return GraphAuthorizationDecision(
            allowed=self.allowed,
            mode="test",
            source="test",
            reason=None if self.allowed else "nope",
            graph_access=GraphAccessNarrowing(
                enabled=self.narrowed,
                allow_local_graph=False,
                include_graph_ids=("graph-a",) if self.narrowed else (),
            ),
        )


@pytest.fixture
def storage(temp_dir) -> GraphStorage:
    store = GraphStorage(json_path=os.path.join(temp_dir, "graph.json"))
    store.add_nodes(
        [
            Node(id="a1", type="Actor", name="Alice"),
            Node(id="i1", type="Initiative", name="Project X"),
        ],
        [Edge(id="e1", source="a1", target="i1", type="BELONGS_TO")],
    )
    return store


def _client(storage, hook=None, job_store=None):
    service = GraphService(storage, authorization_hook=hook)
    router = create_rest_router(
        service, import_job_store=job_store or InMemoryExecutionStore()
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/graph")
    return TestClient(app)


def _body(**overrides):
    body = {
        "document": {
            "nodes": [
                {"id": "a1", "type": "Actor", "name": "Alice v2"},
                {"id": "n-new", "type": "Goal", "name": "New goal"},
            ],
            "edges": [
                {"id": "e-new", "source": "a1", "target": "n-new", "type": "AIMS_FOR"}
            ],
        },
        "policy": {"default": "overwrite"},
    }
    body.update(overrides)
    return body


def _snapshot(storage: GraphStorage):
    return (
        json.dumps([n.to_dict() for n in storage.get_all_nodes()], sort_keys=True),
        json.dumps([e.to_dict() for e in storage.get_all_edges()], sort_keys=True),
        storage.generation,
        open(storage.json_path, "rb").read(),
    )


class TestPreviewIsReadOnly:
    def test_preview_changes_nothing_and_starts_nothing(self, storage, monkeypatch):
        emitted = []
        monkeypatch.setattr(storage, "_emit_event", lambda *a, **k: emitted.append(a))
        job_store = InMemoryExecutionStore()
        before = _snapshot(storage)

        response = _client(storage, job_store=job_store).post(URL, json=_body())

        assert response.status_code == 200
        body = response.json()
        assert body["plan"]["overwrites"]["nodes"]["count"] == 1
        assert body["plan"]["adds"]["nodes"]["ids"] == ["n-new"]
        assert _snapshot(storage) == before
        assert emitted == []
        assert job_store.list_jobs(limit=10) == []
        backup_dir = storage.json_path.parent / "import-backups"
        assert not backup_dir.exists()

    def test_the_same_request_twice_yields_the_same_digest(self, storage):
        client = _client(storage)
        first = client.post(URL, json=_body()).json()
        second = client.post(URL, json=_body()).json()
        assert first["plan_digest"] == second["plan_digest"]


class TestAuthorization:
    def test_requires_the_mutate_action_on_the_merge_target(self, storage):
        hook = _Hook()
        assert _client(storage, hook).post(URL, json=_body()).status_code == 200
        assert [(c.action, c.target) for c in hook.contexts] == [
            (GRAPH_ACTION_MUTATE, "import_graph_merge")
        ]

    def test_a_denied_caller_gets_403_and_no_plan(self, storage):
        response = _client(storage, _Hook(allowed=False)).post(URL, json=_body())
        assert response.status_code == 403
        assert "plan" not in response.text

    def test_a_narrowed_caller_is_refused_before_anything_is_read(
        self, storage, monkeypatch
    ):
        monkeypatch.setattr(
            storage, "get_node", Mock(side_effect=AssertionError("read the graph"))
        )
        response = _client(storage, _Hook(narrowed=True)).post(URL, json=_body())
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error_code"] == "import_requires_full_graph_access"
        assert "plan" not in detail

    def test_request_authorization_reaches_the_worker_thread(self, storage):
        hook = _Hook()
        response = _client(storage, hook).post(
            URL,
            json=_body(),
            headers={"x-communityoverview-actor-id": "actor-7"},
        )
        assert response.status_code == 200
        assert hook.contexts[0].actor.get("actor_id") == "actor-7"

    def test_plans_off_the_event_loop_thread(self, storage, monkeypatch):
        import threading

        service = GraphService(storage)
        threads = []
        original = service.preview_merge_import

        def _record(body, **kwargs):
            threads.append(threading.get_ident())
            return original(body, **kwargs)

        monkeypatch.setattr(service, "preview_merge_import", _record)
        app = FastAPI()
        app.include_router(
            create_rest_router(service, import_job_store=InMemoryExecutionStore()),
            prefix="/api/graph",
        )
        loop_threads = []

        @app.get("/loop-thread")
        async def _loop_thread():
            loop_threads.append(threading.get_ident())
            return {}

        with TestClient(app) as client:
            client.get("/loop-thread")
            assert client.post(URL, json=_body()).status_code == 200
        assert threads and threads[0] != loop_threads[0]

    def test_a_second_concurrent_preview_is_refused_as_busy(self, storage):
        from backend.service import import_service

        assert import_service._PREVIEW_SLOT.acquire(blocking=False)
        try:
            response = _client(storage).post(URL, json=_body())
        finally:
            import_service._PREVIEW_SLOT.release()
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "import_preview_busy"
        assert _client(storage).post(URL, json=_body()).status_code == 200

    def test_a_failed_preview_still_frees_the_slot(self, storage, monkeypatch):
        from backend.service import graph_merge_plan

        def _boom(*args, **kwargs):
            raise KeyError("planner failure")

        monkeypatch.setattr(graph_merge_plan, "compute_merge_plan", _boom)
        client = TestClient(_client(storage).app, raise_server_exceptions=False)
        assert client.post(URL, json=_body()).status_code == 500
        monkeypatch.undo()
        assert _client(storage).post(URL, json=_body()).status_code == 200

    def test_a_denied_caller_gets_403_not_busy_while_the_slot_is_held(self, storage):
        from backend.service import import_service

        assert import_service._PREVIEW_SLOT.acquire(blocking=False)
        try:
            denied = _client(storage, _Hook(allowed=False)).post(URL, json=_body())
            narrowed = _client(storage, _Hook(narrowed=True)).post(URL, json=_body())
        finally:
            import_service._PREVIEW_SLOT.release()
        assert denied.status_code == 403
        assert narrowed.status_code == 409

    def test_authorization_runs_before_parameter_validation(self, storage):
        response = _client(storage, _Hook(allowed=False)).post(
            URL, json={"bogus": True}
        )
        assert response.status_code == 403


class TestRequestRefusals:
    def test_stamp_provenance_true_is_refused(self, storage):
        response = _client(storage).post(URL, json=_body(stamp_provenance=True))
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "provenance_not_supported"

    def test_an_unknown_policy_is_refused(self, storage):
        response = _client(storage).post(URL, json=_body(policy={"default": "merge"}))
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "invalid_parameters"

    def test_a_document_over_the_v1_cap_is_refused(self, storage, monkeypatch):
        from backend.service import graph_import

        monkeypatch.setattr(graph_import, "MAX_IMPORT_EDGES", 0)
        response = _client(storage).post(URL, json=_body())
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error_code"] == "validation_failed"
        assert detail["errors"][0]["location"] == "edges"

    def test_a_replace_storm_is_reported_as_retryable(self, storage, monkeypatch):
        from backend.service import graph_merge_plan

        def _unstable(*args, **kwargs):
            raise graph_merge_plan.MergePlanUnstable("moved")

        monkeypatch.setattr(graph_merge_plan, "compute_merge_plan", _unstable)
        response = _client(storage).post(URL, json=_body())
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "import_plan_unstable"

    def test_a_blocked_plan_is_still_a_successful_preview(self, storage):
        response = _client(storage).post(URL, json=_body(policy={"default": "fail"}))
        assert response.status_code == 200
        plan = response.json()["plan"]
        assert plan["committable"] is False
        reasons = {(c["kind"], c["reason"]) for c in plan["conflicts"]["items"]}
        assert ("node", "id_collision") in reasons


class TestReplaceRoutesAreUnchanged:
    def test_post_import_still_replaces(self, storage):
        response = _client(storage).post(
            "/api/graph/import",
            json={"nodes": [{"id": "only", "type": "Actor", "name": "Only"}]},
        )
        assert response.status_code == 200
        assert response.json()["graph_replaced"] is True
        assert [n.id for n in storage.get_all_nodes()] == ["only"]


class TestMcpPreviewTool:
    def test_plans_only_small_documents(self, storage, monkeypatch):
        from backend.service import graph_merge_plan

        monkeypatch.setattr(graph_merge_plan, "MCP_PREVIEW_MAX_NODES", 1)
        tools = self._tools(storage)
        result = tools["preview_graph_merge"](document=_body()["document"])
        assert result["error_code"] == "validation_failed"
        assert result["errors"][0]["location"] == "nodes"

    def _tools(self, storage, hook=None):
        return register_mcp_tools(
            Mock(), GraphService(storage, authorization_hook=hook)
        )

    def test_the_caps_are_ten_thousand_nodes_and_twenty_thousand_edges(self):
        from backend.service import graph_merge_plan

        assert graph_merge_plan.MCP_PREVIEW_MAX_NODES == 10_000
        assert graph_merge_plan.MCP_PREVIEW_MAX_EDGES == 20_000

    def test_plans_only_documents_within_the_edge_cap(self, storage, monkeypatch):
        from backend.service import graph_merge_plan

        monkeypatch.setattr(graph_merge_plan, "MCP_PREVIEW_MAX_EDGES", 0)
        tools = self._tools(storage)
        result = tools["preview_graph_merge"](document=_body()["document"])
        assert result["errors"][0]["location"] == "edges"

    def test_returns_the_same_plan_and_digest_as_rest(self, storage):
        tools = self._tools(storage)
        body = _body()
        via_mcp = tools["preview_graph_merge"](
            document=body["document"], policy=body["policy"]
        )
        via_rest = _client(storage).post(URL, json=body).json()
        assert via_mcp["plan_digest"] == via_rest["plan_digest"]
        assert via_mcp["plan"] == via_rest["plan"]

    def test_is_refused_for_a_narrowed_caller(self, storage):
        tools = self._tools(storage, _Hook(narrowed=True))
        result = tools["preview_graph_merge"](document=_body()["document"])
        assert result["error_code"] == "import_requires_full_graph_access"

    def test_is_refused_for_a_denied_caller(self, storage):
        tools = self._tools(storage, _Hook(allowed=False))
        result = tools["preview_graph_merge"](document=_body()["document"])
        assert result["error_code"] == "access_denied"

    def test_refuses_stamp_provenance(self, storage):
        tools = self._tools(storage)
        result = tools["preview_graph_merge"](
            document=_body()["document"], stamp_provenance=True
        )
        assert result["error_code"] == "provenance_not_supported"

    def test_there_is_no_merge_commit_tool(self, storage):
        tools = self._tools(storage)
        assert not [
            name for name in tools if "merge" in name and name != "preview_graph_merge"
        ]
