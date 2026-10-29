"""PATCH /api/nodes/{id} and /api/edges/{id}: validation is 422, absence is 404,
and neither is decided before authorization."""

import pytest
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.config import AppConfig
from backend.core import Edge, Node, NodeType
from backend.core.models import MAX_METADATA_DEPTH
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)

_ENV = (
    "COMMUNITYOVERVIEW_ACTOR_ID",
    "COMMUNITYOVERVIEW_ACTOR_TYPE",
    "COMMUNITYOVERVIEW_AUTH_SOURCE",
    "COMMUNITYOVERVIEW_WORKSPACE_ID",
    "COMMUNITYOVERVIEW_WORKSPACE_KIND",
    "COMMUNITYOVERVIEW_GRAPH_SCOPE_ID",
    "COMMUNITYOVERVIEW_AUTHORIZATION_MODE",
)
_AUTH = ("admin", "secretpassword")
_TOO_LONG = "x" * 201
_TOO_DEEP: dict = {}
for _ in range(MAX_METADATA_DEPTH + 10):
    _TOO_DEEP = {"k": _TOO_DEEP}


class DenyMutationsHook:
    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=context.action != GRAPH_ACTION_MUTATE,
            reason="Mutations disabled by test hook.",
        )


class HideLocalGraphHook:
    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=True,
            graph_access=GraphAccessNarrowing(enabled=True, allow_local_graph=False),
        )


def _make_app(tmp_path, monkeypatch, authorization_hook=None, **config):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    app = create_app(
        AppConfig(
            graph_file=str(tmp_path / "graph.json"),
            sessions_dir=str(tmp_path / "sessions"),
            **config,
        )
    )
    service = app.state.graph_service
    if authorization_hook is not None:
        service._authorization_hook = authorization_hook
    storage = service._storage
    storage.add_nodes(
        [
            Node(id="n1", type=NodeType.ACTOR, name="One"),
            Node(id="n2", type=NodeType.ACTOR, name="Two"),
            Node(id="view-1", type=NodeType.SAVED_VIEW, name="View"),
        ],
        [Edge(id="e1", source="n1", target="n2", type="RELATES_TO")],
    )
    return TestClient(app)


@pytest.fixture
def open_client(tmp_path, monkeypatch):
    return _make_app(tmp_path, monkeypatch, auth_enabled=False)


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    client = _make_app(
        tmp_path,
        monkeypatch,
        auth_enabled=True,
        auth_username=_AUTH[0],
        auth_password=_AUTH[1],
    )
    client.headers["X-Requested-With"] = "api-client"
    return client


def _patch(client, kind, entity_id, updates, **kwargs):
    return client.patch(f"/api/{kind}/{entity_id}", json={"updates": updates}, **kwargs)


# (kind, existing id, missing id, valid updates, invalid updates)
_CASES = [
    ("nodes", "n1", "missing", {"description": "ok"}, {"name": _TOO_LONG}),
    ("edges", "e1", "missing", {"label": "ok"}, {"metadata": _TOO_DEEP}),
    (
        "nodes",
        "view-1",
        "missing",
        {"description": "ok"},
        {
            "metadata": {
                "annotations": [
                    {"type": "image", "image": {"url": "https://example.invalid/a.png"}}
                ]
            }
        },
    ),
]
_VALIDATION_DETAILS = {
    "n1": "String should have at most 200 characters",
    "e1": f"metadata nests deeper than {MAX_METADATA_DEPTH} levels",
    "view-1": "image annotation content must be an embedded image",
}


class TestValidationStatus:
    @pytest.mark.parametrize("kind,existing,_missing,_valid,invalid", _CASES)
    def test_invalid_update_on_existing_entity_is_422(
        self, open_client, kind, existing, _missing, _valid, invalid
    ):
        response = _patch(open_client, kind, existing, invalid)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert _VALIDATION_DETAILS[existing] in detail

    @pytest.mark.parametrize("kind,_existing,missing,_valid,invalid", _CASES)
    def test_missing_entity_stays_404_whatever_the_body(
        self, open_client, kind, _existing, missing, _valid, invalid
    ):
        assert _patch(open_client, kind, missing, invalid).status_code == 404

    @pytest.mark.parametrize("kind,existing,_missing,valid,_invalid", _CASES)
    def test_valid_update_still_succeeds(
        self, open_client, kind, existing, _missing, valid, _invalid
    ):
        assert _patch(open_client, kind, existing, valid).status_code == 200

    def test_stale_node_update_stays_409(self, open_client):
        response = open_client.patch(
            "/api/nodes/n1",
            json={
                "updates": {"description": "x"},
                "expected_updated_at": "2000-01-01T00:00:00+00:00",
            },
        )

        assert response.status_code == 409

    @pytest.mark.parametrize("kind,existing,_missing,valid,invalid", _CASES)
    def test_stale_revision_with_invalid_update_stays_409(
        self, open_client, kind, existing, _missing, valid, invalid
    ):
        updated = _patch(open_client, kind, existing, valid)
        assert updated.status_code == 200
        revision = updated.json()["node" if kind == "nodes" else "edge"]["revision"]

        response = open_client.patch(
            f"/api/{kind}/{existing}",
            json={"updates": invalid, "expected_revision": revision - 1},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "entity_revision_conflict"


class TestAuthorizationComesFirst:
    @pytest.mark.parametrize("kind,existing,missing,valid,invalid", _CASES)
    def test_hook_denied_patch_gets_one_answer(
        self, tmp_path, monkeypatch, kind, existing, missing, valid, invalid
    ):
        client = _make_app(
            tmp_path,
            monkeypatch,
            auth_enabled=False,
            authorization_hook=DenyMutationsHook(),
        )

        for entity_id in (existing, missing):
            for updates in (valid, invalid):
                response = _patch(client, kind, entity_id, updates)
                assert response.status_code == 403
                assert response.json()["detail"] == "Mutations disabled by test hook."

    @pytest.mark.parametrize("updates", [{"description": "ok"}, {"name": _TOO_LONG}])
    def test_invisible_node_stays_404_whatever_the_body(
        self, tmp_path, monkeypatch, updates
    ):
        client = _make_app(
            tmp_path,
            monkeypatch,
            auth_enabled=False,
            authorization_hook=HideLocalGraphHook(),
        )

        response = _patch(client, "nodes", "n1", updates)

        assert response.status_code == 404
        assert response.json()["detail"] == "Node with ID n1 not found"

    @pytest.mark.parametrize("kind,existing,missing,valid,invalid", _CASES)
    def test_unauthenticated_patch_gets_one_answer(
        self, auth_client, kind, existing, missing, valid, invalid
    ):
        seen = {
            (entity_id, tuple(updates)): _patch(auth_client, kind, entity_id, updates)
            for entity_id in (existing, missing)
            for updates in (valid, invalid)
        }

        assert {r.status_code for r in seen.values()} == {401}
        assert len({r.text for r in seen.values()}) == 1

    @pytest.mark.parametrize("kind,existing,missing,valid,invalid", _CASES)
    def test_authenticated_patch_reaches_422_and_404(
        self, auth_client, kind, existing, missing, valid, invalid
    ):
        assert (
            _patch(auth_client, kind, existing, invalid, auth=_AUTH).status_code == 422
        )
        assert (
            _patch(auth_client, kind, missing, invalid, auth=_AUTH).status_code == 404
        )

    @pytest.mark.parametrize("mode", ["read-only", "deny-all"])
    @pytest.mark.parametrize("kind,existing,missing,valid,invalid", _CASES)
    def test_unauthorized_patch_gets_one_answer(
        self, tmp_path, monkeypatch, mode, kind, existing, missing, valid, invalid
    ):
        client = _make_app(tmp_path, monkeypatch, auth_enabled=False)
        monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", mode)

        seen = [
            _patch(client, kind, entity_id, updates)
            for entity_id in (existing, missing)
            for updates in (valid, invalid)
        ]

        assert {r.status_code for r in seen} == {403}
        assert len({r.text for r in seen}) == 1
