# Data Management

This document describes how graph data is managed in the Community Knowledge Graph application.

## Directory Structure

```
data/
  examples/          # Example graph data files (tracked in git)
    default.json     # Default example dataset
  active/            # Active graph data used by the running app (git-ignored)
    graph.json              # Currently active graph file
    graph.embeddings.bin    # Embedding vectors for that graph (see below)
    graph.history.ndjson    # Mutation history sidecar
```

## How It Works

The application uses a **two-location model** for graph data:

1. **Example data** (`data/examples/`) - Pre-built datasets that ship with the repository. These are tracked in git and serve as starting points.
2. **Active data** (`data/active/graph.json`) - The graph file actually used by the running application. This file is **git-ignored** so your working data is never accidentally committed.

When the application starts via `./start-dev.sh`, the following logic applies:

```
1. If --data <source> is provided:
   → Copy/download that source to data/active/graph.json (overwrites existing)

2. Else if data/active/graph.json does NOT exist:
   → Check for config/<profile>/graph.json (profile-specific seed data)
   → Else copy data/examples/default.json to data/active/graph.json

3. Else (data/active/graph.json exists):
   → Use the existing file as-is
```

Profiles can include a `graph.json` seed file that is used on first startup. See [PROFILES.md](./PROFILES.md) for details.

This means:
- **First run**: automatically starts with the default example data
- **Subsequent runs**: preserves your working data
- **Explicit data loading**: `--data` flag always overwrites

## Loading Data

### From a local file

```bash
# Load from an example file
./start-dev.sh --data data/examples/default.json

# Load from any local file
./start-dev.sh --data /path/to/my/graph.json

# Load from a relative path
./start-dev.sh --data ../other-project/graph.json
```

### From a URL

```bash
# Load from a remote URL (e.g. GitHub Pages, raw GitHub, any HTTP endpoint)
./start-dev.sh --data https://example.github.io/community-data/graph.json

# Load from raw GitHub content
./start-dev.sh --data https://raw.githubusercontent.com/org/repo/main/data/graph.json
```

### Reset to default example data

```bash
# Delete active data and restart to get the default
rm data/active/graph.json
./start-dev.sh

# Or explicitly load the default
./start-dev.sh --data data/examples/default.json
```

## Adding Example Datasets

To add a new example dataset:

1. Create your graph JSON file with the standard format:
   ```json
   {
     "nodes": [...],
     "edges": [...],
     "metadata": {
       "version": "1.0",
       "last_updated": "2026-01-01T00:00:00"
     }
   }
   ```

2. Save it to `data/examples/` with a descriptive name:
   ```bash
   cp my-dataset.json data/examples/my-community.json
   ```

3. Load it at startup:
   ```bash
   ./start-dev.sh --data data/examples/my-community.json
   ```

## Graph JSON Format

Each graph file follows this structure:

```json
{
  "nodes": [
    {
      "id": "uuid-string",
      "type": "Actor",
      "name": "Organization Name",
      "description": "Longer description text",
      "summary": "Short label for visualization",
      "tags": ["tag1", "tag2"],
      "subtypes": ["Government agency"]
    }
  ],
  "edges": [
    {
      "id": "uuid-string",
      "source": "source-node-id",
      "target": "target-node-id",
      "type": "RELATES_TO"
    }
  ],
  "metadata": {
    "version": "1.0",
    "last_updated": "ISO-8601 timestamp"
  }
}
```

### Node Types

Node types fall into two categories:

**Domain types** (configurable per profile via `schema_config.json`, see [PROFILES.md](./PROFILES.md)):

The default profile includes: Actor, Initiative, Capability, Resource, Legislation, Theme, Goal, Event, Data, Risk. Other profiles can define additional types (e.g., the SCB profile adds Dataset, Undersökning, Variabel, etc.).

**System types** (foundational to the application):
- **SavedView / VisualizationView** (gray) - Saved graph view snapshots
- **EventSubscription** (violet) - Webhook subscriptions for graph mutation events
- **Agent** (pink) - AI agent configurations
- **Groups** - Visual grouping of nodes in the canvas

All domain node types support an optional **subtypes** field for sub-classification within each type. Subtypes are stored as a list of strings (e.g., `["Government agency", "Regulatory body"]`). The UI provides autocomplete with case normalization based on existing subtypes in the graph, helping maintain consistency. Example subtypes:
- Actor: "Government agency", "Municipality", "International organisation", "Steering group"
- Initiative: "Research project", "Pilot program", "Working group"
- Risk: "Cybersecurity", "Compliance", "Operational"
- Data: "Open data", "Register", "API", "Statistics"

Domain types can be freely modified, added, or removed in the schema configuration file. System types are integral to application functionality and should not be removed. See [PROFILES.md](./PROFILES.md) for how to create custom profiles with different node types.

## Embedding Sidecar

Semantic search needs a vector per node. Those vectors are **not** part of the
graph file — they live in a separate binary file next to it, by default
`<graph stem>.embeddings.bin` (so `data/active/graph.embeddings.bin` for the
default layout), overridable with `EMBEDDINGS_FILE`.

They are kept out of `graph.json` because a 384-dimension vector costs about
1.5 kB as float32 and roughly 7.5x that as JSON text. Written into the graph
file, they dominated it, and every graph mutation rewrote all of them even when
none had changed. The sidecar is rewritten only when a vector actually changes.

What this means in practice:

- **Backup.** Back up the sidecar alongside `graph.json`. It is the only copy
  of the vectors.
- **Restore.** A restore without the sidecar still works: the graph loads, and
  semantic search returns nothing until the vectors are regenerated. A sidecar
  that cannot be read is ignored with a warning for the same reason — the graph
  never fails to load because of it. Regenerate with
  `python scripts/generate_embeddings.py`, which needs the optional ML extras
  (`pip install -r backend/requirements-ml.txt`). That script and
  `scripts/migrate_embeddings.py` both honour `EMBEDDINGS_FILE`, and take
  `--embeddings-file` to override it, so they act on the same sidecar the
  running app reads.
- **Migration.** A `graph.json` written before the split still carries its
  vectors inline. Those are read on load and moved into the sidecar on the next
  save; nothing needs to be run by hand.
- **Portability.** Vectors are derived from node text, so a graph file moved
  without its sidecar is complete data — only the search index is missing.
- **Replacing the graph.** A sidecar belongs to the graph it was built from.
  Swapping in a different dataset while leaving the old sidecar in place would
  give any node id present in both the *old* dataset's vector, and nothing
  regenerates a vector for a node that is only loaded. `start-dev.sh` deletes
  the sidecar whenever it puts a different graph in place — `--data`, or seeding
  a missing `graph.json` from a profile or example — and follows
  `EMBEDDINGS_FILE` when doing so. Do the same when replacing a graph file by
  hand.

## Mutation History

Every graph mutation is appended to a history sidecar next to the graph file
(`graph.history.ndjson` for the default layout). It is an audit trail: append-only,
newest records last, one self-contained JSON record per line.

**Retention.** The sidecar is capped at `HISTORY_MAX_EVENTS` records (default
100000). When the cap is exceeded, a compaction pass rewrites the file keeping the
newest N, atomically. `HISTORY_MAX_AGE_DAYS` additionally drops records older than
a given age; it is unset by default, because "delete records older than X" is a
retention policy an operator should choose rather than inherit. Setting
`HISTORY_MAX_EVENTS=0` removes the count cap; trimming then stops altogether
unless `HISTORY_MAX_AGE_DAYS` is also set, in which case age-based trimming
still runs. **To keep every record you must set `HISTORY_MAX_EVENTS=0`
explicitly and leave `HISTORY_MAX_AGE_DAYS` unset** — leaving both unset
applies the default cap, it does not disable trimming.

The default is deliberately generous, so that upgrading a typical deployment does
not trim anything. It is a cap, not a promise: a sidecar that already holds more
than `HISTORY_MAX_EVENTS` records loses the excess on the first compaction after
the upgrade. Set `HISTORY_MAX_EVENTS=0` before upgrading if that matters. Note
also that the resulting file size depends on the record size, so the cap bounds
the record count rather than the bytes directly.

**When trimming runs.** A compaction pass reads and rewrites the whole sidecar
while holding the store lock, so it is throttled rather than run on every append:
once per tenth of the records the previous pass left on disk. Deriving it from
the file rather than from the cap keeps the work per mutation roughly constant at
any size — on the order of twenty records read per append, since a pass makes two
forward passes plus the rewrite — including under age-based retention, where
there is no count cap to derive it from. The cost is a bounded overshoot — between passes the sidecar holds what
retention keeps plus at most one interval, about 110%.

One consequence worth knowing if you rely on `HISTORY_MAX_AGE_DAYS`: passes are
counted in mutations, never in time. A record past the cutoff survives until the
next pass — up to a tenth of the history's worth of appends — so on a quiet
instance aged-out records stay on disk indefinitely. Age retention bounds records
in mutations, not in wall-clock time; it is not a deletion deadline.

**Reads.** History queries return a page at a time and read the file backwards, so
answering one costs memory proportional to the page rather than to the file. That
matters on a small container: the previous implementation parsed every record to
return the newest 50.

**What a record holds.** A **node** update record keeps the `patch` — the fields
that actually changed — plus, in `before` and `after`, those same fields and the
entity's display name. That is exactly what the history views read: the name to
title the entry, and each patched field's before-value to render a
`before → after` diff. Fields the mutation left alone are not repeated into the
record, so a one-word edit to a large node costs a small record rather than two
copies of the node.

Every other record keeps both snapshots whole. Creates and deletes have no patch
that could describe what appeared or vanished. Edge updates have no patch either
— one is currently computed for node updates only — so the views fall back to
diffing the pair, which needs both sides intact.

An `embedding` is dropped from every payload of every record the current code
writes. Vectors live in the embedding sidecar; a copy here would put them back
into every mutation record. Note that this is not retroactive: an instance that
ran history before the vectors moved to the sidecar wrote records with the vector
inline, and those records are left as they are (see below).

Older records — written before the trimming, or before the sidecar — keep their
full snapshots and whatever they carried at the time. They are read exactly as
they always were, since the record shape did not change, only how much of it is
filled in. No migration is needed for correctness and none is offered; an
operator who wants the old vectors out of the sidecar can truncate or discard the
history file, which the graph does not depend on.

**Backup.** The sidecar is independent of `graph.json` and can be backed up,
truncated or discarded on its own; the graph does not depend on it.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPH_FILE` | `data/active/graph.json` | Path to the active graph file |
| `EMBEDDINGS_FILE` | `<graph stem>.embeddings.bin` next to the graph | Path to the binary embedding sidecar (see *Embedding Sidecar* above) |
| `HISTORY_MAX_EVENTS` | `100000` | Mutation-history records retained (see *Mutation History* above); `0` removes the count cap |
| `HISTORY_MAX_AGE_DAYS` | *(unset)* | Age-based history retention, opt-in |
| `GRAPH_SCHEMA_CONFIG` | `config/default/schema_config.json` | Path to schema configuration |
| `SCHEMA_FILE` | *(auto-resolved from profile)* | Alternative env var for schema path |

When using `./start-dev.sh --profile <name>`, the `SCHEMA_FILE` variable is automatically set based on the profile's `schema_config.json` (with fallback to default).

## Current state vs future hosted direction

The current application still uses file-based graph persistence as the default runtime model. That remains appropriate for:
- local development
- standalone open source deployment
- early hosted-readiness work where the focus is on seams, contracts, and operational hooks

For the long-term shared SaaS architecture, the target is different:
- multiple graphs or workspaces should eventually be served by shared application/service instances
- user access to graphs should be controlled through application-managed identity and authorization
- the storage layer should eventually support shared persistence with row-based or equivalent record-level access constraints

That future storage direction is not implemented by this document. Its purpose here is only to clarify that file-based graph storage is a current implementation choice, not the intended final architecture for shared SaaS hosting. The seam a shared backend plugs into — and what it has to implement — is described in [PERSISTENCE_BACKENDS.md](./PERSISTENCE_BACKENDS.md).
