# Core Enablement Implementation Plan

> **For Hermes:** Use Claude Code or another isolated coding agent to implement small slices from this plan, then perform independent review before merge.

**Goal:** Define the next concrete implementation slices that make CommunityOverview more ready for hosted operation and future SaaS-only extensions without leaking private roadmap details into the public core.

**Architecture:** The core should expose generic, opt-in seams rather than hard-coded hosted logic. The current priority is to make identity, workspace scope, graph scope, and audit attribution pluggable without forcing a built-in SaaS account system into standalone deployments.

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

**Why this comes next**
The hosted SaaS layer will need to pass actor identity, workspace context, and graph context into the core while the standalone core must stay usable without any built-in hosted account system.

**Target outcome**
- the core can expose a generic request actor context
- the core can expose generic workspace and graph scope context
- all outputs are safe for public introspection and contain no billing, account, or private SaaS logic
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
- keep the model generic enough for personal workspaces, team workspaces, and graph-scoped access later

---

### Priority 2: Authorization hook seam

**Why this follows actor and scope**
Once actor and scope are explicit, the next step is to define where an external service layer can narrow graph access without baking SaaS policy into the core.

**Target outcome**
- the core can call a generic authorization hook for graph access decisions
- default behavior remains permissive in standalone mode
- hosted deployments can later replace or augment the default behavior

**Suggested first slice**
- define a small authorization interface or evaluation seam
- thread it into read and mutation paths where graph access will later matter
- keep enforcement minimal until the seam is proven through tests and docs

---

### Priority 3: Actor attribution for writes and events

**Why this matters**
Hosted service layers will need audit-friendly mutation attribution even before a full audit product exists.

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

**Why this remains important**
Hosted operation depends on reliable health, restore boundaries, and machine-readable diagnostics.

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

## Success criteria

This plan is succeeding when:
- standalone deployments still work with no user directory or built-in auth requirement
- hosted integrations can pass actor, workspace, and graph context through stable contracts
- future authorization logic can attach without forking core
- mutation flows can later emit audit-friendly metadata without invasive rewrites
- public artifacts remain free of private SaaS roadmap detail
