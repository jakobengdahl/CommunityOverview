"""PATCH /api/nodes/{id} and /api/edges/{id}: validation is 422, absence is 404,
and neither is decided before authorization."""

import pytest
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.config import AppConfig
from backend.core import Edge, Node, NodeType
from backend.core.models import MAX_METADATA_DEPTH

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


def _make_app(tmp_path, monkeypatch, **config):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    app = create_app(
        AppConfig(
            graph_file=str(tmp_path / "graph.json"),
            sessions_dir=str(tmp_path / "sessions"),
            **config,
        )
    )
    storage = app.state.graph_service._storage
    storage.add_nodes(
        [
            Node(id="n1", type=NodeType.ACTOR, name="One"),
            Node(id="n2", type=NodeType.ACTOR, name="Two"),
        ],
        [Edge(id="e1", source="n1", target="n2", type="RELATES_TO")],
    )
    return TestClient(app)


@pytest.fixture
def open_client(tmp_path, monkeypatch):
    return _make_app(tmp_path, monkeypatch, auth_enabled=False)


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    return _make_app(
        tmp_path,
        monkeypatch,
        auth_enabled=True,
        auth_username=_AUTH[0],
        auth_password=_AUTH[1],
    )


def _patch(client, kind, entity_id, updates, **kwargs):
    return client.patch(f"/api/{kind}/{entity_id}", json={"updates": updates}, **kwargs)


# (kind, existing id, missing id, valid updates, invalid updates)
_CASES = [
    ("nodes", "n1", "missing", {"description": "ok"}, {"name": _TOO_LONG}),
    ("edges", "e1", "missing", {"label": "ok"}, {"metadata": _TOO_DEEP}),
]


class TestValidationStatus:
    @pytest.mark.parametrize("kind,existing,_missing,_valid,invalid", _CASES)
    def test_invalid_update_on_existing_entity_is_422(
        self, open_client, kind, existing, _missing, _valid, invalid
    ):
        response = _patch(open_client, kind, existing, invalid)

        assert response.status_code == 422
        assert response.json()["detail"]

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


class TestAuthorizationComesFirst:
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
