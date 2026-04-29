# Core Enablement Plan for Hosted and SaaS Integration

## Purpose

This document defines the **public core-side enablement plan** for running CommunityOverview as:
- a standalone open source application
- a hosted service operated by a service team
- a base platform that can later accept optional SaaS-only extensions without forking the core

This document deliberately focuses on **general technical enablement**. It does **not** define private premium features, commercial packaging, or hosted-service-specific business logic.

---

## Core goals

The core should evolve so that it can:

1. run cleanly in standalone mode with minimal required infrastructure
2. support tenant-aware hosted operation through explicit configuration and lifecycle seams
3. expose stable extension points for optional service-layer or plugin-based capabilities
4. keep the open source core useful even when no hosted or premium layer is present

## Clarification: what tenant-aware hosted operation means here

For the target SaaS model, tenant-aware operation should not imply one dedicated application stack per graph.

The default target architecture is:
- one shared application/service environment per deployment tier or region
- multiple graphs or customer workspaces served by the same runtime instances
- graph selection, search scope, rendering scope, and mutation scope controlled by application identity and authorization
- isolation enforced through configuration, graph scoping, authorization, and data-layer controls

This means the core should prepare for:
- shared-hosting deployments where users gain access to one or more graphs through an application-managed user directory and authorization model
- future storage backends that can support record-level or row-based access constraints for shared graph data
- a separation between tenant context metadata and actual graph access decisions

---

## Design principles

### 1. Standalone-first
The default application must remain runnable without private control planes, proprietary dependencies, or SaaS-only services.

### 2. Explicit extension points
If hosted or future SaaS capabilities need to integrate with the core, they should do so through documented hooks, interfaces, events, APIs, or configuration contracts.

### 3. No private roadmap leakage
The core may expose generic enablement for hosted operation and plugins, but it must not contain private feature planning or premium-only product assumptions.

### 4. Safe defaults
New extension mechanisms must default to disabled or no-op behavior unless explicitly configured.

### 5. Replaceable integrations
Hosted or future SaaS layers should be attachable without making the core permanently dependent on a single control plane or vendor-specific service.

---

## What the core should enable

### A. Deployment profile flexibility
The core should support multiple runtime modes through configuration rather than forks.

Target outcomes:
- a clean standalone mode
- a hosted-service mode with additional operational configuration
- the ability to enable optional extensions per deployment profile

Likely core changes:
- clearer separation between runtime config, tenant config, and environment config
- explicit startup configuration for enabled capabilities
- environment-safe defaults when optional services are absent

### B. Capability registration model
The core should be able to register optional capabilities without hard-coding them into the main execution path.

Target outcomes:
- optional feature registration at startup
- capability discovery for the UI and API layer
- safe behavior when a capability is unavailable

Likely core changes:
- a capability registry or feature manifest model
- standardized capability metadata
- public API for checking capability availability

### C. Extension and plugin seams
The core should expose a minimal, documented way to attach additional behavior.

Target outcomes:
- extensions can add behavior without patching core internals
- future SaaS-only features can remain external to the core repository
- core behavior remains testable with extensions disabled

Likely core changes:
- lifecycle hooks during startup and request handling
- extension registration points for backend services
- optional frontend capability exposure through presentation or config payloads
- documented contracts for extension loading and failure handling

### D. Tenant-aware configuration boundaries
The core should be able to operate with per-tenant settings in hosted scenarios without assuming a specific SaaS architecture.

Target outcomes:
- clear separation between shared application settings and tenant-specific settings
- safer loading of tenant-specific graph, schema, prompt, and auth-related configuration
- easier automation of tenant provisioning outside the core
- support for graph- or workspace-scoped configuration inside a shared hosted service, not only one-config-per-deployment assumptions

Likely core changes:
- stronger config layering rules
- explicit tenant context propagation where needed
- validation for tenant-scoped configuration inputs
- boundaries between tenant metadata, graph selection, and authorization policy

### E. Authentication and authorization seams
The core should make it easier to integrate stronger hosted access models later.

Target outcomes:
- pluggable identity context for requests
- clean boundaries between application roles and infrastructure access
- ability to layer in stronger operator or tenant-admin models later
- graph-scoped authorization decisions so shared service instances can restrict which graphs, nodes, and edges a user can access

Likely core changes:
- request identity abstraction
- clearer role and permission evaluation seams
- audit-friendly handling of actor identity in write operations
- future-friendly interfaces between user directory, RBAC/policy checks, and graph access scope

### F. Operational hooks for backup, restore, and data lifecycle
The core should provide generic mechanisms that make hosted backup and restore reliable.

Target outcomes:
- predictable data export and import boundaries
- well-defined graph/config persistence surfaces
- supportable restore verification flows
- a clear migration path away from file-based graph persistence when shared SaaS storage becomes necessary

Likely core changes:
- clearer data ownership boundaries between graph data, config, and generated state
- documented import/export contracts
- stable health and integrity checks around persisted state
- storage abstractions that can later support shared persistence and record-level authorization constraints without rewriting the whole application

### G. Observability and operational introspection
The core should be easier to operate in a hosted environment without changing its product shape.

Target outcomes:
- health checks that reflect real readiness
- structured logs suitable for centralized collection
- metrics or instrumentation points for key operational flows
- easier debugging of config, graph loading, and extension activation

Likely core changes:
- better structured logging around startup and critical mutations
- explicit readiness versus liveness signals
- instrumentation hooks for extension-managed behavior

### H. API and event surfaces for service-layer integration
The core should expose generic mechanisms that a hosted service layer can build on.

Target outcomes:
- stable APIs for tenant setup, status inspection, and lifecycle automation where appropriate
- event hooks for important graph or config mutations
- minimal coupling between the core runtime and external service orchestration

Likely core changes:
- clearer admin-safe API boundaries
- event publication points for important lifecycle changes
- documented contracts for external automation to interact with the core

---

## Recommended public workstreams

### Workstream 1: Runtime modes and configuration layering
Focus:
- standalone versus hosted-compatible runtime behavior
- separation of shared, environment, and tenant-specific config

### Workstream 2: Capability registry and extension contracts
Focus:
- generic capability registration
- extension discovery and safe defaults
- frontend and backend capability exposure

### Workstream 3: Tenant-aware seams
Focus:
- tenant context propagation
- tenant-safe config validation
- tenant-specific data loading boundaries

### Workstream 4: Auth, identity, and audit seams
Focus:
- request identity abstraction
- actor attribution for writes
- future-friendly authorization integration points

### Workstream 5: Operability hooks
Focus:
- health/readiness improvements
- logging and metrics hooks
- data lifecycle and restore-friendly interfaces

---

## Explicitly not part of this public plan

This document does not define:
- private premium features
- commercial packaging or edition strategy
- hosted-service business workflows outside generic technical contracts
- internal service-team operating procedures
- proprietary control-plane implementation details

Those belong outside the public core repository.

---

## Suggested implementation sequence

1. document runtime modes and configuration boundaries
2. introduce a capability registry with no-op defaults
3. define extension loading contracts and failure behavior
4. add tenant-aware configuration seams where core behavior currently assumes a single deployment context
5. improve identity, audit, and mutation attribution surfaces
6. harden observability, health, and persistence boundaries for hosted operation

---

## Success criteria

The core enablement work is succeeding when:
- the application still runs cleanly as a standalone open source deployment
- hosted operation can be layered on without forking the core
- optional capabilities can be enabled or disabled through stable contracts
- future SaaS-only features can integrate through documented seams instead of invasive patches
- public documentation stays generic and technically grounded
