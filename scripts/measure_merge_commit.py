#!/usr/bin/env python3
"""Measure the merge import commit (ADR 0013 §8, slice 2).

Commits the `scripts/measure_merge_preview.py` document (10,000 nodes, half
overwriting live nodes, ~18,000 edges) into a 100,000-node graph built with
the `scripts/measure_capacity.py` fixture, through the same service call
`POST /import/merge` makes, and reports per run:

- `commit_ms`: the whole commit request: both plans, the whole-graph
  backup, the locked unit, the job enqueue and the audit record;
- `backup_ms`: the whole-graph backup alone, written before the lock;
- `lock_ms`: how long the commit holds the storage lock
  (`GraphStorage.apply_import_merge`: the plan recomputed under the lock,
  the writes, the one-batch persist and the per-entity events);
- `events`: graph events the commit fired;
- `deliveries_enqueued`: webhook deliveries the commit put on the delivery
  queue, with EventSubscriptions that match every event — the queue
  depth a target slower than the commit sees;
- `delivery_queue_depth`: what was still queued when the commit returned.
  The target is a loopback address the delivery worker's address check
  refuses at once, so this is a floor, not what a real target sees.

Each run seeds a fresh graph through the file backend, since a commit
changes the graph it is measured against. The figures are the median of the
runs.

Usage:

    python3 scripts/measure_merge_commit.py
    python3 scripts/measure_merge_commit.py --graph 20000 --document 2000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from measure_capacity import _build_graph  # noqa: E402
from measure_merge_preview import _document  # noqa: E402

# One subscription per entity kind, so every event the commit fires matches.
_SUBSCRIPTIONS = [
    {
        "id": f"measure-subscription-{kind}",
        "type": "EventSubscription",
        "name": f"Every {kind} event",
        "metadata": {
            "filters": {"target": {"entity_kind": kind}},
            "delivery": {"webhook_url": "http://127.0.0.1:9/merge-measure"},
        },
    }
    for kind in ("node", "edge")
]


def _run(graph: Dict[str, Any], document: Dict[str, Any]) -> Dict[str, Any]:
    from backend.agents.execution import InMemoryExecutionStore
    from backend.core.storage import GraphStorage
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from backend.service import GraphService, import_service

    timings: Dict[str, float] = {}
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "graph.json"
        seeded = dict(graph, nodes=[*graph["nodes"], *_SUBSCRIPTIONS])
        FileGraphPersistenceBackend(path).save_graph_data(seeded)
        storage = GraphStorage(persistence_backend=FileGraphPersistenceBackend(path))
        storage.setup_events(enabled=True)
        worker = storage._delivery_worker
        enqueued = []
        real_enqueue = worker.enqueue
        worker.enqueue = lambda *a, **k: (enqueued.append(1), real_enqueue(*a, **k))
        storage._event_dispatcher.set_delivery_callback(worker.enqueue)
        service = GraphService(storage)
        job_store = InMemoryExecutionStore()

        real_apply = storage.apply_import_merge
        real_backup = import_service._write_backup
        real_drain = import_service._start_embedding_drain

        def timed_apply(*args, **kwargs):
            started = time.perf_counter()
            try:
                return real_apply(*args, **kwargs)
            finally:
                timings["lock_ms"] = (time.perf_counter() - started) * 1000

        def timed_backup(*args, **kwargs):
            started = time.perf_counter()
            try:
                return real_backup(*args, **kwargs)
            finally:
                timings["backup_ms"] = (time.perf_counter() - started) * 1000

        storage.apply_import_merge = timed_apply
        import_service._write_backup = timed_backup
        # Encoding embeddings is the job's, after the response; not measured.
        import_service._start_embedding_drain = lambda *a: None
        try:
            body = {"document": document, "policy": {"default": "overwrite"}}
            digest = service.preview_merge_import(body)["plan_digest"]
            started = time.perf_counter()
            result = service.commit_merge_import(
                {**body, "plan_digest": digest}, job_store
            )
            commit_ms = (time.perf_counter() - started) * 1000
            depth = worker.queue_size
        finally:
            import_service._write_backup = real_backup
            import_service._start_embedding_drain = real_drain
            storage.flush()
            storage.shutdown_events()
        assert result.get("success"), result
        return {
            "commit_ms": commit_ms,
            "backup_ms": timings["backup_ms"],
            "lock_ms": timings["lock_ms"],
            "events": result["counts"]["events"],
            "deliveries_enqueued": len(enqueued),
            "delivery_queue_depth": depth,
            "counts": result["counts"],
        }


def measure(graph_size: int, document_size: int, repeats: int) -> Dict[str, Any]:
    graph = _build_graph(graph_size)
    graph.pop("_hub")
    document = _document(graph, document_size)
    runs = [_run(graph, document) for _ in range(repeats)]

    def median(key: str) -> float:
        return round(statistics.median(run[key] for run in runs), 1)

    return {
        "graph_nodes": graph_size,
        "graph_edges": len(graph["edges"]),
        "document_nodes": len(document["nodes"]),
        "document_edges": len(document["edges"]),
        "counts": runs[-1]["counts"],
        "commit_ms": median("commit_ms"),
        "backup_ms": median("backup_ms"),
        "lock_ms": median("lock_ms"),
        "events": runs[-1]["events"],
        "deliveries_enqueued": runs[-1]["deliveries_enqueued"],
        "delivery_queue_depth": median("delivery_queue_depth"),
        "runs": [
            {k: round(v, 1) for k, v in run.items() if k != "counts"} for run in runs
        ],
    }


def main() -> int:
    # The schema config is resolved from the working directory.
    os.chdir(REPO_ROOT)
    # Every refused delivery logs a line; the figures are the output.
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=int, default=100_000)
    parser.add_argument("--document", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(measure(args.graph, args.document, args.repeats), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
