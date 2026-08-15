# Available Icons for Node Types

This is the reference list of icon names you can use in the `icon` field of a
node type definition in `schema_config.json` (see
[`docs/PROFILES.md`](PROFILES.md#3-define-node-types)).

## How icon selection works

```json
{
  "MyNodeType": {
    "icon": "GlobeEuropeAfricaFill"
  }
}
```

The `icon` value is a name from the
[Bootstrap Icons](https://icons.getbootstrap.com/) set, rendered via the
[`react-bootstrap-icons`](https://www.npmjs.com/package/react-bootstrap-icons)
package. It must match, **exactly**, a key in the `ICON_REGISTRY` object in
[`frontend/web/src/components/FloatingToolbar.jsx`](../frontend/web/src/components/FloatingToolbar.jsx).

This is the constraint to keep in mind: `ICON_REGISTRY` only contains the
icons that were imported and built into the frontend at deployment time.
Bootstrap Icons ships **2,000+** icons, but a config can only select from the
subset already registered — an `icon` value that isn't a registered key
silently falls back to the built-in default for that type name, and then to a
neutral filled circle (`CircleFill`); it does not raise an error. A type that
declares no `icon` and has no built-in default for its name gets the same
neutral circle. If a type shows the plain circle instead of the icon you
picked, double check the exact spelling against the table below (names are
case-sensitive PascalCase) and that the name is registered.

The tables below list every icon currently registered and available for
selection, grouped by theme, so you don't need to search the full Bootstrap
Icons library and guess whether a name made it into the build.

## People & Organizations

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `PersonFill` | 👤 | Individuals, contacts |
| `PeopleFill` | 👥 | Groups, populations, teams |
| `BuildingFill` | 🏢 | Institutions, single organizations |
| `BuildingsFill` | 🏬 | Multiple organizations, federations |
| `Bank` | 🏦 | Financial institutions, regulators |

## Geography & International

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `GlobeEuropeAfricaFill` | 🌍 | European/international scope, cross-border initiatives |
| `GeoAltFill` | 📍 | Locations, regional entities |
| `MapFill` | 🗺️ | Territories, geographic datasets |
| `PinAngleFill` | 📌 | Anchor points, stable reference items |
| `CompassFill` | 🧭 | Navigation, orientation, strategy direction |

## Statistics & Analytics

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `DatabaseFill` | 🗄️ | Datasets, data sources |
| `ClipboardDataFill` | 📋 | Surveys, investigations |
| `BarChartFill` | 📊 | Indicators, statistical programmes |
| `PieChartFill` | 🥧 | Breakdown / distribution data |
| `GraphUpArrow` | 📈 | Trends, growth indicators |
| `Sliders` | 🎚️ | Variables, measurements |
| `ListOl` | 📝 | Value sets, ordered code lists |
| `ListCheck` | ✅ | Checklists, code lists |

## Documents & Data

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `FileEarmarkTextFill` | 📄 | Generic resources, documents |
| `FileEarmarkSpreadsheetFill` | 📊 | Spreadsheets, tabular data exports |
| `FileEarmarkBarGraphFill` | 📈 | Reports with embedded analytics |
| `FileEarmarkCodeFill` | 💻 | Technical specs, schemas, code artifacts |
| `JournalBookmarkFill` | 🔖 | Reference documents, standards |
| `BookFill` | 📕 | Publications, manuals |
| `Bookshelf` | 📚 | Collections of documents, libraries |
| `ClipboardCheckFill` | ✔️ | Approved/validated items |
| `Clipboard2DataFill` | 📊 | Data collection instruments |
| `CardChecklist` | 🗒️ | Checklists, structured forms |
| `InputCursorText` | ⌨️ | Text input fields, free-text variables |
| `CollectionFill` | 🗂️ | Curated collections, active knowledge sets |

## Security & Legal

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `ShieldFillCheck` | 🛡️ | Legislation, compliance |
| `ShieldLockFill` | 🔒 | Data protection, confidentiality controls |
| `ShieldFillExclamation` | ⚠️ | Compliance risks, legal warnings |
| `ExclamationTriangleFill` | ⚠️ | Risks, general warnings |
| `LockFill` | 🔐 | Restricted access items |
| `KeyFill` | 🔑 | Credentials, access management |

## Technology & Systems

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `CpuFill` | 💻 | Agents, AI, automated processes |
| `Robot` | 🤖 | AI agents, bots |
| `MotherboardFill` | 🖥️ | Infrastructure, technical systems |
| `RouterFill` | 📡 | Networks, connectivity |
| `GearFill` | ⚙️ | Configuration, settings, tools |
| `GearWideConnected` | ⚙️ | Integrated systems, production pipelines |

## Communication & Collaboration

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `BellFill` | 🔔 | Notifications, event subscriptions |
| `ChatFill` | 💬 | Discussions, chat-based capabilities |
| `EnvelopeFill` | ✉️ | Correspondence, contact channels |
| `MegaphoneFill` | 📣 | Announcements, communication initiatives |
| `FunnelFill` | 🔻 | Filters, curated selections |

## Goals, Education & Recognition

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `TrophyFill` | 🏆 | Goals, objectives |
| `StarFill` | ⭐ | Highlights, featured items |
| `AwardFill` | 🎖️ | Achievements, certifications |
| `MortarboardFill` | 🎓 | Training, education, capability building |
| `Bullseye` | 🎯 | Targets, strategic focus areas |
| `FlagFill` | 🚩 | Milestones, markers |
| `LightbulbFill` | 💡 | Ideas, concepts, insights |
| `RocketTakeoffFill` | 🚀 | Projects, initiatives |
| `LightningFill` | ⚡ | Capabilities, skills |

## Structure, Navigation & Misc

| Icon Name | Visual | Suggested For |
|-----------|--------|----------------|
| `TagsFill` | 🏷️ | Themes, categories |
| `CalendarEventFill` | 📅 | Events, milestones |
| `Diagram2Fill` | 🔗 | Two-way relationships, mappings |
| `Diagram3Fill` | 🔀 | Classifications, taxonomies |
| `KanbanFill` | 🗂️ | Workflow stages, process tracking |
| `Boxes` | 📦 | Inventories, packaged resources |
| `LayersFill` | 🗂️ | Layered structures, stacked data |
| `GridFill` | ▦ | Structured/tabular layouts |
| `PuzzleFill` | 🧩 | Components, modular capabilities |
| `BinocularsFill` | 🔭 | Monitoring, foresight, observation |
| `EyeFill` | 👁️ | Oversight, visibility, review |
| `Translate` | 🌐 | Multilingual content, localization |
| `BookmarkFill` | 🔖 | Saved views |
| `FolderFill` | 📁 | Groups, folders |
| `QuestionCircleFill` | ❓ | Open questions, deliberately unknown items |
| `CircleFill` | ⬤ | Generic node; the fallback when no icon is configured |

## Adding an icon that isn't in this list

If none of the icons above fit, you can register a new one:

1. Confirm the icon exists in [Bootstrap Icons](https://icons.getbootstrap.com/)
   and note its exact PascalCase name (the site shows the React component
   name directly).
2. In `frontend/web/src/components/FloatingToolbar.jsx`, add it to the
   `react-bootstrap-icons` import list and to the `ICON_REGISTRY` object.
3. Add a row to the relevant table above so the next person configuring a
   profile knows it's available.
4. This requires a code change and a new deployment before the icon can be
   referenced from a profile's `schema_config.json` — a config-only change
   cannot make a new icon selectable.
