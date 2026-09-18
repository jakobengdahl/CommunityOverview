import copy
import json

import pytest

from scripts import graph_file_to_postgres
from scripts.graph_file_to_postgres import (
    ConversionError,
    GraphCounts,
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


class FakeInspector:
    """Stands in for the catalog: the isolation the target shows, and how
    many rows it holds carrying each scope."""

    def __init__(self, evidence=(), in_scope=None):
        self.evidence = list(evidence)
        self.in_scope = dict(in_scope or {})
        self.asked_scopes = []

    def isolation_evidence(self):
        return list(self.evidence)

    def rows_in_scope(self, scope):
        self.asked_scopes.append(scope)
        return self.in_scope.get(scope, GraphCounts(0, 0))


RLS = "graph_nodes has row-level security enabled and a policy"


def test_copies_graph_json_into_empty_target(tmp_path):
    source = tmp_path / "graph.json"
    data = _graph(
        nodes=[_node("a"), _node("b")],
        edges=[_edge("ab", "a", "b")],
        metadata={"graph_name": "Example"},
    )
    _write_graph(source, data)
    target = MemoryTarget()

    result = convert_graph_file_to_postgres(source, target, inspector=FakeInspector())

    assert result.nodes == 2
    assert result.edges == 1
    assert target.load_graph_data() == data


def test_refuses_non_empty_target_without_explicit_flag(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("source")]))
    target = MemoryTarget(_graph(nodes=[_node("existing")]))

    with pytest.raises(ConversionError, match="target graph is not empty"):
        convert_graph_file_to_postgres(source, target, inspector=FakeInspector())

    assert target.load_graph_data()["nodes"] == [_node("existing")]


def test_explicit_flag_replaces_non_empty_target(tmp_path):
    source = tmp_path / "graph.json"
    replacement = _graph(nodes=[_node("source")])
    _write_graph(source, replacement)
    target = MemoryTarget(_graph(nodes=[_node("existing")]))

    convert_graph_file_to_postgres(
        source, target, inspector=FakeInspector(), allow_non_empty=True
    )

    assert target.load_graph_data() == replacement


def test_refuses_source_with_missing_edge_endpoint(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")], edges=[_edge("bad", "a", "z")]))
    target = MemoryTarget()

    with pytest.raises(ConversionError, match="source graph has 1 edge endpoint"):
        convert_graph_file_to_postgres(source, target, inspector=FakeInspector())

    assert not target.exists()


def test_verifies_target_counts_after_write(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a"), _node("b")]))

    def drop_node(data):
        data["nodes"].pop()

    target = MemoryTarget(corrupt_after_save=drop_node)

    with pytest.raises(ConversionError, match="target count verification failed"):
        convert_graph_file_to_postgres(source, target, inspector=FakeInspector())


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
        convert_graph_file_to_postgres(source, target, inspector=FakeInspector())


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
        inspector_factory=lambda dsn, *, schema: FakeInspector(),
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


# A target that keeps scopes apart. A row written without a scope is admitted
# to every session, so the unscoped conversion is refused rather than run.


def test_unscoped_conversion_refuses_a_target_that_keeps_scopes_apart(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    target = MemoryTarget()

    with pytest.raises(ConversionError, match="--scope") as refused:
        convert_graph_file_to_postgres(
            source, target, inspector=FakeInspector(evidence=[RLS])
        )

    assert RLS in str(refused.value)
    assert not target.exists()


def test_allow_non_empty_does_not_override_the_scope_refusal(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    # Metadata alone: what an unscoped session sees of a store whose rows
    # all belong to scopes, and exactly what that flag was offered to replace.
    before = _graph(metadata={"graph_name": "Another scope's"})
    target = MemoryTarget(before)

    with pytest.raises(ConversionError, match="keeps scopes apart"):
        convert_graph_file_to_postgres(
            source,
            target,
            inspector=FakeInspector(evidence=[RLS]),
            allow_non_empty=True,
        )

    assert target.load_graph_data() == before


def test_scoped_conversion_is_not_refused_by_that_isolation(tmp_path):
    source = tmp_path / "graph.json"
    data = _graph(nodes=[_node("a"), _node("b")], edges=[_edge("ab", "a", "b")])
    _write_graph(source, data)
    target = MemoryTarget()
    inspector = FakeInspector(evidence=[RLS], in_scope={"s1": GraphCounts(2, 1)})

    result = convert_graph_file_to_postgres(
        source, target, inspector=inspector, scope="s1"
    )

    assert (result.nodes, result.edges) == (2, 1)
    assert target.load_graph_data() == data


def test_a_metadata_only_target_is_described_as_that(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    target = MemoryTarget(_graph(metadata={"graph_name": "Existing"}))

    with pytest.raises(ConversionError) as refused:
        convert_graph_file_to_postgres(source, target, inspector=FakeInspector())

    assert "graph metadata but no nodes or edges" in str(refused.value)
    assert "0 node(s)" not in str(refused.value)


@pytest.mark.parametrize(
    "nodes, edges, in_scope",
    [
        (["a", "b"], [("ab", "a", "b")], GraphCounts(0, 0)),
        (["a", "b"], [("ab", "a", "b")], GraphCounts(2, 0)),
        (["a", "b"], [("ab", "a", "b")], GraphCounts(1, 1)),
        ([], [], GraphCounts(1, 0)),
    ],
    ids=["none-stamped", "edges-unstamped", "partly-stamped", "empty-source"],
)
def test_scoped_verification_counts_rows_carrying_the_scope(
    tmp_path, nodes, edges, in_scope
):
    """The reload shows the right counts, and the rows still do not carry
    the scope - a scoped reader is also shown every row that carries none.
    Any mismatch fails, in either count and in either direction."""
    source = tmp_path / "graph.json"
    _write_graph(
        source,
        _graph(nodes=[_node(n) for n in nodes], edges=[_edge(*e) for e in edges]),
    )
    inspector = FakeInspector(in_scope={"s1": in_scope})

    with pytest.raises(ConversionError, match="scope verification failed"):
        convert_graph_file_to_postgres(
            source, MemoryTarget(), inspector=inspector, scope="s1"
        )

    assert inspector.asked_scopes == ["s1"]


def test_a_scoped_conversion_finding_only_metadata_does_not_offer_to_replace_it(
    tmp_path,
):
    """Metadata and none of this scope's rows is, on a shared schema, another
    scope's graph: the metadata row is one per schema. Suggesting the flag
    here is what overwrote it."""
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    before = _graph(metadata={"graph_name": "Another scope's"})
    target = MemoryTarget(before)

    with pytest.raises(ConversionError) as refused:
        convert_graph_file_to_postgres(
            source, target, inspector=FakeInspector(), scope="s1"
        )

    assert "shared by every scope in the schema" in str(refused.value)
    assert "re-run with --allow-non-empty-target" not in str(refused.value)
    assert target.load_graph_data() == before


def test_edges_without_nodes_are_reported_as_edges(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    target = MemoryTarget(_graph(edges=[_edge("xy", "x", "y")]))

    with pytest.raises(ConversionError, match=r"0 node\(s\), 1 edge\(s\)"):
        convert_graph_file_to_postgres(source, target, inspector=FakeInspector())


def test_cli_inspects_the_target_it_writes_and_passes_the_scope_through(tmp_path):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    inspectors = []

    def inspector_factory(dsn, *, schema):
        assert (dsn, schema) == ("postgresql://example/db", "graph")
        inspectors.append(FakeInspector(in_scope={"s1": GraphCounts(1, 0)}))
        return inspectors[0]

    def backend_factory(dsn, *, schema, pool_size, scope):
        assert scope == "s1"
        return MemoryTarget()

    status = main(
        [
            str(source),
            "--dsn",
            "postgresql://example/db",
            "--schema",
            "graph",
            "--scope",
            "s1",
        ],
        backend_factory=backend_factory,
        inspector_factory=inspector_factory,
    )

    assert status == 0
    assert inspectors[0].asked_scopes == ["s1"]


def test_cli_refuses_an_unscoped_conversion_into_a_scoped_target(tmp_path, capsys):
    source = tmp_path / "graph.json"
    _write_graph(source, _graph(nodes=[_node("a")]))
    made = []

    def backend_factory(dsn, *, schema, pool_size, scope):
        made.append(MemoryTarget())
        return made[0]

    status = main(
        [str(source), "--dsn", "postgresql://example/db", "--allow-non-empty-target"],
        backend_factory=backend_factory,
        inspector_factory=lambda dsn, *, schema: FakeInspector(evidence=[RLS]),
    )

    assert status == 1
    assert "keeps scopes apart" in capsys.readouterr().out
    assert not made[0].exists()
    assert made[0].closed
