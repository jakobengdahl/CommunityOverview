# covers: backend/core/template_store.py
import copy
import sys

import pytest

from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
)
from backend.core.template_store import TemplateError, TemplateStore, validate_template


def document():
    return {
        "schema_version": 1,
        "template_id": "1234-5678-1234-5678",
        "name": "Example",
        "description": "",
        "tags": [],
        "revision": 0,
        "created_by": "author",
        "created_at": "2026-01-01T00:00:00Z",
        "source_session_id": "source",
        "source_session_revision": 2,
        "nodes": [
            {
                "key": "a",
                "source_node_id": "original",
                "position": {"x": 0, "y": 1},
                "hidden": False,
                "shape": {
                    "ref": "saved:card",
                    "inline": {
                        "version": 1,
                        "base": "rectangle",
                        "scale": 1.0,
                        "placement": "top-left",
                        "areas": {"first": "name", "second": None, "third": None},
                    },
                    "builtin": False,
                },
                "snapshot": {
                    "type": "Actor",
                    "name": "One",
                    "description": "",
                    "summary": "",
                    "tags": [],
                    "subtypes": [],
                    "metadata": {},
                },
            }
        ],
        "edges": [
            {
                "source": "a",
                "target": "a",
                "source_edge_id": "e",
                "type": "LINK",
                "label": "",
            }
        ],
        "annotation_document": {
            "schema_version": 1,
            "annotations": [
                {"id": "group", "type": "group", "member_node_ids": ["a"]},
                {
                    "id": "note",
                    "type": "label",
                    "content": {"attachment": {"target_id": "a"}},
                },
            ],
        },
        "bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
        "thumbnail": None,
    }


def test_document_round_trips_without_normalization():
    value = document()
    value["annotation_document"]["annotations"].extend(
        [
            {
                "id": "ref",
                "type": "reference",
                "target_kind": "resource",
                "target": "a",
            },
            {
                "id": "nested-ref",
                "type": "reference",
                "content": {"target_kind": "resource", "target": "a"},
            },
            {
                "id": "url",
                "type": "reference",
                "target_kind": "url",
                "target": "https://example.com",
            },
        ]
    )
    assert validate_template(value) == value
    assert validate_template(value) is not value


def _nested(depth):
    value = {}
    for _ in range(depth):
        value = {"x": value}
    return value


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(schema_version=1.0),
        lambda d: d["annotation_document"].update(schema_version=True),
        lambda d: d["annotation_document"].update(schema_version=1.0),
        lambda d: d["annotation_document"].update(schema_version=2),
        lambda d: d.pop("name"),
        lambda d: d["nodes"].append(copy.deepcopy(d["nodes"][0])),
        lambda d: d["edges"][0].update(target="outside"),
        lambda d: d["annotation_document"]["annotations"][0].update(
            member_node_ids=["outside"]
        ),
        lambda d: d["annotation_document"]["annotations"][1]["content"][
            "attachment"
        ].update(target_id="outside"),
        lambda d: d["annotation_document"]["annotations"].append(
            {
                "id": "ref",
                "type": "reference",
                "target_kind": "resource",
                "target": "outside",
            }
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {
                "id": "ref",
                "type": "reference",
                "content": {"target_kind": "resource", "target": "outside"},
            }
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {
                "id": "ref",
                "type": "reference",
                "target_kind": "resource",
                "target": "outside",
                "content": {"target": "a"},
            }
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {
                "id": "img",
                "type": "image",
                "image": {"url": "https://example.com/x.png"},
                "content": {"image": {"url": "data:image/webp;base64,AAAA"}},
            }
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {"id": "label", "type": "label", "anchor": {"target_id": "outside"}}
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {"id": "label", "type": "label", "anchor": {"target_id": {}}}
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {"id": "label", "type": "label", "anchor": {"target_id": []}}
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {"id": "img", "kind": "image", "image": {"url": "https://example.com/x"}}
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {
                "id": "img",
                "kind": "image",
                "image": {"url": "https://example.com/x"},
                "content": {"type": "label"},
            }
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {
                "id": "ref",
                "kind": "reference",
                "target_kind": "resource",
                "target": "outside",
                "content": {"type": "label"},
            }
        ),
        lambda d: d["annotation_document"]["annotations"].append(
            {"id": "line", "type": "line", "start": {"nodeId": "outside"}}
        ),
        lambda d: d["nodes"][0]["snapshot"].update(type="SavedView"),
        lambda d: d["nodes"][0]["snapshot"]["metadata"].update(secret="x"),
        lambda d: d["nodes"][0]["snapshot"]["metadata"].update(
            color={1: "x", "1": "y"}
        ),
        lambda d: d["annotation_document"]["annotations"][0].update(
            style={1: "x", "1": "y"}
        ),
        lambda d: d["annotation_document"]["annotations"][0].update(
            style=({1: "x", "1": "y"},)
        ),
        lambda d: d.update(description="\ud800"),
        lambda d: d["nodes"][0]["shape"].update(inline=None),
        lambda d: d.update(type_shapes={}),
        lambda d: d["annotation_document"]["annotations"][0].update(
            style=_nested(sys.getrecursionlimit() + 100)
        ),
    ],
)
def test_invalid_document_is_rejected(mutation):
    value = document()
    mutation(value)
    with pytest.raises(TemplateError) as error:
        validate_template(value)
    assert error.value.code == "invalid_document"


@pytest.mark.parametrize(
    ("field", "count", "code"),
    [
        ("nodes", 501, "nodes_limit"),
        ("edges", 2001, "edges_limit"),
        ("annotations", 2001, "annotations_limit"),
    ],
)
def test_limits_reject_the_whole_document(field, count, code):
    value = document()
    target = value["annotation_document"] if field == "annotations" else value
    target[field] = [target[field][0]] * count
    with pytest.raises(TemplateError) as error:
        validate_template(value)
    assert error.value.code == code
    assert len(target[field]) == count


def test_limits_accept_exactly_the_maximum():
    value = document()
    node = value["nodes"][0]
    value["nodes"] = [{**node, "key": f"n{i}"} for i in range(500)]
    value["edges"] = [{**value["edges"][0], "source": "n0", "target": "n0"}] * 2000
    value["annotation_document"]["annotations"] = [
        {"id": f"a{i}", "type": "label"} for i in range(2000)
    ]
    assert validate_template(value) == value


@pytest.mark.parametrize("file_backend", [False, True])
def test_store_revisions_tombstones_and_self_contained_reads(tmp_path, file_backend):
    backend = (
        FileSessionPersistenceBackend(tmp_path)
        if file_backend
        else InMemorySessionPersistenceBackend()
    )
    store = TemplateStore(backend)
    source = document()
    first = store.save(source, 0)
    source["nodes"].clear()
    assert store.get(first["template_id"]) == first
    assert store.list() == [first]
    changed = copy.deepcopy(first)
    changed["name"] = "Changed"
    second = store.save(changed, 1)
    assert second["revision"] == 2
    with pytest.raises(TemplateError):
        store.save(changed, 1)
    with pytest.raises(TemplateError):
        store.delete(second["template_id"], 1)
    assert store.get(second["template_id"]) == second
    store.delete(second["template_id"], 2)
    assert store.get(second["template_id"]) is None
    assert store.list() == []
    with pytest.raises(TemplateError):
        store.save(document(), 0)
    if file_backend:
        assert (
            TemplateStore(FileSessionPersistenceBackend(tmp_path)).get(
                second["template_id"]
            )
            is None
        )


@pytest.mark.parametrize("file_backend", [False, True])
def test_failed_write_leaves_previous_document(tmp_path, monkeypatch, file_backend):
    backend = (
        FileSessionPersistenceBackend(tmp_path)
        if file_backend
        else InMemorySessionPersistenceBackend()
    )
    store = TemplateStore(backend)
    before = store.save(document(), 0)
    changed = copy.deepcopy(before)
    changed["name"] = "Another"

    def fail(*args, **kwargs):
        raise OSError("interrupted")

    monkeypatch.setattr(backend, "_replace_locked" if file_backend else "save", fail)
    with pytest.raises(OSError):
        store.save(changed, 1)
    assert store.get(before["template_id"]) == before


def test_directory_sync_failure_restores_previous_file(tmp_path, monkeypatch):
    from backend.core import session_store

    store = TemplateStore(FileSessionPersistenceBackend(tmp_path))
    before = store.save(document(), 0)
    changed = copy.deepcopy(before)
    changed["name"] = "Changed"
    real_sync = session_store._fsync_directory
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("interrupted")
        real_sync(path)

    monkeypatch.setattr(session_store, "_fsync_directory", fail_once)
    with pytest.raises(OSError):
        store.save(changed, 1)
    assert store.get(before["template_id"]) == before


def test_profile_templates_are_merged_read_only_and_isolated(tmp_path):
    import json

    profile = tmp_path / "profile"
    profile.mkdir()
    doc = document()
    (profile / "one.json").write_text(json.dumps(doc))
    backend = InMemorySessionPersistenceBackend()
    store = TemplateStore(backend, profile)
    mutable = document()
    mutable["template_id"] = "4321-8765"
    saved = store.save(mutable, 0)
    assert {d["template_id"] for d in store.list()} == {doc["template_id"], "4321-8765"}
    store.get(doc["template_id"])["name"] = "Changed"
    assert store.get(doc["template_id"]) == doc
    for action in (
        lambda: store.save(doc, 0),
        lambda: store.delete(doc["template_id"], 0),
    ):
        with pytest.raises(TemplateError):
            action()
    assert store.get("4321-8765") == saved
    assert backend.load(doc["template_id"]) is None


@pytest.mark.parametrize(
    "problem", ["duplicate", "mutable", "tombstone", "invalid", "json", "utf8", "nested"]
)
def test_profile_loading_rejects_invalid_documents_and_id_collisions(
    tmp_path, problem, caplog
):
    import json

    doc = document()
    backend = InMemorySessionPersistenceBackend()
    if problem in ("mutable", "tombstone"):
        store = TemplateStore(backend)
        saved = store.save(doc, 0)
        if problem == "tombstone":
            store.delete(doc["template_id"], saved["revision"])
    if problem == "invalid":
        doc["schema_version"] = 2
    bad_file = tmp_path / "one.json"
    if problem == "utf8":
        bad_file.write_bytes(b"\xff")
    elif problem == "nested":
        bad_file.write_text("[" * (sys.getrecursionlimit() + 100))
    else:
        bad_file.write_text("{" if problem == "json" else json.dumps(doc))
    if problem == "duplicate":
        (tmp_path / "two.json").write_text(json.dumps(doc))
    if problem in ("duplicate", "mutable", "tombstone"):
        with pytest.raises(TemplateError, match="profile_id_collision"):
            TemplateStore(backend, tmp_path)
    else:
        valid = document()
        (tmp_path / "two.json").write_text(json.dumps(valid))
        store = TemplateStore(backend, tmp_path)
        assert store.list() == [valid]
        assert store.get(valid["template_id"]) == valid
        assert backend.list_ids() == []
        assert str(bad_file) in caplog.text
