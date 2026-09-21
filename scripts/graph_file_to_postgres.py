#!/usr/bin/env python3
"""Copy a graph JSON snapshot into the PostgreSQL graph backend.

This migrates only the graph payload read from ``graph.json``. Embedding
sidecars, history sidecars, and session files are intentionally not migrated.

A target that keeps scopes apart is only written with ``--scope``. A row that
carries no scope is admitted to every session, so an unscoped conversion into
such a store would publish the whole graph to every scope in it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class ConversionError(RuntimeError):
    """A graph conversion precondition or verification failed."""


class GraphBackend(Protocol):
    def exists(self) -> bool: ...

    def load_graph_data(self) -> Dict[str, Any]: ...

    def save_graph_data(self, data: Dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class TargetInspector(Protocol):
    """What the backend's own load and save cannot tell the conversion.

    Both questions concern rows this conversion's session may not see: the
    backend's own scope predicate hides another scope's rows, with a policy
    as a second, separate layer, and a scoped reader sees rows that carry no
    scope alongside its own. So they are asked of the catalog and of the
    scope column directly rather than of ``load_graph_data``.
    """

    def isolation_evidence(self) -> list[str]: ...

    def rows_in_scope(self, scope: str) -> "GraphCounts": ...


@dataclass(frozen=True)
class GraphCounts:
    nodes: int
    edges: int


@dataclass(frozen=True)
class ConversionResult:
    nodes: int
    edges: int


def _entity_id(entity: Any) -> Any:
    return entity.get("id") if isinstance(entity, dict) else None


def _edge_endpoint(edge: Dict[str, Any], field: str) -> Any:
    return edge.get(field)


def graph_counts(data: Dict[str, Any]) -> GraphCounts:
    return GraphCounts(
        nodes=len(data.get("nodes") or []),
        edges=len(data.get("edges") or []),
    )


def graph_has_content(data: Dict[str, Any]) -> bool:
    if graph_counts(data) != GraphCounts(0, 0):
        return True
    metadata = data.get("metadata")
    return bool(metadata) if isinstance(metadata, dict) else metadata is not None


def not_empty_refusal(existing: Dict[str, Any], scope: str | None) -> str:
    counts = graph_counts(existing)
    if counts != GraphCounts(0, 0):
        found = f"{counts.nodes} node(s), {counts.edges} edge(s)"
    else:
        found = "graph metadata but no nodes or edges"
    if scope is None:
        return (
            f"target graph is not empty (it holds {found}); re-run with "
            "--allow-non-empty-target to replace it"
        )
    # Under a scope, what the load returned does not settle whether the schema
    # is the scope's own - which an application start leaves holding an empty
    # graph's metadata - or shared. A scoped load returns only rows carrying
    # no scope and this scope's own, whatever the role: the backend filters
    # every read itself, and a policy is a second layer on top. And rows
    # carrying no scope look alike whoever wrote them. So it says what the
    # flag would reach and leaves the call to the operator rather than
    # guessing.
    return (
        f"target graph is not empty (this scope sees {found}); rows carrying "
        "no scope and the metadata row are shared by every scope in the "
        "schema, and --allow-non-empty-target replaces them for all of them - "
        "pass it only if this schema holds no graph but this scope's"
    )


def validate_edge_endpoints(data: Dict[str, Any], *, label: str) -> None:
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    node_ids = {_entity_id(node) for node in nodes if _entity_id(node) is not None}
    missing = []
    for edge in edges:
        if not isinstance(edge, dict):
            missing.append(("<unknown>", "<edge is not an object>"))
            continue
        edge_id = edge.get("id", "<unknown>")
        for field in ("source", "target"):
            endpoint = _edge_endpoint(edge, field)
            if endpoint not in node_ids:
                missing.append((edge_id, f"{field}={endpoint!r}"))
    if missing:
        examples = ", ".join(
            f"{edge_id} ({endpoint})" for edge_id, endpoint in missing[:5]
        )
        extra = "" if len(missing) <= 5 else f", plus {len(missing) - 5} more"
        raise ConversionError(
            f"{label} has {len(missing)} edge endpoint(s) that do not refer "
            f"to a node: {examples}{extra}"
        )


def verify_written_graph(source: Dict[str, Any], written: Dict[str, Any]) -> None:
    source_counts = graph_counts(source)
    written_counts = graph_counts(written)
    if written_counts != source_counts:
        raise ConversionError(
            "target count verification failed: "
            f"expected {source_counts.nodes} node(s) and {source_counts.edges} "
            f"edge(s), found {written_counts.nodes} node(s) and "
            f"{written_counts.edges} edge(s)"
        )
    validate_edge_endpoints(written, label="target graph")


def read_graph_file(graph_file: str | Path) -> Dict[str, Any]:
    path = Path(graph_file)
    if not path.exists():
        raise ConversionError(f"source graph does not exist: {graph_file}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConversionError(f"source graph must be a JSON object: {graph_file}")
    return data


def convert_graph_file_to_postgres(
    graph_file: str | Path,
    target_backend: GraphBackend,
    *,
    inspector: TargetInspector,
    scope: str | None = None,
    allow_non_empty: bool = False,
) -> ConversionResult:
    source = read_graph_file(graph_file)
    validate_edge_endpoints(source, label="source graph")

    # Before the emptiness check, so --allow-non-empty-target cannot reach
    # past it: that flag answers "replace this graph?", not "publish it to
    # every scope?", and on such a store the emptiness check sees only the
    # shared metadata row anyway.
    if scope is None:
        evidence = inspector.isolation_evidence()
        if evidence:
            raise ConversionError(
                "target keeps scopes apart ("
                + "; ".join(evidence)
                + "), and rows written without a scope are readable by every "
                "scope in it; re-run with --scope set to the scope this graph "
                "belongs to"
            )

    if target_backend.exists():
        existing = target_backend.load_graph_data()
        if graph_has_content(existing) and not allow_non_empty:
            raise ConversionError(not_empty_refusal(existing, scope))

    target_backend.save_graph_data(source)
    written = target_backend.load_graph_data()
    verify_written_graph(source, written)

    if scope is not None:
        # The reload above cannot show this: a scoped reader is also shown
        # every row that carries no scope, so a graph written unscoped would
        # pass the count check with the right numbers.
        expected = graph_counts(source)
        stamped = inspector.rows_in_scope(scope)
        if stamped != expected:
            raise ConversionError(
                "scope verification failed: expected "
                f"{expected.nodes} node(s) and {expected.edges} edge(s) carrying "
                f"the scope, found {stamped.nodes} node(s) and {stamped.edges} "
                "edge(s)"
            )

    counts = graph_counts(written)
    return ConversionResult(nodes=counts.nodes, edges=counts.edges)


def build_postgres_backend(
    dsn: str,
    *,
    schema: str,
    pool_size: int | None = None,
    scope: str | None = None,
) -> GraphBackend:
    try:
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
    except ModuleNotFoundError as exc:
        if exc.name in {"psycopg", "psycopg_pool"}:
            raise ConversionError(
                "PostgreSQL dependencies are not installed; install "
                "requirements-postgres.txt before running this conversion"
            ) from exc
        raise

    kwargs: Dict[str, Any] = {"schema": schema}
    if pool_size is not None:
        kwargs["pool_size"] = pool_size
    if scope is not None:
        kwargs["scope"] = scope
    return PostgresGraphPersistenceBackend(dsn, **kwargs)


class PostgresTargetInspector:
    """Answers `TargetInspector` from the catalog, on its own connection.

    Its own connection rather than the backend's pool, which is private: the
    session it opens carries no scope setting, exactly as the conversion's
    own does when no scope was given.
    """

    def __init__(self, dsn: str, *, schema: str) -> None:
        self._dsn = dsn
        self._schema = schema

    def isolation_evidence(self) -> list[str]:
        import psycopg
        from psycopg import sql

        from backend.core.postgres_backend import SCOPE_COLUMN, SCOPED_TABLES

        evidence = []
        with psycopg.connect(self._dsn) as conn:
            for table in SCOPED_TABLES:
                row = conn.execute(
                    "SELECT c.relrowsecurity,"
                    " EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid),"
                    " EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid"
                    "   AND a.attname = %s AND a.attnum > 0 AND NOT a.attisdropped)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND c.relname = %s",
                    (SCOPE_COLUMN, self._schema, table),
                ).fetchone()
                if row is None:
                    continue
                rls_enabled, has_policy, has_column = row
                # Either one is a store asking the server to keep rows apart,
                # whatever the policy is called: an operator provisioning by
                # hand is not bound to the backend's name for it.
                if rls_enabled or has_policy:
                    found = [
                        sign
                        for sign, present in (
                            ("row-level security enabled", rls_enabled),
                            ("a policy", has_policy),
                        )
                        if present
                    ]
                    evidence.append(f"{table} has " + " and ".join(found))
                elif has_column:
                    # No policy, so nothing hides a scoped row from this
                    # session: a store keeping scopes apart in the
                    # application alone shows them here.
                    scoped = conn.execute(
                        sql.SQL(
                            "SELECT count(*) FROM {}.{} WHERE {} IS NOT NULL"
                        ).format(
                            sql.Identifier(self._schema),
                            sql.Identifier(table),
                            sql.Identifier(SCOPE_COLUMN),
                        )
                    ).fetchone()[0]
                    if scoped:
                        evidence.append(f"{scoped} row(s) in {table} carry a scope")
        return evidence

    def rows_in_scope(self, scope: str) -> GraphCounts:
        import psycopg
        from psycopg import sql

        from backend.core.postgres_backend import (
            SCOPE_COLUMN,
            SCOPE_SETTING,
            SCOPED_TABLES,
        )

        counts = {}
        with psycopg.connect(self._dsn) as conn:
            with conn.transaction():
                # A forced policy hides a scoped row from a session that has
                # not said which scope it is, this one included.
                conn.execute("SELECT set_config(%s, %s, true)", (SCOPE_SETTING, scope))
                for table in SCOPED_TABLES:
                    counts[table] = conn.execute(
                        sql.SQL("SELECT count(*) FROM {}.{} WHERE {} = %s").format(
                            sql.Identifier(self._schema),
                            sql.Identifier(table),
                            sql.Identifier(SCOPE_COLUMN),
                        ),
                        (scope,),
                    ).fetchone()[0]
        return GraphCounts(nodes=counts["graph_nodes"], edges=counts["graph_edges"])


def build_postgres_inspector(dsn: str, *, schema: str) -> TargetInspector:
    return PostgresTargetInspector(dsn, schema=schema)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy a graph.json file into the PostgreSQL graph backend. "
            "Embedding sidecars, history sidecars, and session files are not "
            "migrated."
        )
    )
    parser.add_argument("graph_file", help="Path to the source graph.json file")
    parser.add_argument(
        "--dsn",
        required=True,
        help="PostgreSQL libpq connection string for the target backend",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Target schema (default: public)",
    )
    parser.add_argument(
        "--pool-size",
        type=_positive_int,
        default=None,
        help="PostgreSQL connection pool size for this conversion",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help=(
            "Opaque scope identifier stamped on every row written. Required "
            "when the target keeps scopes apart; the conversion refuses "
            "without it"
        ),
    )
    parser.add_argument(
        "--allow-non-empty-target",
        action="store_true",
        help="Replace a target graph that already contains data",
    )
    return parser.parse_args(argv)


def main(
    argv: Iterable[str] | None = None,
    *,
    backend_factory: Callable[..., GraphBackend] = build_postgres_backend,
    inspector_factory: Callable[..., TargetInspector] = build_postgres_inspector,
) -> int:
    args = parse_args(argv)
    backend = None
    try:
        backend = backend_factory(
            args.dsn,
            schema=args.schema,
            pool_size=args.pool_size,
            scope=args.scope,
        )
        result = convert_graph_file_to_postgres(
            args.graph_file,
            backend,
            inspector=inspector_factory(args.dsn, schema=args.schema),
            scope=args.scope,
            allow_non_empty=args.allow_non_empty_target,
        )
    except ConversionError as exc:
        print(f"Refusing conversion: {exc}")
        return 1
    finally:
        if backend is not None:
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    print(f"Converted {result.nodes} node(s) and {result.edges} edge(s) to PostgreSQL.")
    print("Embedding sidecars, history sidecars, and session files were not migrated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
