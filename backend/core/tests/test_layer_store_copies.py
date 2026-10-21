"""Copies the layer store hands out are deep: a caller mutating one cannot
change what is stored (the file backend, and layer_store.json_copy)."""

import backend.core.layer_store as ls
from backend.core.storage_backends import EntityOperation
from backend.core.tests.layer_store_contract import A, entry, fresh
from backend.core.tests.persistence_contract import by_id
from backend.core.tests import test_layer_store_file as _file

# The file layer store's fixtures, as this module's own.
staged = _file.staged
path = _file.path
reopen = _file.reopen

REC = {"actor": "t", "entries": [{"n": {"x": 1}}]}


def merge_nested(backend, merge_id="m1"):
    current = by_id(backend.load_graph_data(), "nodes")["a"]
    op = EntityOperation(
        "node",
        "upsert",
        "a",
        fresh({**current, "name": "M"}),
        expected_revision=current["revision"],
    )
    return backend.merge(
        A, merge_id, [op], [], [("node", "a")], dict(REC, entries=[{"n": {"x": 1}}])
    )


def poke(record):
    record["entries"][0]["n"]["x"] = 99


def test_json_copy_is_deep():
    for fn in (ls.json_copy,):
        src = {"a": {"b": [1, {"c": 2}]}}
        out = fn(src)
        out["a"]["b"][1]["c"] = 3
        out["a"]["b"].append(4)
        assert src == {"a": {"b": [1, {"c": 2}]}}


def test_staging_json_copy_is_deep():
    from backend.service.staging import json_copy

    src = {"a": {"b": [1, {"c": 2}]}}
    out = json_copy(src)
    out["a"]["b"][1]["c"] = 3
    assert src == {"a": {"b": [1, {"c": 2}]}}


def test_load_layer_copies_are_deep(staged):
    staged.stage(A, [entry("node", "c", staged={"metadata": {"k": {"v": 1}}})])
    got = {e["entity_id"]: e for e in staged.load_layer(A)}
    got["c"]["staged"]["metadata"]["k"]["v"] = 99
    again = {e["entity_id"]: e for e in staged.load_layer(A)}
    assert again["c"]["staged"]["metadata"]["k"]["v"] == 1


def test_merge_result_record_is_a_deep_copy(staged):
    result = merge_nested(staged)
    poke(result.record)
    assert staged.load_merge("m1")["entries"][0]["n"]["x"] == 1


def test_load_merge_answer_is_a_deep_copy(staged):
    merge_nested(staged)
    first = staged.load_merge("m1")
    poke(first)
    assert staged.load_merge("m1")["entries"][0]["n"]["x"] == 1


def test_load_merge_pending_answer_is_a_deep_copy(staged, monkeypatch):
    def broken(sessions):
        raise OSError("disk gone")

    monkeypatch.setattr(staged, "_write_layers_file", broken)
    merge_nested(staged)
    first = staged.load_merge("m1")
    poke(first)
    assert staged.load_merge("m1")["entries"][0]["n"]["x"] == 1
    assert staged._pending_merge["record"]["entries"][0]["n"]["x"] == 1


def test_landed_after_failed_commit_record_is_a_deep_copy(staged, monkeypatch):
    real = staged._append_journal_locked

    def durable_then_raise(ops, stored):
        real(ops, stored)
        raise OSError("close failed")

    monkeypatch.setattr(staged, "_append_journal_locked", durable_then_raise)
    result = merge_nested(staged)
    monkeypatch.undo()
    poke(result.record)
    assert staged.load_merge("m1")["entries"][0]["n"]["x"] == 1
