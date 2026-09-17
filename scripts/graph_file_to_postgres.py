#!/usr/bin/env python3
"""Copy a graph JSON snapshot into the PostgreSQL graph backend.

This migrates only the graph payload read from ``graph.json``. Embedding
sidecars, history sidecars, and session files are intentionally not migrated.
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
    allow_non_empty: bool = False,
) -> ConversionResult:
    source = read_graph_file(graph_file)
    validate_edge_endpoints(source, label="source graph")

    if target_backend.exists():
        existing = target_backend.load_graph_data()
        if graph_has_content(existing) and not allow_non_empty:
            counts = graph_counts(existing)
            raise ConversionError(
                "target graph is not empty "
                f"({counts.nodes} node(s), {counts.edges} edge(s)); re-run with "
                "--allow-non-empty-target to replace it"
            )

    target_backend.save_graph_data(source)
    written = target_backend.load_graph_data()
    verify_written_graph(source, written)

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
        help="Optional opaque scope identifier for the target rows",
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
