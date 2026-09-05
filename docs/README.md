# Documentation Index

This folder holds the project's long-form documentation. Each document is tagged
with one status so a reader (human or agent session) knows how to trust it:

- **Current** — describes the system as it is today, or an active process/guide.
  Safe to rely on when working in the code.
- **Design (target state)** — a plan or enablement proposal for work that is not
  yet fully implemented. Describes *desired* behaviour, not present behaviour.
- **Historical** — a record of an earlier design or a past change. Kept for
  context; the system has since moved on. Do not treat as current-state.

For the working structural backlog (desired changes to the repo itself, not a
description of the system) see [`../STRUCTURE_REVIEW.md`](../STRUCTURE_REVIEW.md)
and [`../SMALL_FIXES.md`](../SMALL_FIXES.md).

---

## Current

| Document | Covers |
|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | End-user walkthrough of every user-facing feature |
| [PROFILES.md](PROFILES.md) | Configuration profiles: node types, presentation, env |
| [ICONS.md](ICONS.md) | Icon-name reference for node-type definitions |
| [DATA_MANAGEMENT.md](DATA_MANAGEMENT.md) | How graph data files are stored and loaded |
| [PERSISTENCE_BACKENDS.md](PERSISTENCE_BACKENDS.md) | The storage seam: what a persistence backend implements, and how mutations reach it |
| [EVENT_SUBSCRIPTIONS.md](EVENT_SUBSCRIPTIONS.md) | Webhook / graph-mutation event system |
| [AGENT_SCHEDULING.md](AGENT_SCHEDULING.md) | Time-based agent schedule triggers |
| [FEDERATED_GRAPH_DESIGN.md](FEDERATED_GRAPH_DESIGN.md) | Federation design and its implementation status |
| [MULTI_USER_SESSIONS_DESIGN.md](MULTI_USER_SESSIONS_DESIGN.md) | Shared-session design and implementation record (implemented) |
| [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Provider-agnostic guide for deploying the app |
| [DEPLOYMENT_CONTRACT.md](DEPLOYMENT_CONTRACT.md) | App-repo ↔ infra-repo artifact contract |
| [DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md](DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md) | GraphStorage thread/process-safety design |
| [mcp-oauth-gateway.md](mcp-oauth-gateway.md) | The OAuth 2.1 gateway service that fronts the MCP endpoint |
| [SSPCloud-setup.md](SSPCloud-setup.md) | Getting started on SSPCloud with the stat-metadata profile |
| [MANUAL_TESTING.md](MANUAL_TESTING.md) | Manual verification checklist before `dev` → `preview` |
| [TEST_PLAN.md](TEST_PLAN.md) | Manual test plan for features on `dev` not yet in `preview` |

## Design (target state)

| Document | Covers |
|---|---|
| [CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md](CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md) | Public-core enablement plan for standalone/embedded/extensible runtime modes |
| [CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md](CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md) | Concrete implementation slices for the runtime/extension enablement above |
| [PLUGIN_RUNTIME_CORE_ENABLEMENT.md](PLUGIN_RUNTIME_CORE_ENABLEMENT.md) | Generic core work to load, discover, and manage plugins at runtime |

## Historical

| Document | Covers |
|---|---|
| [expert-agents-implementation-plan.md](expert-agents-implementation-plan.md) | Earlier expert-agent design; only partly built (see its header note) |
| [MIGRATION_NOTES.md](MIGRATION_NOTES.md) | Record of the deployment-model refactor that moved orchestration to the infra repo |
| [sprint_documentation/](sprint_documentation/) | Stockholm-Sprint metadata-prototype user stories and plans |
