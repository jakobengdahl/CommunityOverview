"""Staged sessions across two app instances on one PostgreSQL store
(docs/SESSION_OVERLAY_CONTRACT.md §21, slice S7, multi-instance criterion).

Two full apps - `create_app`, every route and the session-binding middleware -
share one PostgreSQL schema and one session directory, as two replicas of a
deployment do. One stages; the other reads the diff, composes the session's
view, and merges. The races a shared store adds are driven across the two:
a merge naming a reviewed version that the other instance restaged since is
`merge_raced` and writes nothing, and a merge retried on the other instance
with the same merge id finds the merge record and applies nothing twice (I9).

This is at API level rather than in a browser: two backend processes behind
one Playwright run would need a second server, port and proxy in CI for no
extra coverage of the store, which is where the multi-instance behaviour
lives. The two apps here are separate storages with separate in-memory
caches, so nothing is shared except what the store and the session directory
share.
"""

from __future__ import annotations

import os
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.api_host import AppConfig, create_app
from backend.runtime.session_context import SESSION_ID_HEADER

REQUIRE = os.environ.get("CO_REQUIRE_POSTGRES") == "1"
DSN = os.environ.get("CO_TEST_POSTGRES_DSN", "")

if REQUIRE:
    import psycopg  # a skip here would be the failure, not a pass
else:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")


def _server_reachable() -> bool:
    if not DSN:
        if REQUIRE:
            raise RuntimeError(
                "CO_REQUIRE_POSTGRES=1 but CO_TEST_POSTGRES_DSN is unset"
            )
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception as exc:
        if REQUIRE:
            raise RuntimeError(
                f"CO_REQUIRE_POSTGRES=1 but the server at {DSN} is unreachable: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason="set CO_TEST_POSTGRES_DSN to a reachable PostgreSQL server to run these",
)

# How long a main-graph write by one instance may take to reach the other's
# cache. An acceptance ceiling for a shared CI runner, as in
# backend/core/tests/test_multi_instance_acceptance.py, not a latency figure.
VISIBILITY_CEILING_S = 10.0


@pytest.fixture
def schema():
    name = f"co_s7_{uuid.uuid4().hex[:16]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def instances(schema, temp_static_dirs, tmp_path, mock_llm_provider):
    """Two apps on one schema and one session directory.

    Both stay up until the test has asserted everything: shutting one down
    while the other still runs is not what this module tests."""
    web_path, widget_path = temp_static_dirs

    def config(name):
        return AppConfig(
            graph_file=str(tmp_path / f"{name}.json"),
            web_static_path=web_path,
            widget_static_path=widget_path,
            sessions_dir=str(tmp_path / "sessions"),
            api_prefix="/api",
            auth_enabled=False,
            mcp_basic_auth=False,
            graph_backend="postgres",
            graph_postgres_dsn=DSN,
            graph_postgres_schema=schema,
            graph_staged_sessions="true",
        )

    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        with TestClient(create_app(config("a"))) as a:
            with TestClient(create_app(config("b"))) as b:
                # Two caches over one store, or the test proves nothing.
                assert a.app.state.graph_storage is not b.app.state.graph_storage
                yield a, b


def _h(sid):
    return {SESSION_ID_HEADER: sid}


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _eventually(predicate, what):
    deadline = time.monotonic() + VISIBILITY_CEILING_S
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"not within {VISIBILITY_CEILING_S}s: {what}")
        time.sleep(0.05)


def _main_node(client, node_id):
    response = client.get(f"/api/nodes/{node_id}")
    return None if response.status_code == 404 else _ok(response)["node"]


def _seed(a, b):
    _ok(
        a.post(
            "/api/nodes",
            json={"nodes": [{"id": "hub", "type": "Actor", "name": "Harbour Hub"}]},
        )
    )
    _eventually(lambda: _main_node(b, "hub") is not None, "the seed reaches B")


def test_one_instance_stages_and_the_other_reviews_composes_and_merges(instances):
    a, b = instances
    _seed(a, b)
    sid = _ok(a.post("/api/sessions", json={"name": "w", "write_mode": "staged"}))["id"]
    staged = _ok(
        a.post(
            "/api/nodes",
            headers=_h(sid),
            json={
                "nodes": [{"id": "kelp", "type": "Actor", "name": "Kelp Forum"}],
                "edges": [
                    {
                        "id": "kelp-link",
                        "source": "kelp",
                        "target": "hub",
                        "type": "RELATES_TO",
                    }
                ],
            },
        )
    )
    assert staged["staged"] is True

    # B resolves the session as staged and sees A's work in the diff and in
    # its composed reads - from the store, with nothing passed between apps.
    assert _ok(b.get(f"/api/sessions/{sid}"))["write_mode"] == "staged"
    diff = _ok(b.get(f"/api/sessions/{sid}/staged"))
    assert {(i["kind"], i["entity_id"]) for i in diff["items"]} == {
        ("node", "kelp"),
        ("edge", "kelp-link"),
    }
    found = _ok(b.post("/api/search", headers=_h(sid), json={"query": "kelp"}))
    assert [(n["id"], n["staged_state"]) for n in found["nodes"]] == [
        ("kelp", "created")
    ]
    related = _ok(b.post("/api/nodes/hub/related", headers=_h(sid), json={}))
    assert "kelp" in {n["id"] for n in related["nodes"]}
    # Neither instance's main graph holds it.
    assert _main_node(a, "kelp") is None and _main_node(b, "kelp") is None
    main_hits = _ok(b.post("/api/search", json={"query": "kelp"}))["nodes"]
    assert "kelp" not in {n["id"] for n in main_hits}

    # B merges; A's main graph and A's view of the session both follow.
    merged = _ok(b.post(f"/api/sessions/{sid}/staged/merge", json={"merge_id": "mi-1"}))
    assert {m["outcome"] for m in merged["merged"]} == {"applied"}
    _eventually(lambda: _main_node(a, "kelp") is not None, "B's merge reaches A")
    assert _ok(a.get(f"/api/sessions/{sid}/staged"))["count"] == 0
    related = _ok(a.post("/api/nodes/hub/related", json={}))
    assert "kelp-link" in {e["id"] for e in related["edges"]}


def test_a_merge_naming_a_version_the_other_instance_restaged_is_raced(instances):
    a, b = instances
    _seed(a, b)
    sid = _ok(a.post("/api/sessions", json={"name": "w", "write_mode": "staged"}))["id"]
    _ok(
        a.patch(
            "/api/nodes/hub", headers=_h(sid), json={"updates": {"name": "Reviewed"}}
        )
    )
    reviewed = _ok(a.get(f"/api/sessions/{sid}/staged"))["items"][0]

    # B restages the entry after A reviewed it.
    _ok(b.patch("/api/nodes/hub", headers=_h(sid), json={"updates": {"name": "Later"}}))
    entry = {"kind": "node", "entity_id": "hub", "version": reviewed["version"]}
    raced = a.post(
        f"/api/sessions/{sid}/staged/merge",
        json={"merge_id": "mi-r", "entries": [entry]},
    )
    assert raced.status_code == 409, raced.text
    assert raced.json()["detail"]["error"] == "merge_raced"
    for client in (a, b):
        assert _main_node(client, "hub")["name"] == "Harbour Hub"
        item = _ok(client.get(f"/api/sessions/{sid}/staged"))["items"][0]
        assert item["fields"]["name"]["staged"] == "Later"

    # Re-reading the diff and merging what it now says succeeds.
    fresh = _ok(a.get(f"/api/sessions/{sid}/staged"))["items"][0]
    entry["version"] = fresh["version"]
    _ok(
        a.post(
            f"/api/sessions/{sid}/staged/merge",
            json={"merge_id": "mi-r", "entries": [entry]},
        )
    )
    _eventually(lambda: _main_node(b, "hub")["name"] == "Later", "the merge reaches B")


def test_a_merge_retried_on_the_other_instance_is_answered_from_its_record(instances):
    a, b = instances
    _seed(a, b)
    sid = _ok(a.post("/api/sessions", json={"name": "w", "write_mode": "staged"}))["id"]
    _ok(
        a.post(
            "/api/nodes",
            headers=_h(sid),
            json={"nodes": [{"id": "once", "type": "Actor", "name": "Merged Once"}]},
        )
    )
    first = _ok(
        a.post(f"/api/sessions/{sid}/staged/merge", json={"merge_id": "mi-once"})
    )
    landed = _main_node(a, "once")
    assert landed is not None

    # A client that lost A's answer retries on B with the same merge id while
    # the session stages more meanwhile: B returns the recorded merge and
    # writes nothing, neither the new entry nor the old one a second time.
    _ok(
        b.post(
            "/api/nodes",
            headers=_h(sid),
            json={"nodes": [{"id": "after", "type": "Actor", "name": "Staged After"}]},
        )
    )
    retry = _ok(
        b.post(f"/api/sessions/{sid}/staged/merge", json={"merge_id": "mi-once"})
    )
    assert [m["entity_id"] for m in retry["merged"]] == [
        m["entity_id"] for m in first["merged"]
    ]
    assert _main_node(b, "after") is None
    _eventually(lambda: _main_node(b, "once") is not None, "the merge reaches B")
    assert _main_node(b, "once")["revision"] == landed["revision"]
    assert {
        i["entity_id"] for i in _ok(b.get(f"/api/sessions/{sid}/staged"))["items"]
    } == {"after"}

    # The merge id belongs to that session: another session cannot reuse it.
    other = _ok(b.post("/api/sessions", json={"name": "o", "write_mode": "staged"}))[
        "id"
    ]
    _ok(
        b.post(
            "/api/nodes",
            headers=_h(other),
            json={"nodes": [{"id": "elsewhere", "type": "Actor", "name": "Elsewhere"}]},
        )
    )
    taken = a.post(f"/api/sessions/{other}/staged/merge", json={"merge_id": "mi-once"})
    assert taken.status_code == 409, taken.text
    assert taken.json()["detail"]["error"] == "merge_id_taken"
    assert _main_node(a, "elsewhere") is None


def test_a_main_edit_on_one_instance_is_a_conflict_in_the_others_diff(instances):
    a, b = instances
    _seed(a, b)
    sid = _ok(a.post("/api/sessions", json={"name": "w", "write_mode": "staged"}))["id"]
    _ok(
        a.patch(
            "/api/nodes/hub", headers=_h(sid), json={"updates": {"name": "Workshop"}}
        )
    )
    _ok(a.patch("/api/nodes/hub", json={"updates": {"name": "Main meanwhile"}}))
    _eventually(
        lambda: _main_node(b, "hub")["name"] == "Main meanwhile", "the edit reaches B"
    )
    item = _ok(b.get(f"/api/sessions/{sid}/staged"))["items"][0]
    assert item["status"] == "conflict"
    assert item["fields"]["name"] == {
        "base": "Harbour Hub",
        "staged": "Workshop",
        "main": "Main meanwhile",
        "conflict": True,
    }
