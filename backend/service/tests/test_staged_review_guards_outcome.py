from __future__ import annotations

import pytest

from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import _stage


@pytest.fixture
def pgw(request, tmp_path):
    request.param = "postgres"
    yield from make_world(request, tmp_path)


def _failing_merge(world, monkeypatch):
    def fail(*a, **k):
        raise ConnectionError("store down")

    monkeypatch.setattr(world.storage.layer_store, "merge", fail)


def _setup(pgw, monkeypatch):
    sid = pgw.staged_session()
    _stage(pgw, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    _failing_merge(pgw, monkeypatch)
    return sid, ("node", "a"), pgw.storage.get_node("a").revision


# ST23
def test_an_unreadable_store_leaves_the_merge_outcome_unknown(pgw, monkeypatch):
    sid, key, _ = _setup(pgw, monkeypatch)
    backend = pgw.storage._persistence_backend

    def unreadable(keys):
        raise ConnectionError("no read")

    monkeypatch.setattr(backend, "read_write_evidence", unreadable)
    res = pgw.service.merge_staged_changes(sid, "m")
    assert res["error"] == "write_outcome_unknown"
    assert key in pgw.layer(sid)


# ST19 / ST22 / ST20
def test_a_second_reading_that_differs_leaves_the_outcome_unknown_and_reloads(
    pgw, monkeypatch
):
    sid, key, revision = _setup(pgw, monkeypatch)
    backend = pgw.storage._persistence_backend
    readings = iter([{key: (revision,)}, {key: (revision + 1,)}])
    monkeypatch.setattr(backend, "read_write_evidence", lambda keys: next(readings))
    loaded = []
    original = pgw.storage.load
    monkeypatch.setattr(
        pgw.storage, "load", lambda *a, **k: (loaded.append(1), original(*a, **k))[1]
    )
    res = pgw.service.merge_staged_changes(sid, "m")
    assert res["error"] == "write_outcome_unknown"
    assert loaded


# ST18 sanity (already killed): both readings unchanged -> write_failed
def test_two_unchanged_readings_rule_the_merge_out(pgw, monkeypatch):
    sid, key, revision = _setup(pgw, monkeypatch)
    backend = pgw.storage._persistence_backend
    monkeypatch.setattr(backend, "read_write_evidence", lambda keys: {key: (revision,)})
    res = pgw.service.merge_staged_changes(sid, "m")
    assert res["error"] == "write_failed"
