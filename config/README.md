# Configuration Profiles

Profiles let you run the same codebase with different domain configurations — node types, colors, prompts, API keys, and seed graph data.

## Directory Structure

```
config/
  default/                   # Base profile (always present)
    schema_config.json       # Node types, relationships, presentation
    federation_config.json   # Federation topology
    .env.example             # Template for profile-specific env vars
  stat-metadata/             # European Statistical System metadata profile
    schema_config.json       # ESS-focused node types (NSIs, programmes, datasets, variables…)
    graph.json               # ESS seed graph data
  scb/                       # Statistics Sweden (SCB) profile
    schema_config.json       # SCB-specific node types
  test/                      # Test profile
    schema_config.json       # Minimal config for testing
  profile-utils.sh           # Shared helpers (sourced by start scripts)
```

## How It Works

Each profile is a directory under `config/`. A profile only needs to contain the files it wants to override — everything else falls back to `config/default/`.

### Fallback Chain

```
profile file → config/default/ file
profile .env → config/default/.env → root .env
```

Environment variables already set by the caller are never overridden.

### Recognized Files

| File | Purpose |
|------|---------|
| `schema_config.json` | Node types, relationship types, colors, prompts |
| `federation_config.json` | Federation topology and sync settings |
| `.env` | Secrets: API keys, auth passwords (git-ignored) |
| `.env.example` | Documents expected env vars (tracked) |
| `graph.json` | Seed graph data for initial setup |

## Usage

### Single Instance

```bash
./start-dev.sh                                    # Uses config/default/
./start-dev.sh --profile stat-metadata            # ESS metadata profile
./start-dev.sh --profile stat-metadata --lang en  # Profile + language override
./start-dev.sh --profile scb --lang sv            # SCB profile in Swedish
```

For cloud environments (e.g. SSPCloud), use `start-sprint.sh` which auto-installs
dependencies and loads the `stat-metadata` profile. See [docs/SSPCloud-setup.md](../docs/SSPCloud-setup.md).

### Federated (Multi-Profile)

```bash
# Legacy mode: two instances with default schema, auto-split data
./start-federated-dev.sh

# Profile mode: each profile becomes a federated instance
./start-federated-dev.sh --profile esam --profile unece
```

In profile federation mode, each instance gets its own schema, env vars, and graph data. Federation configs are auto-generated to wire the instances together.

## Creating a New Profile

```bash
# 1. Create profile directory
mkdir config/myprofile

# 2. Add the files you want to customize (copy from default as a starting point)
cp config/default/schema_config.json config/myprofile/schema_config.json

# 3. Edit to customize
# ... edit config/myprofile/schema_config.json ...

# 4. Optionally add secrets
cp config/default/.env.example config/myprofile/.env
# ... edit config/myprofile/.env ...

# 5. Optionally add seed graph data
# ... create or copy config/myprofile/graph.json ...

# 6. Run with your profile
./start-dev.sh --profile myprofile
```

## Environment Variable Override

You can always override resolved config paths via environment variables:

```bash
SCHEMA_FILE=/custom/path.json ./start-dev.sh --profile esam
```

The env var takes precedence over the profile's file.
