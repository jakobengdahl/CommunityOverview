# Architecture Decision Records

This directory holds Architecture Decision Records (ADRs) for the open core:
short, immutable notes that capture a significant technical decision, the context
that forced it, and its consequences.

- One decision per file, numbered `NNNN-kebab-case-title.md`.
- An ADR is a point-in-time record. When a later decision changes course, add a
  new ADR that supersedes the old one rather than rewriting history; mark the old
  one `Superseded by NNNN`.
- ADRs record *decisions*. Normative, evolving contracts live as their own
  documents (e.g. [`../DURABLE_EXECUTION_CONTRACT.md`](../DURABLE_EXECUTION_CONTRACT.md))
  and are linked from the ADR that adopted them.

Planning status (who does the work, when) is tracked outside this repository —
ADRs carry only the technical decision.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-local-durable-execution-store.md) | Default local durable execution store | Accepted |
| [0002](0002-webxr-immersive-graph-client-spike.md) | WebXR immersive graph client (Quest) — spike | Proposed |
