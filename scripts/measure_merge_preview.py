#!/usr/bin/env python3
"""Measure the read-only merge preview (ADR 0013 §8, slice 1).

Plans a 10,000-node document against a 100,000-node graph built with the
`scripts/measure_capacity.py` fixture, through the same service call
`POST /import/merge/preview` makes, and reports:

- `preview_ms`: median wall time of a preview, first run discarded;
- `preview_peak_mb`: peak Python allocation during one preview, traced with
  `tracemalloc` (memory beyond the resident graph; tracing slows the run,
  so it is a separate run from the latency one).

The graph is seeded through the file backend in this process: unlike the
capacity envelope this measures what a preview adds on top of a loaded
graph, not what the graph costs to hold, so the fixture staying reachable
does not distort either figure.

The document mixes the cases a preview meets: half its nodes collide with
live ids (overwritten, half of them with changed text), half are new; edges
join new nodes to each other and to live nodes, and some repeat a live
edge's triple under a new id (duplicates).

Usage:

    python3 scripts/measure_merge_preview.py
    python3 scripts/measure_merge_preview.py --graph 20000 --document 2000
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from measure_capacity import _build_graph  # noqa: E402


def _document(graph: Dict[str, Any], size: int, seed: int = 2) -> Dict[str, Any]:
    rng = random.Random(seed)
    live_nodes = graph["nodes"]
    half = size // 2
    nodes = []
    for raw in rng.sample(live_nodes, half):
        node = dict(raw)
        if rng.random() < 0.5:
            node["description"] = raw["description"] + " revised"
        nodes.append(node)
    new_ids = [f"m{i}" for i in range(size - half)]
    nodes.extend(
        {"id": node_id, "type": "Actor", "name": f"merged {node_id}"}
        for node_id in new_ids
    )

    edges = []
    for i in range(int(size * 1.7)):
        source = rng.choice(new_ids)
        target = rng.choice(new_ids) if i % 2 else rng.choice(live_nodes)["id"]
        if source != target:
            edges.append(
                {
                    "id": f"me{i}",
                    "source": source,
                    "target": target,
                    "type": "RELATES_TO",
                }
            )
    for raw in rng.sample(graph["edges"], size // 10):
        edges.append(dict(raw, id=f"dup-{raw['id']}"))
    return {"nodes": nodes, "edges": edges}


def measure(graph_size: int, document_size: int, repeats: int) -> Dict[str, Any]:
    from backend.core.storage import GraphStorage
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from backend.service import GraphService

    graph = _build_graph(graph_size)
    graph.pop("_hub")
    document = _document(graph, document_size)
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "graph.json"
        FileGraphPersistenceBackend(path).save_graph_data(graph)
        storage = GraphStorage(persistence_backend=FileGraphPersistenceBackend(path))
        service = GraphService(storage)
        body = {"document": document, "policy": {"default": "overwrite"}}

        samples = []
        result: Dict[str, Any] = {}
        for _ in range(repeats + 1):
            started = time.perf_counter()
            result = service.preview_merge_import(body)
            samples.append((time.perf_counter() - started) * 1000)
        assert result.get("success"), result
        # A blocked plan (e.g. the schema not found, so every node rejected)
        # is cheap and would make any threshold look met.
        plan = result["plan"]
        assert plan["committable"], plan["rejected"]
        assert plan["adds"]["nodes"]["count"] and plan["overwrites"]["nodes"]["count"]

        tracemalloc.start()
        service.preview_merge_import(body)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        storage.shutdown_events()

    plan = result["plan"]
    return {
        "graph_nodes": graph_size,
        "graph_edges": len(graph["edges"]),
        "document_nodes": len(document["nodes"]),
        "document_edges": len(document["edges"]),
        "overwrites": plan["overwrites"]["nodes"]["count"],
        "adds": plan["adds"]["nodes"]["count"] + plan["adds"]["edges"]["count"],
        "skips": plan["skips"]["count"],
        "preview_ms": round(statistics.median(samples[1:]), 1),
        "preview_peak_mb": round(peak / 1024 / 1024, 1),
    }


def main() -> int:
    # The schema config is resolved from the working directory.
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=int, default=100_000)
    parser.add_argument("--document", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(measure(args.graph, args.document, args.repeats), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
