# covers: backend/api_host/federation_routes.py, backend/api_host/middleware.py
"""Tests for the federation read route as served by the app."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from backend.api_host import federation_routes
from backend.api_host.server import create_app
from backend.api_host.tests.test_auth_middleware import _make_config
from backend.federation import publish
from backend.federation.publish import FEDERATION_GRAPH_PATH, FederationAuthResult
from backend.runtime.extensions import RuntimeExtensions

INSTANCE_TOKEN = "instance-token"
CONSUMER_KEY = "consumer-key"


def _client(tmp_path, monkeypatch, federation, runtime_extensions=None):
    path = tmp_path / "federation.json"
    path.write_text(json.dumps({"federation": federation}), encoding="utf-8")
    monkeypatch.setenv("FEDERATION_FILE", str(path))
    monkeypatch.setenv("CONSUMER_KEY", CONSUMER_KEY)
    config, _ = _make_config(auth_enabled=True, auth_bearer_token=INSTANCE_TOKEN)
    return TestClient(create_app(config, runtime_extensions=runtime_extensions))


def _per_consumer(enabled=True):
    return {
        "enabled": enabled,
        "publish": {
            "enabled": True,
            "access": "per_consumer",
            "graph_id": "methods",
            "consumers": [{"consumer_id": "partner", "token_env": "CONSUMER_KEY"}],
        },
    }


def test_switched_off_federation_has_no_route_whatever_keys_are_set(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch, _per_consumer(enabled=False))

    with_key = client.get(FEDERATION_GRAPH_PATH, headers={"X-API-Key": CONSUMER_KEY})
    as_instance = client.get(
        FEDERATION_GRAPH_PATH, headers={"Authorization": f"Bearer {INSTANCE_TOKEN}"}
    )

    assert with_key.status_code == 401
    assert as_instance.status_code == 404


def test_a_consumer_key_reads_the_graph_and_every_refusal_looks_the_same(
    tmp_path, monkeypatch, caplog
):
    client = _client(tmp_path, monkeypatch, _per_consumer())

    with caplog.at_level(logging.INFO, logger=publish.__name__):
        served = client.get(
            FEDERATION_GRAPH_PATH, headers={"Authorization": f"Bearer {CONSUMER_KEY}"}
        )
        refusals = [
            client.get(FEDERATION_GRAPH_PATH, headers=headers)
            for headers in (
                {},
                {"X-API-Key": "wrong"},
                {"Authorization": f"Bearer {INSTANCE_TOKEN}"},
                {"Authorization": f"Bearer {CONSUMER_KEY}", "X-API-Key": "other"},
            )
        ]

    assert served.status_code == 200
    assert set(served.json()) == {"nodes", "edges"}
    assert {(r.status_code, r.content) for r in refusals} == {
        (401, refusals[0].content)
    }
    records = [r for r in caplog.records if hasattr(r, "federation_fetch")]
    assert [
        (r.federation_fetch["consumer_id"], r.federation_fetch["outcome"])
        for r in records
    ] == [("partner", "served")] + [("unauthenticated", "refused_auth")] * 4
    assert {r.federation_fetch["graph_id"] for r in records} == {"methods"}
    for record in records:
        assert CONSUMER_KEY not in record.args
        assert CONSUMER_KEY not in record.federation_fetch.values()


def test_open_mode_serves_without_a_key_while_instance_auth_guards_the_rest(
    tmp_path, monkeypatch
):
    federation = {"enabled": True, "publish": {"enabled": True, "access": "open"}}
    client = _client(tmp_path, monkeypatch, federation)

    assert client.get(FEDERATION_GRAPH_PATH).status_code == 200
    assert client.get("/export_graph").status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", FEDERATION_GRAPH_PATH),
        ("PUT", FEDERATION_GRAPH_PATH),
        ("POST", "/execute_tool"),
        ("GET", "/export_graph"),
        ("POST", "/api/nodes"),
    ],
)
def test_a_federation_key_opens_no_other_route_or_method(
    tmp_path, monkeypatch, method, path
):
    client = _client(tmp_path, monkeypatch, _per_consumer())

    response = client.request(
        method,
        path,
        headers={"Authorization": f"Bearer {CONSUMER_KEY}"},
        json={"tool_name": "add_nodes", "arguments": {}},
    )

    assert response.status_code == 401


def test_a_graph_read_failure_after_a_matched_key_is_logged_as_error(
    tmp_path, monkeypatch, caplog
):
    client = _client(tmp_path, monkeypatch, _per_consumer())
    client.raise_server_exceptions = False

    def broken(graph_storage):
        raise RuntimeError("storage down")

    monkeypatch.setattr(federation_routes, "published_graph", broken)
    with caplog.at_level(logging.INFO, logger=publish.__name__):
        response = client.get(
            FEDERATION_GRAPH_PATH, headers={"X-API-Key": CONSUMER_KEY}
        )

    assert response.status_code == 500
    records = [r for r in caplog.records if hasattr(r, "federation_fetch")]
    assert [
        (r.federation_fetch["consumer_id"], r.federation_fetch["outcome"])
        for r in records
    ] == [("partner", publish.OUTCOME_ERROR)]


class _AsyncProvider:
    async def authenticate(self, presented):
        return FederationAuthResult("external-partner", "served")


class _RaisingProvider:
    def authenticate(self, presented):
        raise RuntimeError("auth backend down")


@pytest.mark.parametrize("broken_provider", [_AsyncProvider, _RaisingProvider])
def test_external_mode_admits_through_the_installed_key_provider(
    tmp_path, monkeypatch, caplog, broken_provider
):
    class Provider:
        def authenticate(self, presented):
            outcome = "served" if presented == "external-key" else "refused_auth"
            return FederationAuthResult("external-partner", outcome)

    federation = {"enabled": True, "publish": {"enabled": True, "access": "external"}}
    installed = _client(
        tmp_path,
        monkeypatch,
        federation,
        RuntimeExtensions(federation_key_provider=Provider()),
    )
    missing = _client(tmp_path, monkeypatch, federation)
    broken = _client(
        tmp_path,
        monkeypatch,
        federation,
        RuntimeExtensions(federation_key_provider=broken_provider()),
    )
    broken.raise_server_exceptions = False

    headers = {"X-API-Key": "external-key"}
    assert installed.get(FEDERATION_GRAPH_PATH, headers=headers).status_code == 200
    refused = missing.get(FEDERATION_GRAPH_PATH, headers=headers)
    with caplog.at_level(logging.INFO, logger=publish.__name__):
        failed = broken.get(FEDERATION_GRAPH_PATH, headers=headers)
    assert (failed.status_code, failed.content) == (401, refused.content)
    records = [r for r in caplog.records if hasattr(r, "federation_fetch")]
    assert [r.federation_fetch["outcome"] for r in records] == ["refused_auth"]
