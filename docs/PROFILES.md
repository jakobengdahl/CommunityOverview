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

# Combine with language and data options (--lang is currently inert; the UI
# is temporarily locked to English — see docs/USER_GUIDE.md#language)
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
  },
  "ui": {
    "ai_assistant": {
      "default_collapsed": false
    }
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
| `color` | No | Hex color code for UI display. A declared color is authoritative: it takes precedence over the built-in color a known type name would otherwise get, so a profile can recolor `Actor`, `Capability`, `Theme` and the other built-in types. Omitted, it defaults to `#9CA3AF` (gray) — the built-in colors only cover type names a profile does not declare at all. Note that a `presentation.colors` entry for the same type overrides this value on the canvas and on the other node-scoped surfaces (the create/edit/detail dialogs, node history, chat type chips); the toolbar, search results and the node-type statistics lists use this value directly (see the presentation section below) |
| `icon` | No | Bootstrap Icon name for the toolbar (e.g. `"DatabaseFill"`, `"PeopleFill"`). Omitted or unregistered names fall back to the built-in icon for known type names, otherwise to a neutral circle |
| `static` | No | If `true`, nodes of this type cannot be created via the chat. Used for system types |
| `ui_form` | No | Specialized creation dialog. `"skill"` opens the SKILL.md-compatible form |
| `labels` | No | Localized display names for this node type, keyed by language code (e.g. `{"sv": "Aktör"}`). Its live effect is on **search**: every label value, in every language, is joined into the type's lexical search text, so a query like `aktör` matches nodes of that type — in local and in federated results alike (see `backend/DEVELOPMENT.md`). Removing a label narrows search silently. As a *display* name it is looked up by the active UI language — always `en` while the UI is locked to English (see above) — so an `en` entry does render as the type name in search results and the metamodel explorer, while entries for other languages stay dormant until language switching returns; whenever the active language has no entry the path falls back to the raw type name. No shipped profile declares an `en` label, so none shows a translated type name today. The metamodel explorer's `Label (sv)` column is separate: it always shows the `sv` entry whatever the UI language, and `—` when unset |
| `context_menu` | No | Array of extra items for the right-click context menu (see below) |
| `allows_attachments` | No | `true` marks this node type as accepting file attachments. Defaults to `false`. Returned by `get_schema` on every node type, so clients can read it unconditionally. Per [ADR 0008](adr/0008-node-attachment-storage.md) section 7 it gates uploads only — listing, downloading and deleting files already attached stay available, so turning it back off never strands them. Uploads to a type without it answer `422 attachments_not_allowed`; the endpoints are listed in `backend/DEVELOPMENT.md` (*Node attachments*) |

**A malformed node type is dropped on its own.** These fields are validated
per node type when the profile loads. A value of the wrong type for any of them
— `"allows_attachments": "ture"`, a `null`, a list where a bool belongs — drops
that one node type and logs a warning naming the type and the field; the
profile's other node types and its relationship types still load, and the
profile behaves as if the dropped type were not declared. Nothing else
reports the dropped type, so check the startup log after editing. Boolean
fields do accept the usual spellings (`true`, `"true"`, `"yes"`, `1`), so only
genuinely unparseable values trip this.

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
    "default_language": "en",
    "default_chat_collapsed": false
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
| `default_chat_collapsed` | Whether the assistant panel starts collapsed for a visitor with no stored preference of their own (default `false`, i.e. open). A visitor's own explicit open/collapse choice, once made, is stored in their browser and takes precedence over this on every later visit until they reset it |
| `theme` | Optional accent colour for the web UI: `accent`, `accent_hover`, `accent_contrast` (see [THEMING.md](THEMING.md)). An unknown key or a value that is not a plain CSS colour is skipped with a warning |
| `skills_config` | Skills loader settings (see below) |

The optional top-level `ui.ai_assistant.default_collapsed` boolean controls the
assistant's initial state for browsers that have not made a choice. It defaults
to `false`, preserving the expanded assistant. Expanding or collapsing the
assistant stores a browser-local preference under
`community-graph:ui:ai-assistant-collapsed`; that preference takes precedence
over profile configuration on later visits. It does not require an account and
does not affect deployments where the assistant is unavailable.

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
- `options.max_tokens` (a JSON integer) sets the completion token limit for chat and agent calls with this profile. It takes precedence over the `LLM_MAX_TOKENS` environment variable, which takes precedence over the default of 16000. An invalid or non-positive value falls back to the default with a warning. Lower it for a model that rejects the default as too large. The Claude provider caps the limit to what the installed Anthropic SDK allows without streaming, with a warning
- `options.enable_thinking: false` sends `chat_template_kwargs: {"enable_thinking": false}` on requests to an OpenAI-compatible model that would otherwise spend its token limit on reasoning. It takes precedence over the `LLM_ENABLE_THINKING` environment variable, so `true` here keeps thinking on for this profile. The Claude provider never sends it

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
    "max_skill_body_chars": 8000,
    "mcp": {"enabled": false, "fallback": true, "ttlMs": 0, "cacheScope": "private",
            "graphSemantics": {"ttlMs": 0, "cacheScope": "private"}}
  }
}
```

The optional `skills_config.mcp` section controls the MCP skills extension.
`enabled` defaults to `false`; when enabled, clients can discover the core
working-method skill and a graph-semantics skill and read their files through MCP resources. The latter describes the loaded schema: node types, fields, declared subtypes, relationships, vocabularies, language policy and prompt prefix. It contains no graph data. Its snapshot date is a deterministic identifier derived from the schema, not a modification time; content statistics are available through `describe_graph`. `ttlMs` is a
non-negative freshness hint in milliseconds (default `0`), and `cacheScope` is
`private` (the default) or `public`. Use `private` whenever content or access
can vary between callers. Besides the built-in skills, the server serves
Skill nodes from the graph (see below); `create_app` does not accept additional providers. Third-party and remote
providers are planned for a follow-up slice. The configured cache scope is used
as supplied; it is not automatically changed for additional providers. `graphSemantics.ttlMs` and `graphSemantics.cacheScope` independently configure the schema skill, with the same defaults and validation as the working-method hints. Modern clients receive per-skill hints in the list and get results.
The default authorization hook permits configured public cache scopes; custom
authorization hooks and additional providers use private scope because access may vary
between callers. The extension, resources and fallback tools check graph read
access on every request. These cache hints do not replace authorization or
verification of file digests.

With MCP skills enabled, `refresh_skills` rebuilds the catalog after an admin
authorization check. Its per-skill results include the frontmatter metadata version,
or a SHA-256 version derived from the sorted file manifest when none is supplied.
The manifest includes the file digests. A change in file names or digests publishes one standard
`notifications/resources/list_changed` event to each `subscriptions/listen`
subscriber. The publisher uses an in-process bus; clients must refresh their
catalog and cannot assume every host reacts to changes. The `fallback` setting
(default `true`) adds `list_skills` and `get_skill` tools for clients without
the skills extension; `get_skill` accepts a skill URI and an optional relative
file path. Set `fallback` to `false` to omit those tools. They use the same
validated snapshot and access checks as the extension.
Skills with more than 512 files, more than 16 MiB, or rejected content prevent
startup; files are sanitized before their digests are calculated.

#### Skill nodes as MCP skills

With MCP skills enabled, every Skill node with `metadata.content` that is not
archived is also served as a skill. `metadata.content` becomes the body of
`SKILL.md` (a frontmatter block in it is replaced); the frontmatter is
generated from the node: `name` from the node name (lower-case, hyphenated),
`description` from the node description or `when_to_use`, `allowed-tools` from
`allowed_tools`, and `metadata` with `node-id`, `revision` (the node revision)
and `version` when set. The optional `metadata.skill_path` sets the skill URI
path, for example `team/review` serves `skill://team/review/SKILL.md` with the
name `review`. When MCP attachment reading is enabled for the instance, the
node's UTF-8 text attachments are served as `references/<filename>`; binary
attachments are not served.

Skill nodes go through the same validation and sanitisation as the built-in
skills, but an invalid node — rejected content, more than 512 files or 16 MiB,
a name or path already in use, or a path inside another skill — is skipped
with a warning instead of preventing startup. On a name or path clash the
node already being served keeps it and every clashing newcomer is withheld;
after a restart no node is being served yet, so every clashing node is
withheld. A caller sees a Skill node's skill
only when it may read that node under the node-read access rules; a skill with
attachment files additionally needs the access `read_node_attachment` needs
(`get_node_attachment` and `read_node_attachment_content`, including their
graph narrowing), and each served attachment file read is audited as an
`attachment_content_read`. As soon as the graph serves a Skill node, every
skill uses `cacheScope: private`. The catalog is a snapshot: `refresh_skills`
picks up created, changed or deleted Skill nodes; an archived or deleted node
is hidden at once. Agents (`skill_node_ids` on Agent nodes) and expert agents
in the assistant (`skill_node_ids` on expert agent configs, which needs no
`skills_urls`) read Skill nodes through the same provider, so they get the same
sanitised body. With MCP skills disabled, agents and experts still read their
linked Skill nodes, but nothing is served over MCP.

Skills can also be loaded from external URLs via `skills_urls` on expert agent configs, or fetched from GitHub repos by pointing to the repo URL.

`trusted_domains` is necessary but not sufficient. Every skill URL is also
checked against the same SSRF address guard the webhook, image-ingest and
agent-fetch paths use: a hostname that resolves to a private, loopback,
link-local, CGNAT or reserved address is refused even when its domain is
allowlisted, and there is no config override. A SKILL.md served from an
internal host is therefore not a supported configuration. Redirects are not
followed automatically — each hop must satisfy both `trusted_domains` and the
address guard, so an allowlisted host cannot redirect a fetch off the
allowlist or inward.

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

## Example: stat-metadata Profile

The `config/stat-metadata/` profile models European Statistical System metadata.
Its metamodel covers the MetaPlus concepts while deliberately keeping the number
of node types small:

- **DataSet** — statistical datasets. Register structure is expressed with the
  subtypes `Register`, `RegisterVariant` and `RegisterVersion` rather than with
  separate node types.
- **Variable** — statistical variables. Subtypes carry both the conceptual level
  (`RepresentedVariable`, `InstanceVariable`) and the semantic role (`Identifier`,
  `Measure`, `Attribute`), and one node may hold several. Column-level details
  (`column_name`, `data_type`, `length`, `format`) are attributes on the variable;
  there is no separate Column node type.
- **Population** — the set of units a dataset or register version describes.
  Version-specific timing is carried on the `HAS_POPULATION` edge.
- **ValueDomain** — permissible values, with the subtypes `EnumeratedValueDomain`,
  `NumericRangeValueDomain` and `DescribedValueDomain`.
- **CodeList** — simple, normally flat value lists. Structured entries live under
  the metadata key `codes` as `[{code, label}, …]`.
- **Classification** / **ClassificationItem** — formal, managed and often
  hierarchical classifications (NACE, NUTS, COICOP) and their codes. The hierarchy
  is expressed with `HAS_CHILD_ITEM` between items rather than with level nodes.

`Variable` replaced `InstanceVariable` as the primary node type. Existing data is
migrated with `scripts/migrate_stat_metadata_metaplus.py`, which converts each
`InstanceVariable` node to `Variable` while adding `InstanceVariable` as a subtype,
so nothing about a variable's meaning is lost. `InstanceVariable` remains a valid
subtype; it is no longer a primary type.

A `CodeList` becomes a `Classification` only when it explicitly says it is one —
`metadata.is_classification`, or the `Statistical Classification` subtype. A node
whose name merely looks like a classification is left alone and reported. This
profile's data is demonstration data, and some of its nodes were stubs, so
`demo_enrichment.json` supplies the metadata they were missing before that rule
runs (`--enrich`). Keeping enrichment in its own reviewable file means the
conversion rule stays strict and every node that passes only because of an
enrichment is visible in one place.

The profile's `graph.json` is example data only. Runtime state — `EventSubscription`, `Agent` and `Skill` nodes, as well as
legacy `SavedView` and `VisualizationView` nodes — does not belong in it. A seed captured by
exporting a live instance carries that state along, so run
`scripts/strip_profile_runtime_nodes.py` over any such export before committing it:
it removes those node types and every edge touching them, drops the file's
`metadata.journal_id`, and refuses to write if the result would leave a dangling
edge. Watch for what rides along in them — `EventSubscription` nodes carry webhook
URLs, which is how a personal lab hostname once reached this repository. The
`journal_id` is the lineage the graph journal beside a running instance's
`graph.json` belongs to (see `DATA_MANAGEMENT.md`, *Graph Journal*); an export
through the app never carries it, but a `graph.json` copied by hand does, and a
seed that keeps it gives every instance seeded from it the same lineage. A test
(`backend/tests/test_strip_profile_runtime_nodes.py`) fails if a committed seed
carries the key.

Metadata keys on `Variable` are snake_case, matching the convention every other
profile follows. Metadata keys on `DataSet`, `DataStructure` and
`StatisticalProgramme` are snake_case and declared in the profile fields. A `Variable` node's
semantic role is carried by its subtypes (`Identifier`, `Measure`, `Attribute`)
and nowhere else — some nodes still hold a leftover `role` metadata key from an
earlier import, which is deliberately left undeclared: declaring it would give
the UI a second, separately editable copy of the subtype that can drift out of
agreement with it.

Two values are documented rather than schema-validated in this step:

```text
sensitive_personal_data_status: confirmed | not_confirmed | context_dependent | unknown
identity_status:                confirmed | not_confirmed | potential | unknown
```

## System Node Types

The loader injects these node types from code (`SYSTEM_NODE_TYPES` in `backend/config/config_loader.py`) on every load, regardless of profile:

- **SavedView** — Legacy graph data type, retained for compatibility
- **VisualizationView** — Legacy graph data type, retained for compatibility
- **EventSubscription** — Webhook subscription for graph mutation events
- **Agent** — AI agent configuration
- **Skill** — Reusable AI skill definition
- **ActiveKnowledgeCollection** — Structured data-gathering collection configuration
- **CollectionResponse** — A single submission gathered by an ActiveKnowledgeCollection

Do not declare these in a profile's `schema_config.json`: a profile's own declaration of a system type is removed at load time (with a warning) and replaced by the code-managed definition, so it has no effect.

To omit a system type from a profile, list its name in `system.disabled_node_types`, for example `"system": {"disabled_node_types": ["Skill"]}`. Names not in the list above are logged as unrecognised and ignored.

## Toolbar Layout

The left-side toolbar in the UI is driven by the schema:

- **Domain types** appear first (in schema definition order)
- **System types** (Agent, EventSubscription, Group) appear after a separator

The toolbar uses a two-column grid layout to accommodate many node types.

### Profile templates

Place individual v1 template JSON documents in `config/<profile>/templates/` alongside the active schema configuration. Valid documents load at startup and appear as read-only templates; invalid documents are logged and skipped. IDs already used by another profile or stored template or session (including deleted records) prevent startup; resolve the ID collision before restarting. See [Templates contract](TEMPLATES_CONTRACT.md) for the document format.


### MCP graph content profile

The `describe_graph` tool and read-only `graph://profile` resource return the
same deterministic statistics for the local graph. Each read recomputes the
profile; there is no cache or TTL. The caller's graph read access filters nodes
and both endpoints of edges, and archived nodes and edges are excluded. In a
staged session the profile is computed from the session's composed view, like
other composing reads.

`node_count` is the visible total. `node_types` and `subtypes` contain counts;
`tags` counts nodes carrying each tag. `relationships` reports each directed
source type, relationship type and target type, the number of distinct source
nodes with at least one matching edge, and that count divided by the visible
source-type count. `central_nodes` ranks nodes by visible incident edge count,
including zero-degree nodes, with ties ordered by ID. Each list is capped at 100
entries. Each string is truncated to 256 Unicode characters in the response;
ordering and counts use the original values, so truncated labels can coincide.
Types, subtypes and relationships are ordered by name; tags are ordered
by descending count then name. A self-loop contributes two to degree.

When MCP skills are enabled, each listen stream compares its own authorized
profile, evaluated with the listen request's own authorization headers,
and receives the standard
`notifications/resources/updated` for `graph://profile` only when that profile
differs from its baseline or the one at its last notification. The baseline is
taken in the background right after the stream subscribes, or within one
debounce window (250 ms) if a comparison is already pending; a change landing
before it is part of the baseline. Comparisons run at most once per debounce
window, from one snapshot of the graph shared by every stream, and once per
distinct access scope; the storage write lock is held only while the snapshot's
plain fields are copied.
Graph changes, whole-graph imports and reloads (including an external change applied as
a reload) all trigger the comparison. While its read access is denied nothing
is sent, and access returning to an unchanged profile announces nothing. A stream that has never been able to read the profile is notified on
the first graph change after access is granted. The comparison uses the main
graph, not a staged session's composed view: staged session writes send no
notification, and a main-graph change masked by a session layer still sends
one. Notifying from a session's composed view is not yet implemented. The
notification carries no profile content. With skills disabled the
bus is absent and no notification is published. Graph content does
not change skill files or their digests.
