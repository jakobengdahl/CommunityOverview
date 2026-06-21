# Plugin Runtime Core Enablement

## Purpose

This document defines the **generic, public-core technical work** required to make CommunityOverview ready to load, discover, and manage plugins at runtime. It covers only the core infrastructure layer — the seams that any future optional extension layer (internal or external) would need to integrate cleanly.

This document deliberately omits distribution strategy, business rules, and non-technical policy. Those concerns belong outside the public core repository and must not constrain the generic runtime design.

---

## Scope

The work described here enables:

- loading plugins from well-defined entry points
- discovering and advertising what plugins are available and active
- enforcing permission and scope boundaries at the API layer
- versioning the plugin API contract so plugins can evolve safely
- providing controlled namespace and export hooks
- supporting migration hooks for schema or data changes introduced by plugins
- making plugin behavior testable in isolation
- exposing runtime introspection for active plugins and their health
- failing safely when a plugin is missing, incompatible, or misbehaving

It does **not** define:
- which specific plugins exist
- how plugins are distributed or installed
- business review or approval policy
- revenue or business-system integration
- any implementation details that belong to downstream embedding systems rather than the core runtime

---

## Core requirements

### 1. Plugin manifest parsing

Each plugin must declare its identity and requirements in a structured manifest.

**Minimum manifest fields:**
- `id` — unique stable identifier (reverse-domain style, e.g. `org.example.my-plugin`)
- `version` — semver string
- `api_version` — the plugin API version this plugin was built for
- `entry_point` — module or package path relative to the plugin root
- `permissions` — list of requested scopes (see §3)
- `capabilities` — list of capability identifiers this plugin contributes
- `schema_migrations` — ordered list of migration descriptors (see §6)

The core manifest parser must:
- validate required fields before any other loading step
- reject manifests with unknown or malformed fields
- surface parse errors as structured diagnostics, not exceptions that crash startup
- remain forwards-compatible: unknown manifest fields should produce a warning, not a hard failure, unless the field is in a `required_fields` list

### 2. Capability discovery

The core must expose a stable, generic capability registry that any plugin can register into and any consumer (UI, API client, external automation) can query.

**Required behaviors:**
- capability registration happens at plugin load time, before the plugin services requests
- capabilities are identified by a structured name (`namespace.capability-name`)
- registered capabilities include: id, label, description, providing plugin id, and active status
- the capability list is queryable through both REST (`GET /api/capabilities`) and MCP tool
- query results are stable in shape even when no plugins are loaded (returns empty list, not an error)
- deregistration happens automatically when a plugin is unloaded or disabled

**Avoidance constraints:**
- capability names must not encode commercial tiers or private identifiers
- the registry must not require a central plugin directory service to function

### 3. Permission and scope enforcement seams

The core must enforce a permission model at the API boundary so plugins cannot access resources beyond what they declared in their manifest.

**Design:**
- define a set of named scopes covering the core resource surfaces (e.g. `graph:read`, `graph:write`, `config:read`, `events:publish`, `admin:inspect`)
- plugins request a subset of these scopes in their manifest
- the core grants only requested scopes; plugins cannot self-elevate
- every plugin-initiated API call or internal service call must carry its granted scope set
- the enforcement layer must be a clearly isolated middleware or decorator, not scattered inline checks
- denials must produce structured error responses, not silent no-ops or crashes

**Extension:**
- the scope vocabulary must be extensible by adding new named scopes without breaking existing plugins that do not request them

### 4. Plugin API versioning

The core must provide a stable, versioned contract for what plugins can call into.

**Required behaviors:**
- the plugin API exposes a declared version (e.g. `PLUGIN_API_VERSION = "1.0"`)
- at load time, the core checks the plugin's `api_version` against the current API version
- version compatibility rules:
  - same major version: compatible
  - different major version: rejected with a descriptive error
  - newer minor version requested than available: warning, load continues with degraded capability
- the plugin API version is advertised through the runtime introspection endpoint (see §7)
- the public API surface available to plugins is documented and must not change within a major version without a deprecation cycle

### 5. Namespace and export hooks

Plugins must be isolated from each other's internals. The core must provide controlled export hooks for cross-plugin or plugin-to-core communication.

**Design:**
- each plugin is loaded into an isolated namespace
- plugins may not directly import from each other
- the core exposes a hook registry where plugins can publish named hooks and consumers can subscribe
- hooks are identified by `namespace.hook-name`
- hook signatures are typed and validated at registration time
- the core provides a small set of built-in hooks for common extension points (e.g. `core.graph.node-created`, `core.config.updated`)
- plugins may register additional hooks; these become part of the plugin's declared API surface
- hook dispatch is synchronous by default; async hooks require explicit declaration in the manifest

### 6. Migration hooks

Plugins that introduce schema or data changes must declare and run migrations in a controlled, ordered way.

**Design:**
- migration descriptors in the manifest reference migration modules by path
- each migration module exposes: `id`, `description`, `up()`, and `down()`
- the core migration runner:
  - records applied migrations in a stable migration log
  - applies `up()` migrations in declaration order on first plugin activation
  - applies `down()` migrations in reverse order on plugin deactivation (if supported)
  - refuses to activate a plugin if its migrations conflict with recorded state
- migration failures are surfaced as structured errors and block plugin activation; they must not silently corrupt state
- plugin migrations run in their own transaction scope where the underlying storage supports it

### 7. Runtime introspection

The core must provide a stable endpoint for inspecting the active plugin runtime state.

**Required output (per plugin):**
- plugin id and version
- active/inactive/error status
- granted scopes
- registered capabilities (list of ids)
- registered hooks (list of ids)
- api_version the plugin declared
- last activation timestamp (if available)

**Endpoint:** `GET /api/runtime/plugins` (REST) and equivalent MCP tool.

**Constraints:**
- the endpoint must be available even when no plugins are loaded
- error-state plugins must appear in the list with their error details, not be silently dropped
- the output format must be stable across patch releases and must version-bump on breaking changes

### 8. Failure behavior

Plugin failures must not destabilize the core application.

**Required behaviors:**
- manifest parse failure: log structured error, skip plugin, continue startup
- incompatible API version: log structured error, skip plugin, continue startup
- permission grant failure: log structured error, skip plugin, continue startup
- migration failure: log structured error, skip plugin, do not apply partial migrations
- runtime exception from plugin code: catch at the plugin boundary, log structured error, mark plugin as error-state, continue serving other requests
- the core must expose a per-plugin health state through the runtime introspection endpoint (see §7)
- plugin failures must never suppress the core health/readiness signals; they may contribute a degraded signal only if a plugin is declared `required` in the runtime config

### 9. Testability

Plugin runtime behavior must be testable without requiring real plugin packages.

**Required test infrastructure:**
- a fixture factory for minimal valid manifests
- a test double for the capability registry (reset between tests)
- a test double for the hook registry (reset between tests)
- a test double for the migration runner (in-memory migration log)
- test helpers for asserting scope enforcement (simulate a plugin call with a given scope set)
- test helpers for asserting introspection output shape
- test coverage requirements:
  - manifest parsing: valid, missing required fields, unknown fields, version mismatch
  - capability registration and deregistration
  - scope grant and denial
  - migration apply, skip (already applied), and failure rollback
  - runtime introspection shape with zero, one, and multiple plugins
  - failure isolation: one bad plugin must not affect others in the same test

---

## Public/private boundary

| In scope (public core) | Out of scope (distribution / business layer) |
|---|---|
| Manifest parsing and validation | Plugin distribution and packaging format |
| Capability registry and discovery | Business review or approval workflows |
| Permission and scope enforcement | Product-specific labeling or certification schemes |
| Plugin API versioning contract | Revenue model or business-system integration |
| Namespace and export hooks | Downstream plugin catalogs or registries |
| Migration hooks and runner | Deployment-specific plugin orchestration |
| Runtime introspection endpoint | Internal operating procedures for downstream teams |
| Failure isolation and error surfaces | Product-tier-specific plugin classification |
| Test infrastructure for the above | Any non-core extension implementation |

---

## Relationship to other plans

- **CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md** — defines the broader runtime and extension enablement context; the extension-and-plugin section is the direct precursor to the work here
- **CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md** — defines concrete implementation slices; Priority 1 (capability manifest and discovery) is the first slice that feeds into this plan

Plugin runtime enablement sits between those two documents: it takes the generic seam design from the broader runtime plan and elaborates what the plugin-specific runtime layer must actually implement before any plugin can be safely loaded.

---

## Recommended implementation sequence

1. define and parse the plugin manifest schema (§1)
2. implement scope vocabulary and enforcement middleware (§3)
3. implement capability registry with REST and MCP exposure (§2)
4. implement plugin API version check at load time (§4)
5. implement namespace isolation and hook registry (§5)
6. implement migration runner and migration log (§6)
7. implement runtime introspection endpoint (§7)
8. harden failure isolation at every plugin boundary (§8)
9. ship test infrastructure alongside each of the above (§9)

Each step should be independently mergeable with passing tests before the next begins.

---

## Success criteria

This work is done when:
- a plugin with a valid manifest can be loaded, its capabilities discovered, and its hooks registered without modifying core internals
- a plugin requesting an undeclared scope is denied at the API boundary, not at an ad-hoc check inside a handler
- a plugin with an incompatible API version is refused cleanly at startup
- a plugin that throws a runtime exception does not affect other plugins or core request handling
- a plugin's migrations are applied, recorded, and reversible without manual intervention
- the runtime introspection endpoint returns accurate, stable output for zero through N plugins
- the full plugin lifecycle is exercisable in tests without shipping real plugin packages
