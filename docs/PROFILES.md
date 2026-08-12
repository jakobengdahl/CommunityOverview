# Configuration Profiles

Configuration profiles allow you to run the application with different metadata models, presentation settings, and environment variables. Each profile is a directory under `config/` that can override the default configuration.

## Directory Structure

```
config/
  default/                    # Base profile (always required)
    schema_config.json        # Node types, relationships, presentation
    federation_config.json    # Federation topology
    .env.example              # Template for environment variables
    .env                      # Secrets (git-ignored)
    skills/                   # Skills loaded for all profiles (fallback)
      impact-analysis/        # Generic graph dependency impact analysis
        SKILL.md
  stat-metadata/              # European Statistical System metadata profile
    schema_config.json        # ESS node types (NSIs, programmes, datasets, variables…)
    graph.json                # ESS seed data
    skills/                   # ESS-specific skills (supplement default skills)
      graph-analysis/         # Generic graph pattern analysis
        SKILL.md
      gsim-lineage-impact/    # GSIM lineage tracing and change impact assessment
        SKILL.md
  scb/                        # SCB (Statistics Sweden) profile
    schema_config.json        # Custom metadata model
    .env                      # Profile-specific secrets (git-ignored)
  test/                       # Test profile
    schema_config.json        # Minimal config for testing
```

## Using Profiles

Start the application with a specific profile using `--profile`:

```bash
# Use the default profile
./start-dev.sh

# Use the SCB profile
./start-dev.sh --profile scb

# Combine with language and data options
./start-dev.sh --profile scb --lang sv --data data/examples/scb-seed.json
```

## File Resolution (Fallback Chain)

Each profile only needs to contain files that differ from the default. Missing files are resolved from `config/default/`:

```
Profile file exists?  →  Use profile file
        ↓ no
Default file exists?  →  Use default file
        ↓ no
       Use code defaults
```

For example, an SCB profile with only `schema_config.json` will use:
- `config/scb/schema_config.json` for the metadata model
- `config/default/federation_config.json` for federation (fallback)

## Environment Variable Fallback

Environment variables follow a similar fallback chain, with existing variables never overridden:

```
Caller environment (highest priority)
  → config/<profile>/.env
    → config/default/.env
      → .env (project root, lowest priority)
```

## Creating a New Profile

### 1. Create the profile directory

```bash
mkdir config/my-profile
```

### 2. Create `schema_config.json`

The schema config defines the metadata model. It has two main sections:

```json
{
  "schema": {
    "node_types": { ... },
    "relationship_types": { ... }
  },
  "presentation": {
    "title": "My Knowledge Graph",
    "introduction": "Welcome text shown in the chat.",
    "colors": { ... },
    "prompt_prefix": "System prompt context for the AI assistant.",
    "prompt_suffix": "Reminders appended to the AI system prompt.",
    "default_language": "en"
  }
}
```

### 3. Define node types

Each node type has the following fields:

```json
{
  "MyNodeType": {
    "fields": ["name", "description", "summary", "tags", "subtypes"],
    "category": "domain",
    "description": "Human-readable description of this node type",
    "color": "#3B82F6",
    "icon": "PersonFill"
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `fields` | No | List of fields for this type. Defaults to `["name", "description", "summary"]` |
| `category` | No | `"domain"` (default) or `"system"`. Domain types appear in the toolbar |
| `description` | No | Describes the node type. Shown in MCP instructions to AI clients |
| `color` | No | Hex color code for UI display. Defaults to `#9CA3AF` (gray) |
| `icon` | No | Bootstrap Icon name for the toolbar (e.g. `"DatabaseFill"`, `"PeopleFill"`) |
| `static` | No | If `true`, nodes of this type cannot be created via the chat. Used for system types |
| `ui_form` | No | Specialized creation dialog. `"skill"` opens the SKILL.md-compatible form |
| `context_menu` | No | Array of extra items for the right-click context menu (see below) |

Every node — regardless of its type or its `fields` list — also carries a
universal `aliases` field: a list of alternative names/synonyms. Aliases are
editable in every node editor and are matched during search (ranked just below
a node's real name, above tags and descriptions), so a node can be found by an
abbreviation or alternate spelling. You do not need to list `aliases` in a
node type's `fields` array; it is always available, just like `tags` and
`subtypes`.

See [`docs/ICONS.md`](ICONS.md) for the full, up-to-date list of icon names
you can choose from — grouped by theme (people & organizations, statistics,
documents, security, technology, and more) — plus the process for
registering an icon that isn't in that list yet.

Icons can only be selected from names already registered in
`frontend/web/src/components/FloatingToolbar.jsx` (`ICON_REGISTRY`) — this
list is fixed at deployment time, so a config alone cannot introduce a brand
new icon; see `docs/ICONS.md` for how to add one.

#### Context menu items

You can add custom right-click menu items per node type using `context_menu`. Each item has a `label`, an optional `icon` (emoji), and an `action`:

```json
{
  "MyNodeType": {
    "context_menu": [
      {
        "label": "Open in tool",
        "icon": "🔗",
        "action": { "type": "open_url", "url": "https://example.com/{identifier}" }
      },
      {
        "label": "Run analysis",
        "icon": "⚡",
        "action": { "type": "callback", "name": "run_analysis" }
      }
    ]
  }
}
```

| Action type | Description |
|-------------|-------------|
| `open_url` | Opens a URL in a new tab. Use `{field}` in the URL to substitute node field values. |
| `callback` | Fires `onContextMenuAction(name, nodeId, nodeData)` in `App.jsx`. Wire new actions there. |

### 4. Define relationship types

```json
{
  "relationship_types": {
    "BELONGS_TO": {
      "description": "Belongs to (actor belongs to organization)"
    },
    "PRODUCES": {
      "description": "Produces (initiative produces resource)"
    }
  }
}
```

### 5. Configure presentation

The presentation section controls the UI and AI behavior:

```json
{
  "presentation": {
    "title": "My Knowledge Graph",
    "introduction": "Welcome text.\n\nCan contain multiple paragraphs.",
    "colors": {
      "MyNodeType": "#3B82F6"
    },
    "prompt_prefix": "You are a knowledge agent for my domain...",
    "prompt_suffix": "Always confirm before making changes.",
    "default_language": "en"
  }
}
```

| Field | Description |
|-------|-------------|
| `title` | Application title shown in the header |
| `introduction` | Welcome text in the chat. If it contains newlines, it's used as the complete welcome message |
| `colors` | Color overrides per node type (supplements schema colors) |
| `prompt_prefix` | Injected at the start of the AI system prompt |
| `prompt_suffix` | Appended to the AI system prompt |
| `default_language` | Default UI language (`"en"` or `"sv"`) |
| `skills_config` | Skills loader settings (see below) |

#### Model profiles

`model_profiles` is an optional top-level section in `schema_config.json`. If omitted or empty, the application keeps the legacy single-provider behavior (`LLM_PROVIDER`, `LLM_MODEL`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). If configured, each enabled chat request and background agent resolves to a named profile; agents may set `metadata.model_profile_id`, while chat users can select profiles in the UI when selection is enabled.

```json
{
  "model_profiles": {
    "selection_enabled": true,
    "profiles": [
      {
        "id": "fast-openai",
        "name": "Fast OpenAI",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "default": true,
        "credential_ref": "OPENAI_API_KEY"
      },
      {
        "id": "local-llm",
        "name": "Local OpenAI-compatible LLM",
        "provider": "openai",
        "model": "qwen2.5-coder",
        "endpoint": "http://localhost:8080/v1",
        "credential_ref": "LOCAL_LLM_API_KEY"
      }
    ]
  }
}
```

Rules:
- exactly one configured profile must be `default: true`, and it must be enabled
- `credential_ref` is an environment variable name, never an inline API key
- the public capabilities API only exposes enabled profile id/name/provider/model/default, never credentials or endpoints
- `provider: "openai"` supports OpenAI-compatible endpoints via `endpoint`

#### Skills configuration

Skills are SKILL.md-format instructions injected into the AI agent's system prompt at startup. They tell the agent *how* to perform specific tasks.

**Default skills** live in `config/default/skills/` and are always loaded (for every profile). **Profile skills** are loaded in addition when that profile is active. Set `skills_config.skills_dir` in the profile's `schema_config.json` to point to the profile's skills directory:

```json
{
  "presentation": {
    "skills_config": {
      "skills_dir": "config/my-profile/skills"
    }
  }
}
```

Structure the directory as `skills/<skill-name>/SKILL.md`. Each file uses YAML frontmatter + Markdown body:

```markdown
---
name: My Skill
description: Short description of what this skill does.
when-to-use: Use when the user asks to do X.
allowed-tools: search_graph get_related_nodes add_nodes
effort: low
version: "1.0"
---

## Steps
1. ...

## Output format
...
```

| Frontmatter field | Description |
|-------------------|-------------|
| `name` | Required. Human-readable skill name |
| `description` | Short summary shown in skill listings |
| `when-to-use` | Instructs the AI when to activate this skill |
| `allowed-tools` | Space-separated list of tool names the skill may use |
| `effort` | `low`, `medium`, or `high` — indicates task complexity |
| `version` | Version string for tracking changes |

**Full `skills_config` options:**

```json
{
  "skills_config": {
    "skills_dir": "config/my-profile/skills",
    "allow_external_skills": true,
    "trusted_domains": ["github.com", "raw.githubusercontent.com", "agentskills.io"],
    "cache_ttl_seconds": 3600,
    "max_skill_content_bytes": 50000,
    "max_skill_body_chars": 8000
  }
}
```

Skills can also be loaded from external URLs via `skills_urls` on expert agent configs, or fetched from GitHub repos by pointing to the repo URL.

**Shipped skills:**

| Skill | Profile | Description |
|-------|---------|-------------|
| Impact Analysis | default (all profiles) | Traces which nodes depend on a given node and assesses what would be affected by a change. Uses standard graph tools only. |
| Graph Analysis | stat-metadata | Analyses graph patterns, clusters, hub nodes, and non-obvious connections. |
| GSIM Lineage & Change Impact | stat-metadata | Traces data lineage through the GSIM metadata chain and assesses impact of classification/code list version changes. Uses `get_lineage`, `assess_change_impact`, `get_impact_report`. |

#### Skill node type and creation form

Any schema node type with `"ui_form": "skill"` uses the SKILL.md-compatible creation dialog instead of the generic node form. The dialog provides fields for name, description, when-to-use, content (Markdown body), source URL, allowed tools, version, and effort.

```json
{
  "Skill": {
    "ui_form": "skill",
    "category": "system",
    "fields": ["name", "description", "summary", "metadata"]
  }
}
```

Skills created this way are stored in the graph as nodes. Agent workers automatically discover linked Skill nodes and include their content in the system prompt.

### 6. Add environment variables (optional)

```bash
cp config/default/.env.example config/my-profile/.env
# Edit with your secrets
```

### 7. Add seed data (optional)

Place a `graph.json` in the profile directory. It will be used as the initial dataset when no active data exists:

```bash
cp data/examples/default.json config/my-profile/graph.json
# Edit with your seed data
```

## MCP Integration

The metadata model is automatically exposed to MCP clients (like ChatGPT). When the server starts, it builds dynamic instructions that include:

- All node types with their descriptions
- All relationship types
- The `prompt_prefix` as domain context

This means MCP clients understand your custom domain concepts without additional configuration.

## Example: SCB Profile

The `config/scb/` profile demonstrates a domain-specific configuration for Statistics Sweden (SCB). It adds node types like:

- **Dataset** — Statistical datasets
- **Hållpunkt** — Stable data product commitments
- **Undersökning** — Statistical surveys
- **Variabel** — Statistical variables
- **Värdemängd** — Value sets / code lists
- **Population** — Target populations
- **Klassifikation** — Statistical classifications (SNI, SSYK, etc.)

These are in addition to the common types (Actor, Initiative, Resource, etc.) that are shared across profiles.

## System Node Types

Certain node types are always present regardless of profile:

- **SavedView** — Saved graph view snapshots (auto-injected if missing)
- **VisualizationView** — Legacy saved views (auto-injected if missing)

Other system types (Agent, EventSubscription) should be defined in each profile's schema config if needed.

## Toolbar Layout

The left-side toolbar in the UI is driven by the schema:

- **Domain types** appear first (in schema definition order)
- **System types** (Agent, EventSubscription, Group) appear after a separator
- **SavedView** appears last after another separator

The toolbar uses a two-column grid layout to accommodate many node types.
