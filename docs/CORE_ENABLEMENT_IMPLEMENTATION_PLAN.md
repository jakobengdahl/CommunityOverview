# Core Enablement Implementation Plan

**Goal:** Define the first concrete implementation slices that make CommunityOverview more ready for embedded deployment and optional extensions, without introducing product-specific assumptions into the public core.

**Architecture:** The core should expose generic, opt-in seams rather than hard-coded integration logic. The current priorities are to make identity, workspace scope, graph scope, and audit attribution pluggable without forcing built-in assumptions into standalone deployments, and to improve capability discovery and runtime-mode introspection so optional extension layers can integrate cleanly through public contracts.

**Related documents:**
- [CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md](./CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md) — broader runtime and extension enablement context
- [PLUGIN_RUNTIME_CORE_ENABLEMENT.md](./PLUGIN_RUNTIME_CORE_ENABLEMENT.md) — detailed plugin runtime requirements (manifest parsing, scope enforcement, API versioning, hook registry, migration runner, testability, failure behavior)

**Tech Stack:** Python, FastAPI, Pydantic, existing `config_loader`, `GraphService`, REST router, MCP tool registration, pytest.

---

## Current completed slices

1. capability manifest and discovery
2. runtime mode metadata and introspection
3. tenant context metadata
4. tenant-aware config layering with non-sensitive config-context introspection

These slices establish the public runtime and configuration seams needed before identity-aware hosted integration.

---

## Implementation priorities

### Priority 1: Request actor and scope foundation

**Why this comes first**
External or embedding deployment layers will need to pass actor identity, workspace context, and graph context into the core, while the standalone core must remain usable without any built-in account system. Establishing this generic foundation enables all subsequent authorization and attribution work.

**Target outcome**
- the core can expose a generic request actor context
- the core can expose generic workspace and graph scope context
- all outputs are safe for public introspection and contain no service-specific or private logic
- standalone defaults remain no-op and permissive

**Scope**
- introduce a lightweight request actor abstraction driven by environment-safe or request-safe inputs
- introduce a workspace/graph scope abstraction that can later be populated by a SaaS gateway or service layer
- expose a small public introspection surface through config/service/REST/MCP
- add targeted tests for default behavior and override behavior

**Likely files**
- `backend/` helper module for actor/scope context
- `backend/config_loader.py` or adjacent runtime/context module
- `backend/service/service.py`
- `backend/service/rest_api.py`
- `backend/service/mcp_tools.py`
- `backend/agents/mcp_loader.py`
- targeted pytest files in `backend/tests/`, `backend/service/tests/`, and `backend/agents/tests/`

**Design constraints**
- do not add a built-in user directory
- do not make authentication mandatory for standalone mode
- do not expose secrets, tokens, or private account metadata
- keep the model generic enough for personal workspaces, team workspaces, and graph-scoped access
- generic names and descriptions only; no distribution-specific identifiers in public defaults

---

### Priority 2: Authorization hook seam

**Why this follows actor and scope**
Once actor and scope are explicit, the next step is to define where an external or embedding layer can narrow graph access without baking proprietary policy into the core.

**Target outcome**
- the core can call a generic authorization hook for graph access decisions
- default behavior remains permissive in standalone mode
- external deployment layers can replace or augment the default behavior

**Suggested first slice**
- define a small authorization interface or evaluation seam
- thread it into read and mutation paths where graph access will later matter
- keep enforcement minimal until the seam is proven through tests and docs

---

### Priority 3: Actor attribution for writes and events

**Why this matters**
Embedding systems and extended deployment layers need audit-friendly mutation attribution. The public core should prepare for this generically without requiring a full audit subsystem.

**Target outcome**
- write operations can carry actor metadata in a generic form
- event payloads or mutation metadata can include actor and scope information where safe
- later audit logging can attach without rewriting core mutation paths

**Suggested first slice**
- add actor attribution fields to mutation context
- expose them to event publication or mutation metadata surfaces where already available
- add regression tests around no-op standalone defaults

---

### Priority 4: Operability hooks

**Why this matters**
Reliable health signals, restore boundaries, and machine-readable diagnostics matter across all deployment types.

**Target outcome**
- clearer readiness versus liveness behavior
- better startup diagnostics for config and extension loading
- improved export/import friendliness for support and restore workflows
- a future-friendly path from file-based persistence to shared storage for hosted SaaS

**Suggested first slice**
- improve structured startup logging around config/runtime/capability/context loading
- add generic integrity diagnostics where cheap to expose
- identify the storage abstraction seams needed before a shared RBAC-aware storage backend is introduced

---

## Recommended execution order

1. implement request actor and scope foundation
2. implement authorization hook seam
3. implement actor attribution for writes and events
4. improve operability hooks

---

## Public boundary

This implementation plan covers only generic core infrastructure. It does not include:
- distribution strategy or packaging policy
- business-specific review or approval policy
- revenue or business-system integration
- implementation details of any external system that embeds or extends the core

Those concerns belong outside the public core repository. The slices here create the seams; external layers may build on those seams, but their logic must not flow back into the public core.

---

## Success criteria

This plan is succeeding when:
- optional capabilities can be discovered through public, generic contracts
- runtime mode is explicit instead of implicit
- standalone deployments still work with no built-in user directory or mandatory auth requirement
- external deployment layers can pass actor, workspace, and graph context through stable contracts
- future authorization logic can attach without forking core
- mutation flows can later emit audit-friendly metadata without invasive rewrites
- extension integrations can attach through stable APIs rather than patches
- public artifacts remain free of private roadmap or product-specific detail
- the plugin runtime requirements in [PLUGIN_RUNTIME_CORE_ENABLEMENT.md](./PLUGIN_RUNTIME_CORE_ENABLEMENT.md) can be implemented incrementally on top of the foundation built here
