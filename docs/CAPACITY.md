# Capacity envelope

What one instance holds, and what it costs to serve it.

Every figure here comes from `scripts/measure_capacity.py`. Re-run it rather
than quoting these numbers second-hand: a figure in this document that the
script cannot reproduce is stale, and that is the only way to tell.

```bash
# exactly what produced the tables below - the 100,000 row is not a default
python3 scripts/measure_capacity.py --sizes 5000,20000,50000,100000
CO_TEST_POSTGRES_DSN=... python3 scripts/measure_capacity.py \
    --sizes 5000,20000,50000,100000 --postgres
```

Without `--sizes` the script measures 5,000 / 20,000 / 50,000 only, and the
marginal figure it prints is the slope over that shorter span rather than the
one quoted here.

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

## How a measurement is taken

Each `(backend, size)` pair costs **two** processes, and the split is what
makes the memory figures mean anything.

The first process builds the fixture, writes it to the backend and exits. The
second starts clean, loads the graph from that store and measures. So the
process reporting resident memory never allocated the fixture — it holds the
interpreter, the imports and the graph, which is what a deployment holds.

Freeing the fixture instead of exiting does **not** work — though not for the
reason it first appears, and the difference matters because the wrong reason
is a false general fact about Python.

Measured at 50,000 nodes: build the fixture (~70 MB), write it to the backend,
then release it and collect. Resident memory stays **72 MB above** where it
started. The tempting conclusion is that Python keeps the pages. It does not:
drop the *backend* object as well and 69 of those 72 MB come straight back.
The fixture was still reachable the whole time, because
`FileGraphPersistenceBackend` keeps an in-memory mirror holding the same node
dicts.

That is a fragile thing to build a measurement on. Whether releasing the
fixture is enough would depend on which backend is under test and whether it
happens to retain a reference — and in practice it was not enough: measured
this way the file backend at 50,000 nodes reported **617.7 MB** of process
memory, against **548.6 MB** measured properly. Exiting the process needs to
know none of that.

## File backend

| nodes | edges | process MB | graph MB | cold start s | snapshot s | search rare ms | search all ms | hub depth 3 ms |
|---|---|---|---|---|---|---|---|---|
| 5,000 | 8,497 | 110.5 | 60.5 | 0.35 | 0.10 | 1.6 | 13.3 | 20.7 |
| 20,000 | 33,999 | 254.8 | 204.8 | 2.49 | 0.40 | 6.7 | 69.1 | 35.3 |
| 50,000 | 84,998 | 548.6 | 498.5 | 6.03 | 1.00 | 16.3 | 154.8 | 146.3 |
| 100,000 | 169,997 | 1022.8 | 972.7 | 12.76 | 1.97 | 33.3 | 341.5 | 186.0 |

- **marginal: 10,069 B per node** — one node and its 1.7 edges
- **graph's own fixed cost: 12 MB** — the intercept the slope leaves
- **process floor: ~50 MB** — `process MB` minus `graph MB`, flat across all
  four sizes (50.0 / 50.0 / 50.1 / 50.1)

## PostgreSQL backend

| nodes | edges | process MB | graph MB | cold start s | snapshot s | search rare ms | search all ms | hub depth 3 ms |
|---|---|---|---|---|---|---|---|---|
| 5,000 | 8,497 | 122.1 | 61.0 | 0.45 | 0.51 | 1.7 | 13.2 | 44.9 |
| 20,000 | 33,999 | 251.9 | 190.8 | 1.71 | 1.87 | 6.6 | 60.4 | 73.1 |
| 50,000 | 84,998 | 516.8 | 455.6 | 4.62 | 3.85 | 17.1 | 166.9 | 293.9 |
| 100,000 | 169,997 | 953.7 | 892.7 | 9.40 | 8.80 | 33.3 | 352.2 | 338.4 |

- **marginal: 9,180 B per node** — one node and its 1.7 edges
- **graph's own fixed cost: 17 MB**
- **process floor: ~61 MB** (61.1 / 61.1 / 61.2 / 61.0) — higher than the file
  backend's by psycopg and its connection pool

## How to read the memory figures

There are **two** fixed costs, and conflating them is the easiest way to size a
container wrongly.

`graph MB` is the difference between two readings in the measuring process:
one taken before `GraphStorage` is constructed, one after the graph is loaded.

**Its intercept is not the graph's own data structures**, and it is worth
saying so because the name invites that reading. Measured at a single node,
`graph MB` is already 12.54 MB — the whole intercept is present before there
is any graph to speak of. Pre-import `numpy` before the baseline and the same
measurement gives 1.58 MB, with total process memory unchanged. So roughly
11 MB of the file backend's 12 MB intercept is `numpy`, imported lazily on the
load path *after* the baseline is taken, and only about 1.5 MB is what
`GraphStorage` itself allocates.

That is a measurement artefact, not a cost: those 11 MB are resident either
way, and land in `process MB` regardless of which side of the baseline they
fall on. The three-term model below reconciles with the tables for that
reason. But do not read "12 MB" as the price of an empty graph — it is about
1.5 MB.

**NetworkX is in neither of those**: `import networkx` is module-scope in
`backend/core/storage.py`, so its 24.1 MB is paid before the baseline and sits
in the process floor; the `MultiDiGraph` it provides then grows per node and
per edge, which is marginal rather than fixed.

The ~11 MB by which the PostgreSQL process floor exceeds the file backend's is
the psycopg import (measured). Its intercept is also higher — 17 MB against
12 MB — and that difference is *not* the connection pool, which costs under
0.1 MB; what accounts for it has not been measured, so it is left unexplained
rather than guessed at.

So there are three terms, not two:

```
resident ≈ nodes × marginal  +  graph's fixed cost  +  process floor
```

Checked against the table, both backends at both large sizes:

| | model | measured |
|---|---|---|
| file, 100,000 | 960 + 12 + 50 = **1,022 MB** | 1,022.8 |
| file, 50,000 | 480 + 12 + 50 = **542 MB** | 548.6 |
| PostgreSQL, 100,000 | 876 + 17 + 61 = **954 MB** | 953.7 |
| PostgreSQL, 50,000 | 438 + 17 + 61 = **516 MB** | 516.8 |

Dropping a fixed term throws the answer out by 12-17 MB (the graph's) or
50-61 MB (the floor), and 62-78 MB for both — which at the top of that range
is the difference between a container that fits and one the kernel kills.

Take the **slope between two sizes**, not the ratio at one size. A per-size
`bytes / nodes` ratio mixes both fixed costs into the per-node cost and makes
small graphs look extravagant and large ones look cheap — at 2,000 nodes it
reports 16.5 kB a node against a marginal cost of about 10 kB. The slope
cancels the fixed term; the intercept names it. That is why the script reports
the slope only when it has at least two sizes to take it between, and says so
instead when it does not.

## Sizing from this

About **10 MB of resident memory per 1,000 nodes**, plus *both* fixed costs —
the graph's own 12 MB (file) or 17 MB (PostgreSQL), and the process floor of
~50 MB or ~61 MB. Dropping either is wrong by tens of megabytes.

| graph | file backend | PostgreSQL |
|---|---|---|
| 20,000 nodes | ~255 MB | ~252 MB |
| 50,000 nodes | ~549 MB | ~517 MB |
| 100,000 nodes | ~1.02 GB | ~954 MB |

A 1 GB container holds 50,000 nodes with room to serve requests. It does not
comfortably hold 100,000: the file backend needs 1,022.8 MiB there and
PostgreSQL 953.7 MiB, so against a 1024 MiB limit one is within about a
megabyte of it and the other has 70 MiB left — for the interpreter's own
peaks and every concurrent request. 100,000 nodes wants 2 GB.

(The tables are MiB throughout, as `/proc` reports them. The figures above are
compared against a 1024 MiB container for that reason.)

## Where the ceiling is, and what gives first

Memory is not what breaks. It is linear, predictable from the slope, and
cheap to buy. Two other things degrade with size, and which one bites first
depends on which budget is tighter in your deployment.

**Cold start** is the operational constraint. At 100,000 nodes it is 12.8 s
(file) and 9.4 s (PostgreSQL) — inside a generous readiness probe, past a
default one, and paid on every rollout and every scale-up. It grows roughly
linearly with the graph.

**Search** is the user-visible one, and both kinds scale with the corpus. A
term matching most of the corpus costs 341 ms (file) and 352 ms (PostgreSQL)
at 100,000 nodes, against about 13 ms at 5,000 — already at the edge of what
feels immediate.

A selective search is cheaper by a constant factor, **not** independent of
graph size: the rare term costs 1.6 ms at 5,000 and 33.3 ms at 100,000 — the
same 20× the corpus grew by. `LexicalIndex.candidates` counts and scans one
joined copy of the whole corpus before it visits any candidate node, so corpus
size is what both searches pay for; the selective one merely does less work
per match afterwards, about 10× less overall.

So the practical ceiling for an interactive deployment on this hardware is
around **100,000 nodes**, and what gives first is exhaustive search latency
followed by cold start — not memory. Beyond that, measure rather than
extrapolate: these figures stop at 100,000 on purpose.

### Against the earlier measurement

**These three "then" figures are the one second-hand quote in this document.**
They come from a measurement recorded before the vector split and the
traversal work, taken with tooling that no longer exists; this script cannot
reproduce them and nothing in the repository pins them. Treat them as a
recorded observation, not as a reproducible baseline — the point is the
direction of travel, not the exact deltas.

The same three figures, file backend at 100,000 nodes:

| | then | now |
|---|---|---|
| bytes per node | 15.7 kB | 10.1 kB |
| cold start at 100,000 | 15.7 s | 12.8 s |
| lexical search at 100,000 | 394 ms | 341 ms |

Memory per node is down by about a third; the two latencies are modestly
better. Nothing regressed.

## Interactive latency

Read the two `hub depth 3` columns as two engines rather than as a race. Only
the PostgreSQL backend declares `store_traversal`, so its figure is the SQL
level query; the file backend's is the in-memory walk. That walk is also what
a PostgreSQL deployment falls back to whenever a write is pending, so the file
column doubles as the fallback cost on either backend — it is not a number
only file-backed installations see.

The PostgreSQL traversal figure is a *steady-state* number, deliberately. The
measurement runs depth 1, depth 2 and then depth 3 with a warm-up, so the
connection has issued eighteen level queries before the depth-3 block starts,
twenty-one before the first timed sample, and thirty by the time the reported
median is taken.

On the code as it stands that count changes nothing: the level query passes
`prepare=False` and psycopg never prepares it, at any execution count (see the
note on the level query in `docs/PERSISTENCE_BACKENDS.md`). The count is there
as a **regression guard**, not as a description of what happens today. It is
past the point at which psycopg *would* start preparing if that opt-out were
ever removed — and a prepared level query is exactly where a 17× cliff hid
until this measurement found it. So a benchmark that stopped at the third call
would report the same figure today and catch nothing tomorrow; this one would
show the cliff.

## What this does not measure, and why

**Semantic search.** The base install carries no ML stack by deliberate policy
(`requirements-ml.txt` is separate), so `VectorStore` falls back to its mock
path here exactly as it does in CI. A semantic number measured against the
mock would be a number about the mock, not about search.

**Sizes above 100,000 nodes.** Memory extrapolates honestly from the slope,
but cold start and search are both linear and already near their budgets at
100,000, so a larger deployment should be measured rather than assumed.

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
| 3 | A change written by one instance becomes visible to the others within a stated bound | holds — **tens of milliseconds**: 7-36 ms across twelve runs, against a 10 s acceptance ceiling |
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
path, and `SESSIONS_DIR` overrides it — `AppConfig.resolve_sessions_dir()` is
the one definition, and the server calls it. Whether the criterion holds is
decided by whether *that* path is on shared storage — a mounted bucket or
volume — and not by which graph backend is configured. Moving the graph to
PostgreSQL does not move the sessions with it, which is precisely how a
multi-instance deployment came to fail 45% of its MCP calls; the module
docstring in `backend/core/tests/test_multi_instance_acceptance.py` describes
that failure.

The 10 s ceiling in criterion 3 is an acceptance bound for a shared CI runner,
not a latency anyone should quote. The measured number is what belongs in this
document, and the test prints it on every run — so the figure above is a range
across observed runs rather than a single sample, and re-running the suite is
how to check it still holds.

That number is timed from the moment the write is **issued**, not from the
moment `flush()` returns — and not from the commit either: the commit happens
on a background worker afterwards, so the figure includes the writer's own
local write as well as the propagation, and on a fast run the local half can
dominate it. It is therefore a conservative upper bound on cross-instance
visibility rather than a measurement of it alone.

Timing from `flush()`'s return instead would be worse, not better: with a
write large enough that propagation finishes while the flush is still
running, it reports 0 ms — which reads as instant and actually means the clock
started after the thing it was timing.

Criterion 3 covers a node and an edge together, because "a change" is not "a
node change" and an edge report dropped on its own would otherwise go
unnoticed. It does not reach the *ordering* invariant between the two, because
`add_nodes` emits nodes and edges as two separate announcements;
`test_persistence_contract_postgres.py` holds that.
