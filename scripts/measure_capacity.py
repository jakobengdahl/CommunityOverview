#!/usr/bin/env python3
"""Measure what one instance holds, and what it costs to serve it.

This is the maintained form of the capacity envelope: re-run it rather than
quoting a number somebody measured once. A figure in `docs/CAPACITY.md` that
this script cannot reproduce is stale, and the document says so.

Each (backend, size) pair is measured in a FRESH SUBPROCESS. That is not
tidiness: resident memory is the number the envelope turns on, and a process
that has already built one graph carries its allocator's free lists, the
import graph of whatever ran before, and any cache a previous size warmed.
Measuring two sizes in one process makes the second look cheaper than it is.
The worker prints one JSON line; the driver aggregates.

Usage:

    python3 scripts/measure_capacity.py                         # file backend
    python3 scripts/measure_capacity.py --sizes 5000,20000
    CO_TEST_POSTGRES_DSN=... python3 scripts/measure_capacity.py --postgres

What it does NOT measure, and why: semantic search. The base install carries
no ML stack by deliberate policy (`requirements-ml.txt` is separate), so
`VectorStore` falls back to its mock path here exactly as it does in CI. A
semantic number measured against the mock would be a number about the mock.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_SIZES = (5_000, 20_000, 50_000)

# The Corp field profile: how many edges a real graph carries per node. Used
# so the envelope describes a graph of the shape deployments actually hold
# rather than one chosen to make the numbers look good.
EDGES_PER_NODE = 1.7

# Roughly the payload a real node carries - a few-word name and a couple of
# sentences of description. Node cost is dominated by the payload, not by the
# topology, so a fixture with empty descriptions would understate memory by
# the thing that actually fills it.
WORDS = (
    "statistics register variable population dataset classification survey "
    "metadata quality indicator collection reference frame unit measure"
).split()


def _rss_bytes() -> int:
    """Resident set size, read from the kernel rather than from the allocator.

    `sys.getsizeof` and friends measure what Python thinks it holds;
    deployments are killed on what the kernel thinks the process holds.
    """
    with open("/proc/self/statm") as handle:
        pages = int(handle.read().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE")


def _build_graph(size: int, seed: int = 1) -> Dict[str, Any]:
    """A graph of `size` nodes with preferential attachment.

    Preferential attachment rather than uniform random wiring, because it is
    what real graphs look like: a few hubs and a long tail. Traversal cost
    from a hub is the worst case an interactive canvas actually meets, and
    uniform wiring has no hubs to find it with.
    """
    rng = random.Random(seed)
    nodes = []
    for i in range(size):
        nodes.append(
            {
                "id": f"n{i}",
                "type": "Actor",
                "name": " ".join(rng.sample(WORDS, 4)),
                "description": " ".join(rng.choices(WORDS, k=25)),
                "tags": rng.sample(WORDS, 2),
            }
        )

    edges = []
    degree = [1] * size
    targets: List[int] = list(range(min(size, 10)))
    for i in range(1, int(size * EDGES_PER_NODE)):
        source = i % size
        target = rng.choice(targets)
        if source == target:
            continue
        edges.append(
            {
                "id": f"e{len(edges)}",
                "source": f"n{source}",
                "target": f"n{target}",
                "type": "RELATES_TO",
            }
        )
        degree[target] += 1
        targets.append(target)
        targets.append(source)

    return {
        "nodes": nodes,
        "edges": edges,
        "metadata": {"version": "1.0", "graph_name": "capacity"},
        # The most connected node, so the traversal worst case is measurable
        # rather than guessed at.
        "_hub": f"n{max(range(size), key=lambda i: degree[i])}",
    }


def _time(fn, repeats: int = 5) -> float:
    """Median milliseconds over `repeats`, discarding the first run.

    The first call pays for whatever the path lazily builds; the median of the
    rest is what a request meets. Reporting the mean would let one stall
    dominate, and reporting the minimum would describe a machine at rest.
    """
    fn()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def measure_one(backend_kind: str, size: int, dsn: str | None) -> Dict[str, Any]:
    """Measure one (backend, size) pair. Runs in its own process."""
    from backend.core.storage import GraphStorage
    from backend.core.storage_backends import FileGraphPersistenceBackend

    data = _build_graph(size)
    hub = data.pop("_hub")
    edge_count = len(data["edges"])

    workdir = tempfile.mkdtemp(prefix="capacity-")
    schema = None
    pg_backend = None

    if backend_kind == "file":
        path = Path(workdir) / "graph.json"
        make_backend = lambda: FileGraphPersistenceBackend(path)  # noqa: E731
    else:
        import uuid

        import psycopg

        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        schema = f"cap_{uuid.uuid4().hex[:12]}"
        make_backend = lambda: PostgresGraphPersistenceBackend(  # noqa: E731
            dsn, schema=schema
        )

    try:
        # --- write the fixture, and time the snapshot while doing it --------
        backend = make_backend()
        started = time.perf_counter()
        backend.save_graph_data(data)
        snapshot_ms = (time.perf_counter() - started) * 1000
        if backend_kind == "postgres":
            pg_backend = backend

        # --- cold start: a fresh process would do exactly this --------------
        baseline_rss = _rss_bytes()
        started = time.perf_counter()
        storage = GraphStorage(persistence_backend=make_backend())
        cold_start_ms = (time.perf_counter() - started) * 1000
        loaded_rss = _rss_bytes()

        assert len(storage.nodes) == size, (
            f"loaded {len(storage.nodes)} nodes, expected {size} - the "
            f"measurement would describe a graph nobody asked for"
        )

        # --- what a request costs -------------------------------------------
        rare = data["nodes"][size // 2]["name"].split()[0]
        common = WORDS[0]
        result = {
            "backend": backend_kind,
            "nodes": size,
            "edges": edge_count,
            "rss_total_mb": round(loaded_rss / 1024 / 1024, 1),
            "rss_graph_mb": round((loaded_rss - baseline_rss) / 1024 / 1024, 1),
            "bytes_per_node": round((loaded_rss - baseline_rss) / size),
            "cold_start_s": round(cold_start_ms / 1000, 2),
            "snapshot_s": round(snapshot_ms / 1000, 2),
            "search_rare_ms": round(
                _time(lambda: storage.search_nodes(rare, limit=20)), 1
            ),
            "search_common_ms": round(
                _time(lambda: storage.search_nodes(common, limit=20)), 1
            ),
            "traverse_d1_ms": round(
                _time(lambda: storage.get_related_nodes(hub, depth=1)), 1
            ),
            "traverse_d2_ms": round(
                _time(lambda: storage.get_related_nodes(hub, depth=2)), 1
            ),
            "traverse_d3_ms": round(
                _time(lambda: storage.get_related_nodes(hub, depth=3), repeats=3), 1
            ),
        }
        storage.shutdown_events()
        return result
    finally:
        if pg_backend is not None:
            pg_backend.close()
        if schema is not None:
            import psycopg

            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _run_worker(backend_kind: str, size: int, dsn: str | None) -> Dict[str, Any]:
    """Spawn a fresh interpreter for one measurement and read its JSON line."""
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        backend_kind,
        str(size),
    ]
    env = dict(os.environ)
    if dsn:
        env["CO_TEST_POSTGRES_DSN"] = dsn
    completed = subprocess.run(argv, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        raise SystemExit(
            f"measurement failed for {backend_kind} at {size} nodes; "
            "a partial envelope is worse than none, so this stops here"
        )
    for line in completed.stdout.splitlines():
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit(f"worker produced no result for {backend_kind} at {size}")


COLUMNS = (
    ("nodes", "nodes", "{:,}"),
    ("edges", "edges", "{:,}"),
    ("rss_total_mb", "process MB", "{}"),
    ("rss_graph_mb", "graph MB", "{}"),
    ("cold_start_s", "cold start s", "{}"),
    ("snapshot_s", "snapshot s", "{}"),
    ("search_rare_ms", "search rare ms", "{}"),
    ("search_common_ms", "search all ms", "{}"),
    ("traverse_d3_ms", "hub depth 3 ms", "{}"),
)


def _marginal(subset: List[Dict[str, Any]]) -> str:
    """The cost of one more node, separated from the cost of booting at all.

    A per-row `bytes / nodes` ratio is not the node cost: at 2,000 nodes it
    reported 16.5 kB a node against the ~1.9 kB a node actually costs, because
    the fixed cost of an instance - the import graph, the config, NetworkX,
    the empty indexes - was being divided by too few nodes. The slope between
    two sizes cancels that fixed term; the intercept it leaves is the fixed
    cost itself, which is worth naming rather than hiding.
    """
    if len(subset) < 2:
        return "  (needs at least two sizes to separate fixed from marginal cost)"
    low, high = subset[0], subset[-1]
    span = high["nodes"] - low["nodes"]
    if span <= 0:
        return "  (sizes do not differ)"
    slope_mb = (high["rss_graph_mb"] - low["rss_graph_mb"]) / span
    intercept_mb = low["rss_graph_mb"] - slope_mb * low["nodes"]
    per_node = slope_mb * 1024 * 1024
    return (
        f"  marginal: {per_node:,.0f} B per node "
        f"(at {EDGES_PER_NODE} edges a node, so node + its edges), "
        f"measured as the slope from {low['nodes']:,} to {high['nodes']:,}\n"
        f"  fixed:    {intercept_mb:,.0f} MB before the first node - "
        f"the intercept that slope leaves"
    )


def _print_table(rows: List[Dict[str, Any]]) -> None:
    for backend_kind in ("file", "postgres"):
        subset = [r for r in rows if r["backend"] == backend_kind]
        if not subset:
            continue
        print(f"\n## {backend_kind} backend\n")
        headers = [label for _, label, _ in COLUMNS]
        widths = [len(h) for h in headers]
        table = []
        for row in subset:
            cells = [fmt.format(row[key]) for key, _, fmt in COLUMNS]
            widths = [max(w, len(c)) for w, c in zip(widths, cells)]
            table.append(cells)
        print(" | ".join(h.rjust(w) for h, w in zip(headers, widths)))
        print("-+-".join("-" * w for w in widths))
        for cells in table:
            print(" | ".join(c.rjust(w) for c, w in zip(cells, widths)))
        print()
        print(_marginal(subset))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="also measure the PostgreSQL backend (needs CO_TEST_POSTGRES_DSN)",
    )
    parser.add_argument("--json", action="store_true", help="emit raw JSON rows")
    parser.add_argument("--worker", nargs=2, metavar=("BACKEND", "SIZE"))
    args = parser.parse_args()

    if args.worker:
        backend_kind, size = args.worker[0], int(args.worker[1])
        print(
            json.dumps(
                measure_one(backend_kind, size, os.environ.get("CO_TEST_POSTGRES_DSN"))
            )
        )
        return 0

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    backends = ["file"]
    dsn = os.environ.get("CO_TEST_POSTGRES_DSN")
    if args.postgres:
        if not dsn:
            parser.error("--postgres needs CO_TEST_POSTGRES_DSN")
        backends.append("postgres")

    rows = []
    for backend_kind in backends:
        for size in sizes:
            print(f"measuring {backend_kind} at {size:,} nodes ...", file=sys.stderr)
            rows.append(_run_worker(backend_kind, size, dsn))

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
