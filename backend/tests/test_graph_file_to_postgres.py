import copy
import json

import pytest

from scripts import graph_file_to_postgres
from scripts.graph_file_to_postgres import (
    ConversionError,
    convert_graph_file_to_postgres,
    main,
)


def _graph(*, nodes=None, edges=None, metadata=None):
    return {
        "nodes": list(nodes or []),
        "edges": list(edges or []),
        "metadata": dict(metadata or {}),
    }


def _node(node_id):
    return {"id": node_id, "type": "Thing", "name": node_id}


def _edge(edge_id, source, target):
    return {"id": edge_id, "type": "RELATED_TO", "source": source, "target": target}


def _write_graph(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


class MemoryTarget:
    def __init__(self, initial=None, *, corrupt_after_save=None):
        self.data = copy.deepcopy(initial)
        self.corrupt_after_save = corrupt_after_save
        self.closed = False

    def exists(self):
        return self.data is not None

    def load_graph_data(self):
        return copy.deepcopy(self.data or _graph())

    def save_graph_data(self, data):
        self.data = copy.deepcopy(data)
        if self.corrupt_after_save is not None:
            self.corrupt_after_save(self.data)

    def close(self):
        self.closed = True


def test_copies_graph_json_into_empty_target(tmp_path):
    source = tmp_path / "graph.json"
    data = _graph(
        nodes=[_node("a"), _node("b")],
        edges=[_edge("ab", "a", "b")],
        metadata={"graph_name": "Example"},
    )
    _write_graph(source, data)
    target = MemoryTarget()

    result = convert_graph_file_to_postgres(source, target)

    assert result.nodes == 2
    assert result.edges == 1
    assert target.load_graph_data() == data


def test_refuses_non_empty_target_without_explicit_flag(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("source")]))
    target = MemoryTarget(_graph(nodes=[_node("existing")]))

    with pytest.raises(ConversionError, match="target graph is not empty"):
        convert_graph_file_to_postgres(source, target)

    assert target.load_graph_data()["nodes"] == [_node("existing")]


def test_explicit_flag_replaces_non_empty_target(tmp_path):
    source = tmp_path / "graph.json"
    replacement = _graph(nodes=[_node("source")])
    _write_graph(source, replacement)
    target = MemoryTarget(_graph(nodes=[_node("existing")]))

    convert_graph_file_to_postgres(source, target, allow_non_empty=True)

    assert target.load_graph_data() == replacement


def test_refuses_source_with_missing_edge_endpoint(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")], edges=[_edge("bad", "a", "z")]))
    target = MemoryTarget()

    with pytest.raises(ConversionError, match="source graph has 1 edge endpoint"):
        convert_graph_file_to_postgres(source, target)

    assert not target.exists()


def test_verifies_target_counts_after_write(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a"), _node("b")]))

    def drop_node(data):
        data["nodes"].pop()

    target = MemoryTarget(corrupt_after_save=drop_node)

    with pytest.raises(ConversionError, match="target count verification failed"):
        convert_graph_file_to_postgres(source, target)


def test_verifies_target_edge_endpoints_after_write(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(
        source,
        _graph(nodes=[_node("a"), _node("b")], edges=[_edge("ab", "a", "b")]),
    )

    def break_edge(data):
        data["edges"][0]["target"] = "missing"

    target = MemoryTarget(corrupt_after_save=break_edge)

    with pytest.raises(ConversionError, match="target graph has 1 edge endpoint"):
        convert_graph_file_to_postgres(source, target)


def test_cli_closes_backend_and_reports_sidecars_not_migrated(tmp_path, capsys):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    made = []

    def factory(dsn, *, schema, pool_size, scope):
        made.append(MemoryTarget())
        assert dsn == "postgresql://example/db"
        assert schema == "graph"
        assert pool_size is None
        assert scope is None
        return made[0]

    status = main(
        [str(source), "--dsn", "postgresql://example/db", "--schema", "graph"],
        backend_factory=factory,
    )

    assert status == 0
    assert made[0].closed
    assert (
        "Embedding sidecars, history sidecars, and session files were not migrated"
        in (capsys.readouterr().out)
    )


def test_cli_reports_postgres_dependency_error(monkeypatch, tmp_path, capsys):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph())
    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "backend.core.postgres_backend":
            raise ModuleNotFoundError(
                "No module named 'psycopg_pool'", name="psycopg_pool"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    status = graph_file_to_postgres.main(
        [str(source), "--dsn", "postgresql://example/db"]
    )

    assert status == 1
    assert "requirements-postgres.txt" in capsys.readouterr().out
