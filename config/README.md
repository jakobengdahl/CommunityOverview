# Configuration Profiles

Profiles let you run the same codebase with different domain configurations — node types, colors, prompts, API keys, and seed graph data.

## Directory Structure

```
config/
  default/                   # Base profile (always present)
    schema_config.json       # Node types, relationships, presentation
    federation_config.json   # Federation topology
    .env.example             # Template for profile-specific env vars
    skills/                  # Skills loaded for ALL profiles (fallback)
      impact-analysis/SKILL.md   # Generic graph dependency impact analysis
  stat-metadata/             # European Statistical System metadata profile
    schema_config.json       # ESS-focused node types (NSIs, programmes, datasets, variables…)
    graph.json               # ESS seed graph data
    skills/                  # ESS-specific skills (loaded in addition to default)
      graph-analysis/SKILL.md        # Graph pattern analysis
      gsim-lineage-impact/SKILL.md   # GSIM lineage and classification change impact
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

| File/Dir | Purpose |
|----------|---------|
| `schema_config.json` | Node types, relationship types, colors, prompts, skills config |
| `federation_config.json` | Federation topology and sync settings |
| `.env` | Secrets: API keys, auth passwords (git-ignored) |
| `.env.example` | Documents expected env vars (tracked) |
| `graph.json` | Seed graph data for initial setup |
| `skills/<name>/SKILL.md` | Agent skill instructions injected into the AI system prompt |

## Usage

### Single Instance

```bash
./start-dev.sh                                    # Uses config/default/
./start-dev.sh --profile stat-metadata            # ESS metadata profile
./start-dev.sh --profile stat-metadata --lang en  # Profile + language override
./start-dev.sh --profile scb --lang sv            # SCB profile in Swedish
```

For cloud environments (e.g. SSPCloud), use `scripts/start-sprint.sh` which auto-installs
dependencies and loads the `stat-metadata` profile. See [docs/SSPCloud-setup.md](../docs/SSPCloud-setup.md).

### Federated (Multi-Profile)

```bash
# Legacy mode: two instances with default schema, auto-split data
./scripts/start-federated-dev.sh

# Profile mode: each profile becomes a federated instance
./scripts/start-federated-dev.sh --profile esam --profile unece
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

# 6. Optionally add profile-specific skills
mkdir -p config/myprofile/skills/my-skill
# ... create config/myprofile/skills/my-skill/SKILL.md ...
# Set skills_config.skills_dir in schema_config.json:
#   "skills_config": { "skills_dir": "config/myprofile/skills" }

# 7. Run with your profile
./start-dev.sh --profile myprofile
```

Skills in `config/default/skills/` are always loaded. Profile skills are loaded in addition when that profile is active. See [docs/PROFILES.md](../docs/PROFILES.md) for the SKILL.md format and full skills configuration reference.

## Environment Variable Override

You can always override resolved config paths via environment variables:

```bash
SCHEMA_FILE=/custom/path.json ./start-dev.sh --profile esam
```

The env var takes precedence over the profile's file.
