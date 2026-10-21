"""Staging and merge helpers hand out deep copies: a caller mutating one cannot change an entry, a document or what is stored."""

import copy

from backend.service import staged_review as sr
from backend.service.staging import _apply_staged, _edge, _node, compose


def _doc():
    return {
        "id": "n",
        "type": "Actor",
        "name": "N",
        "description": "",
        "metadata": {"k": {"v": 1}},
        "tags": [1, {"z": 1}],
    }


def test_apply_staged_result_shares_nothing_with_doc_or_staged():
    doc = _doc()
    staged = {"tags": [2, {"z": 2}], "metadata": {"j": 1}}
    d0, s0 = copy.deepcopy(doc), copy.deepcopy(staged)
    out = _apply_staged(doc, staged)
    out["metadata"]["k"]["v"] = 9
    out["tags"][1]["z"] = 9
    assert doc == d0
    assert staged == s0


def test_apply_staged_replaced_field_shares_nothing_with_staged():
    doc = _doc()
    staged = {"tags": [2, {"z": 2}]}
    out = _apply_staged(doc, staged)
    out["tags"][1]["z"] = 9
    assert staged == {"tags": [2, {"z": 2}]}


def test_compose_of_a_create_shares_nothing_with_the_entry():
    entry = {"action": "create", "staged": _doc()}
    out = compose(entry, None)
    out["metadata"]["k"]["v"] = 9
    assert entry["staged"]["metadata"]["k"]["v"] == 1


def test_content_copies_deeply():
    doc = _doc()
    out = sr._content(doc)
    out["metadata"]["k"]["v"] = 9
    assert doc["metadata"]["k"]["v"] == 1


def test_with_manual_does_not_alias_doc_or_values():
    doc = _doc()
    values = {"metadata.m": {"deep": {"x": 1}}, "tags": [{"q": 1}]}
    out = sr._with_manual(doc, values, ["metadata.m", "tags"])
    out["metadata"]["k"]["v"] = 9
    out["metadata"]["m"]["deep"]["x"] = 9
    out["tags"][0]["q"] = 9
    assert doc["metadata"]["k"]["v"] == 1
    assert values == {"metadata.m": {"deep": {"x": 1}}, "tags": [{"q": 1}]}


def test_node_and_edge_builders_do_not_alias_the_document():
    doc = _doc()
    del doc["tags"]
    n = _node(doc)
    n.metadata["k"]["v"] = 9
    assert doc["metadata"]["k"]["v"] == 1
    e = {
        "id": "e",
        "source": "a",
        "target": "b",
        "type": "RELATES_TO",
        "metadata": {"k": {"v": 1}},
    }
    ed = _edge(e)
    ed.metadata["k"]["v"] = 9
    assert e["metadata"]["k"]["v"] == 1
