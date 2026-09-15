# Capacity envelope

What one instance holds, and what it costs to serve it.

Every figure here comes from `scripts/measure_capacity.py`. Re-run it rather
than quoting these numbers second-hand: a figure in this document that the
script cannot reproduce is stale, and that is the only way to tell.

```bash
python3 scripts/measure_capacity.py                       # file backend
CO_TEST_POSTGRES_DSN=... python3 scripts/measure_capacity.py --postgres
```

The two backends are reported separately and neither is a proxy for the
other. Most installations run the file backend, and it is measured here as a
first-class configuration rather than as a baseline the PostgreSQL numbers are
compared against.

## Where these numbers came from

4 vCPU Intel Xeon at 2.80 GHz, 15 GB RAM, Python 3.11, PostgreSQL 16 on the
same host as the application. A container on shared infrastructure, not a
tuned benchmark machine — treat the figures as the right order of magnitude
and the right *shape*, and re-measure on the hardware you intend to deploy on
before sizing anything tightly.

The fixture is a preferential-attachment graph at 1.7 edges per node, which is
what a real deployment's graph looks like: a few hubs and a long tail. Nodes
carry a few-word name and a couple of sentences of description, because node
cost is dominated by payload rather than by topology. Traversals start from
the single most connected node — the worst case an interactive canvas actually
meets, and the one uniform wiring has no hubs to find.

## File backend

| nodes | edges | process MB | graph MB | cold start s | snapshot s | search rare ms | search all ms | hub depth 3 ms |
|---|---|---|---|---|---|---|---|---|
| 5,000 | 8,497 | 117.4 | 60.1 | 0.33 | 0.11 | 13.5 | 13.0 | 18.7 |
| 20,000 | 33,999 | 282.7 | 202.9 | 1.63 | 0.43 | 68.1 | 65.6 | 40.7 |
| 50,000 | 84,998 | 618.9 | 494.2 | 5.47 | 0.99 | 186.0 | 186.4 | 173.4 |

- **marginal: 10,115 B per node** — one node and its 1.7 edges
- **fixed: 12 MB** before the first node

## PostgreSQL backend

| nodes | edges | process MB | graph MB | cold start s | snapshot s | search rare ms | search all ms | hub depth 3 ms |
|---|---|---|---|---|---|---|---|---|
| 5,000 | 8,497 | 129.0 | 53.5 | 0.52 | 0.48 | 17.1 | 16.3 | 47.0 |
| 20,000 | 33,999 | 279.1 | 181.3 | 1.93 | 1.88 | 71.3 | 67.0 | 74.5 |
| 50,000 | 84,998 | 586.2 | 447.4 | 7.08 | 3.92 | 175.7 | 178.2 | 268.3 |

- **marginal: 9,179 B per node** — one node and its 1.7 edges
- **fixed: 10 MB** before the first node

## How to read the memory figures

Take the **slope between two sizes**, not the ratio at one size. A per-size
`bytes / nodes` ratio mixes the fixed cost of the process into the per-node
cost and makes small graphs look extravagant and large ones look cheap; the
slope cancels the fixed term, and its intercept is what names that fixed cost.
The script reports both, and this is why it measures at least two sizes.

Each `(backend, size)` pair is measured in a **fresh subprocess**. That is not
tidiness. Resident memory is the number the envelope turns on, and a process
that has already built one graph carries its allocator's free lists and
whatever a previous size warmed — measuring two sizes in one process makes the
second look cheaper than it is.

`process MB` is resident memory for the whole process and is the number to
size a container on. `graph MB` is the graph's own share of it.

## Sizing from this

At roughly 10 KB per node in both backends, a graph is about **10 MB of
resident memory per 1,000 nodes**, plus 10–12 MB for the process itself.
50,000 nodes needs on the order of 600 MB resident; a 1 GB container holds
that with room to serve requests, and a 512 MB container does not.

Cold start is the figure that constrains deployment rather than serving:
5–7 seconds at 50,000 nodes, on both backends. That is a readiness-probe
budget and a rolling-update consideration, not a per-request cost.

## Interactive latency

Search and depth-3 traversal from the largest hub both stay well inside a
canvas's tolerance at 50,000 nodes.

Read the two traversal columns as two engines rather than as a race. Only the
PostgreSQL backend declares `store_traversal`, so its figure is the SQL level
query; the file backend's is the in-memory walk. That walk is also what a
PostgreSQL deployment falls back to whenever a write is pending, so the file
column doubles as the fallback cost on either backend — it is not a number
only file-backed installations see.

The PostgreSQL traversal figure is a *steady-state* number, deliberately. The
measurement runs depth 1, depth 2 and then depth 3 with a warm-up, so the
connection has issued roughly eighteen level queries before the reported
median is taken, and twenty-one before the first timed sample.

On the code as it stands that count changes nothing: the level query passes
`prepare=False` and psycopg never prepares it, at any execution count (see the
note on the level query in `docs/PERSISTENCE_BACKENDS.md`). The count is there
as a **regression guard**, not as a description of what happens today. It is
past the point at which psycopg *would* start preparing if that opt-out were
ever removed — and a prepared level query is exactly where a 17× cliff hid
until this measurement found it. So a benchmark that stopped at the third call
would report the same ~250 ms today and catch nothing tomorrow; this one would
show the cliff.

## What this does not measure, and why

**Semantic search.** The base install carries no ML stack by deliberate policy
(`requirements-ml.txt` is separate), so `VectorStore` falls back to its mock
path here exactly as it does in CI. A semantic number measured against the
mock would be a number about the mock, not about search.

**Sizes above 50,000 nodes.** The three measured sizes establish a slope that
extrapolates honestly for memory, but cold start is super-linear and search is
linear in graph size, so a 200,000-node deployment should be measured rather
than extrapolated.

**Concurrency.** Every figure is single-request. What multiple concurrent
readers cost is a separate question this script does not answer.

## Multi-instance acceptance

Four criteria must hold before a deployment runs more than one instance. They
are stated as tests rather than as an argument, in
`backend/core/tests/test_multi_instance_acceptance.py`:

| # | Criterion | Status |
|---|---|---|
| 1 | Concurrent writes from two instances lose no updates | holds — proved in `test_multi_instance_postgres.py` |
| 2 | An MCP session survives being served by a different instance | holds **conditionally** — see below |
| 3 | A change written by one instance becomes visible to the others within a stated bound | holds — **10-13 ms measured** across runs, against a 10 s acceptance ceiling |
| 4 | Restart does not lose acknowledged writes | holds |

### What this means for file-backed installations

The four criteria above are about running *more than one* instance. The file
backend declares `incremental_writes` and `transactions` but **not**
`change_notification`: there is no mechanism by which one instance learns that
another has written. A file-backed installation is therefore a single-instance
configuration, and that is unchanged by the multi-instance work — nothing in
it alters the file path, and the boot gate that holds change reports until the
first load returns is constructed only for a backend that announces changes at
all.

So a file-backed installation keeps working exactly as before, at the capacity
measured in the first table above. Running two instances against one graph
file is not supported and never was; the second instance would not see the
first one's writes, and the last snapshot written would win.

**Criterion 2 is the one with a condition rather than a yes.** The open core's
session store is file-backed, so it holds exactly when the instances share the
session directory and fails when they do not. The acceptance suite asserts
both halves: two instances on one directory share a session, and two on
separate directories do not. A deployment that runs several instances without
a shared session directory will serve 404 to MCP sessions it did not itself
create.

The session directory defaults to a `sessions/` directory beside the graph
path, and `SESSIONS_DIR` overrides it. Whether the criterion holds is decided
by whether *that* path is on shared storage — a mounted bucket or volume — and
not by which graph backend is configured. Moving the graph to PostgreSQL does
not move the sessions with it, which is precisely how a multi-instance
deployment came to fail 45% of its MCP calls — the module docstring in
`backend/core/tests/test_multi_instance_acceptance.py` describes that failure.

The 10 s ceiling in criterion 3 is an acceptance bound for a shared CI runner,
not a latency anyone should quote. The measured number is what belongs in this
document, and the test prints it on every run — so the figure above is a range
across observed runs rather than a single sample, and re-running the suite is
how to check it still holds.
