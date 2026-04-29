# Core Enablement Implementation Plan

> **For Hermes:** Use Claude Code or another isolated coding agent to implement small slices from this plan, then perform independent review before merge.

**Goal:** Define the first concrete implementation slices that make CommunityOverview more ready for hosted operation and future SaaS-only extensions without leaking private roadmap details into the public core.

**Architecture:** The core should expose generic, opt-in seams rather than hard-coded hosted logic. The first slices should improve capability discovery and runtime-mode introspection so optional service layers can integrate cleanly through public contracts.

**Tech Stack:** Python, FastAPI, Pydantic, existing `config_loader`, `GraphService`, REST router, MCP tool registration, pytest.

---

## Implementation priorities

### Priority 1: Capability manifest and discovery

**Why this comes first**
The core needs a generic way to describe optional capabilities without forcing them into the default execution path. This becomes the foundation for future hosted-only or SaaS-only integrations.

**Target outcome**
- the schema config can declare optional capabilities in a generic format
- the backend exposes the resulting capability manifest through stable APIs
- the frontend or external service layers can discover capability availability without relying on private assumptions

**Scope**
- add a generic capability model to configuration
- expose a capability manifest through `config_loader`
- expose it through `GraphService`
- add a REST endpoint and MCP tool for capability discovery
- add tests for default behavior, custom config behavior, and client access

**Likely files**
- `backend/config_loader.py`
- `backend/service/service.py`
- `backend/service/rest_api.py`
- `backend/service/mcp_tools.py`
- `backend/tests/test_config_loader.py`
- `backend/service/tests/test_clients.py`
- `config/test/schema_config.json`

**Design constraints**
- optional by default
- safe no-op defaults when no capabilities are configured
- generic names and descriptions only
- no premium-specific identifiers required in public defaults

---

### Priority 2: Runtime mode metadata and introspection

**Why this comes second**
Hosted and standalone deployments need a clean, public way to report how the application is running. This should be explicit and machine-readable instead of inferred from ad hoc environment assumptions.

**Target outcome**
- the core can report a generic runtime mode such as `standalone` or `hosted`
- external automation can inspect the active runtime mode and enabled extension identifiers
- hosted integrations can use this contract without the core depending on a proprietary control plane

**Scope**
- add runtime-mode metadata with environment-safe defaults
- expose runtime information through `config_loader` or a dedicated runtime helper
- expose it through `GraphService`
- add a REST endpoint and MCP tool for runtime introspection
- add tests for default behavior and environment override behavior

**Likely files**
- `backend/config_loader.py` or a new runtime helper module under `backend/`
- `backend/service/service.py`
- `backend/service/rest_api.py`
- `backend/service/mcp_tools.py`
- `backend/tests/test_config_loader.py`
- `backend/service/tests/test_clients.py`

**Design constraints**
- default to `standalone`
- avoid vendor-specific runtime semantics
- keep the output descriptive but small
- make extension reporting optional and generic

---

### Priority 3: Tenant-aware configuration seams

**Why this matters**
Hosted operation will need cleaner boundaries between shared app config and tenant-specific configuration, but this should be introduced incrementally.

**Target outcome**
- clearer rules for tenant-scoped versus shared config
- fewer implicit single-tenant assumptions
- easier external provisioning and validation

**Suggested first slice**
- document config layering rules
- identify which existing config fields are tenant-specific
- add validation seams instead of full tenant orchestration

**Potential files**
- `backend/config_loader.py`
- `docs/PROFILES.md`
- selected tests around config loading

---

### Priority 4: Identity and audit seams

**Why this matters**
Hosted service layers will eventually need stronger actor attribution and access boundaries. The public core should prepare for this generically.

**Target outcome**
- request identity can be carried through write operations
- mutation responses or event payloads can attribute actor identity in a generic form
- the core stays auth-provider-agnostic

**Suggested first slice**
- introduce a lightweight request actor abstraction
- thread actor metadata into mutation paths where it can later feed audit logs or webhooks

---

### Priority 5: Operability hooks

**Why this matters**
Hosted operation depends on reliable health, restore boundaries, and machine-readable diagnostics.

**Target outcome**
- clearer readiness versus liveness behavior
- better startup diagnostics for config and extension loading
- improved export/import friendliness for support and restore workflows

**Suggested first slice**
- improve structured startup logging around config/runtime/capability loading
- add generic integrity diagnostics where cheap to expose

---

## Recommended execution order

1. implement capability manifest and discovery
2. implement runtime mode metadata and introspection
3. document and harden tenant-aware config seams
4. introduce identity and audit seams
5. improve operability hooks

---

## Success criteria

This plan is succeeding when:
- optional capabilities can be discovered through public, generic contracts
- runtime mode is explicit instead of implicit
- hosted integrations can attach through stable APIs rather than patches
- the application still behaves cleanly with no extra config in standalone mode
- public artifacts remain free of private SaaS roadmap detail
