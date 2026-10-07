"""Tests for ``POST /api/federation/teleport`` at the REST layer.

Two things only this layer can pin:

- the deliberate split between a **request-level** refusal, which is a 403 via
  ``_raise_for_access_denied``, and a **per-graph** denial, which is a 200 whose
  ``status`` the UI renders. Collapsing either into the other would turn a
  normal "you cannot see that graph" into a hard error, or a refused request
  into something the UI silently renders.
- that the route passes the deployment's configured public URL as the origin to
  compare against, rather than the request's own ``base_url`` — which behind a
  TLS-terminating proxy is the internal one and would make every hop look
  cross-deployment.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import GraphStorage, Node, NodeType
from backend.federation import teleport
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.service import GraphService
from backend.service.rest_api import create_rest_router

_GUI_URL = "https://esam.example/app"
_PUBLIC_BASE_URL = "https://here.example/app"


class _NarrowingHook:
    def __init__(self, *include, allow_local=True, allowed=True):
        self._include = tuple(include)
        self._allow_local = allow_local
        self._allowed = allowed

    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=self._allowed,
            reason="" if self._allowed else "request refused",
            graph_access=GraphAccessNarrowing(
                enabled=True,
                allow_local_graph=self._allow_local,
                include_graph_ids=self._include,
            ),
        )


def _service(tmp_path, *, gui_url=_GUI_URL):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))
    storage.add_nodes([Node(id="local-1", type=NodeType.ACTOR, name="Local Node")], [])

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "endpoints": {
                            "graph_json_url": "https://esam.example/graph.json",
                            "gui_url": gui_url,
                        },
                    }
                ],
            }
        }
    )
    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0],
        [{"id": "remote-1", "type": "Actor", "name": "External Node"}],
        [],
    )
    manager._cache["esam-main"].nodes = cache_nodes
    manager._cache["esam-main"].status = "healthy"
    return GraphService(storage, federation_manager=manager)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(*, hook=None, public_base_url=_PUBLIC_BASE_URL, gui_url=_GUI_URL):
        monkeypatch.setenv("COMMUNITYOVERVIEW_PUBLIC_BASE_URL", public_base_url)
        service = _service(tmp_path, gui_url=gui_url)
        if hook is not None:
            service._authorization_hook = hook
        app = FastAPI()
        app.include_router(create_rest_router(service), prefix="/api")
        return TestClient(app), service

    return _make


def _post(client, node_id, **body):
    return client.post("/api/federation/teleport", json={"node_id": node_id, **body})


class TestTheRouteResolves:
    def test_a_federated_node_returns_a_route(self, make_client):
        client, _ = make_client()

        response = _post(client, "federated::esam-main::remote-1")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["status"] == teleport.STATUS_OK
        assert body["route"].startswith("https://esam.example/app?")
        assert "node=remote-1" in body["route"]

    def test_a_local_node_reports_being_here_rather_than_a_route(self, make_client):
        client, _ = make_client()

        body = _post(client, "local-1").json()

        assert body["status"] == teleport.STATUS_LOCAL
        assert "route" not in body

    def test_an_unknown_id_is_a_200_with_an_unknown_status(self, make_client):
        client, _ = make_client()

        response = _post(client, "no-such-node")

        assert response.status_code == 200
        assert response.json()["status"] == teleport.STATUS_UNKNOWN_NODE

    def test_the_session_and_search_context_reach_the_route(self, make_client):
        client, _ = make_client()

        body = _post(
            client,
            "federated::esam-main::remote-1",
            session_id="1111-2222",
            search_query="external",
        ).json()

        assert "from_session=1111-2222" in body["route"]
        assert "q=external" in body["route"]


class TestTheDenialSplit:
    def test_a_per_graph_denial_is_a_200_the_ui_can_render(self, make_client):
        client, _ = make_client(hook=_NarrowingHook("other-graph"))

        response = _post(client, "federated::esam-main::remote-1")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["status"] == teleport.STATUS_PERMISSION_DENIED

    def test_a_per_graph_denial_leaks_nothing_about_the_graph(self, make_client):
        client, _ = make_client(hook=_NarrowingHook("other-graph"))

        body = _post(client, "federated::esam-main::remote-1").json()

        assert body["origin_graph_id"] == ""
        assert body["origin_graph_name"] == ""
        assert body["origin_node_id"] == ""
        assert "route" not in body
        assert "trust_level" not in body
        assert "backlink" not in body
        serialized = json.dumps(body)
        assert "esam" not in serialized.lower()
        assert "here.example" not in serialized

    def test_a_request_level_refusal_is_a_403(self, make_client):
        client, _ = make_client(hook=_NarrowingHook(allowed=False))

        response = _post(client, "federated::esam-main::remote-1")

        assert response.status_code == 403

    def test_a_request_level_refusal_body_carries_no_teleport_status(self, make_client):
        client, _ = make_client(hook=_NarrowingHook(allowed=False))

        body = _post(client, "federated::esam-main::remote-1").json()

        assert "status" not in body
        assert "route" not in body


class TestTheOriginComparedAgainst:
    def test_the_configured_public_url_decides_cross_deployment_not_base_url(
        self, make_client
    ):
        # TestClient's base_url is http://testserver, so comparing against it
        # would call this a cross-deployment hop. The public URL is the real
        # origin and shares a host with the gui_url here.
        client, _ = make_client(
            public_base_url="https://here.example/app",
            gui_url="https://here.example/other-graph",
        )

        body = _post(client, "federated::esam-main::remote-1").json()

        assert body["status"] == teleport.STATUS_OK
        assert body["cross_deployment"] is False

    def test_a_different_host_is_still_a_cross_deployment_hop(self, make_client):
        client, _ = make_client(
            public_base_url="https://here.example/app",
            gui_url="https://elsewhere.example/app",
        )

        assert (
            _post(client, "federated::esam-main::remote-1").json()["cross_deployment"]
            is True
        )

    def test_an_unset_public_url_offers_no_backlink(self, make_client):
        client, _ = make_client(public_base_url="")

        body = _post(client, "federated::esam-main::remote-1").json()

        assert "backlink" not in body

    def test_a_configured_public_url_offers_a_backlink(self, make_client):
        client, _ = make_client()

        body = _post(
            client, "federated::esam-main::remote-1", session_id="1111-2222"
        ).json()

        assert body["backlink"].startswith("https://here.example/app?")
        assert "from_session=1111-2222" in body["backlink"]
