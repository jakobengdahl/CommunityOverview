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
  scb/                        # Example: SCB (Statistics Sweden) profile
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

**Available icon names** (from [Bootstrap Icons](https://icons.getbootstrap.com/)):

| Icon Name | Visual | Suggested For |
|-----------|--------|---------------|
| `PersonFill` | 👤 | People, organizations |
| `RocketTakeoffFill` | 🚀 | Projects, initiatives |
| `DatabaseFill` | 🗄️ | Datasets, data sources |
| `LightningFill` | ⚡ | Capabilities, skills |
| `FileEarmarkTextFill` | 📄 | Resources, documents |
| `ShieldFillCheck` | 🛡️ | Legislation, compliance |
| `TagsFill` | 🏷️ | Themes, categories |
| `TrophyFill` | 🏆 | Goals, objectives |
| `CalendarEventFill` | 📅 | Events, milestones |
| `ExclamationTriangleFill` | ⚠️ | Risks, warnings |
| `PinAngleFill` | 📌 | Anchor points, stable items |
| `ClipboardDataFill` | 📋 | Surveys, investigations |
| `PeopleFill` | 👥 | Populations, groups |
| `Sliders` | 🎚️ | Variables, measurements |
| `ListOl` | 📝 | Value sets, code lists |
| `Diagram3Fill` | 🔀 | Classifications, taxonomies |
| `CpuFill` | 💻 | Agents, AI |
| `BellFill` | 🔔 | Notifications, webhooks |
| `BookmarkFill` | 🔖 | Saved views |
| `FolderFill` | 📁 | Groups, folders |

To add support for additional icons, add the import to `frontend/web/src/components/FloatingToolbar.jsx` in the `ICON_REGISTRY` object.

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
