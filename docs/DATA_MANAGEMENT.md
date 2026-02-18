# Data Management

This document describes how graph data is managed in the Community Knowledge Graph application.

## Directory Structure

```
data/
  examples/          # Example graph data files (tracked in git)
    default.json     # Default example dataset
  active/            # Active graph data used by the running app (git-ignored)
    graph.json       # Currently active graph file
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
   → Copy data/examples/default.json to data/active/graph.json

3. Else (data/active/graph.json exists):
   → Use the existing file as-is
```

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
      "communities": []
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

**Domain types** (configurable via `config/schema_config.json`):
- **Actor** (blue) - Organizations, agencies, individuals
- **Initiative** (green) - Projects, programs, collaborative activities
- **Capability** (orange) - Capabilities, competencies, skills
- **Resource** (yellow) - Reports, software, tools, datasets
- **Legislation** (red) - Laws, directives (NIS2, GDPR, etc.)
- **Theme** (teal) - AI strategies, data strategies, themes
- **Goal** (indigo) - Strategic objectives and targets
- **Event** (fuchsia) - Conferences, workshops, milestones

**System types** (foundational to the application):
- **SavedView / VisualizationView** (gray) - Saved graph view snapshots
- **EventSubscription** (violet) - Webhook subscriptions for graph mutation events
- **Agent** (pink) - AI agent configurations
- **Groups** - Visual grouping of nodes in the canvas

Domain types can be freely modified, added, or removed in the schema configuration file. System types are integral to application functionality and should not be removed.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPH_FILE` | `data/active/graph.json` | Path to the active graph file |
| `GRAPH_SCHEMA_CONFIG` | `config/schema_config.json` | Path to schema configuration |

When using `./start-dev.sh`, the `GRAPH_FILE` variable is automatically set to `data/active/graph.json`.
