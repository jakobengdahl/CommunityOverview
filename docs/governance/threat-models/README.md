# Threat models

A pull request that creates or moves a trust boundary, or adds a new path across
an existing boundary, requires a threat model. The PR names the affected model
and updates it in the same PR. Reviewers use it to trace inputs across boundaries,
challenge the threats and controls against the code, and assess residual risk as
part of the change. The [scanner threat model](../../../.oss-scanner/threat_model.md)
is the baseline for the scanner; these models cover individual product surfaces.

## Template

Use the following sections for each model:

1. **Assets** — what needs protection and why.
2. **Actors** — legitimate users, components and potential adversaries.
3. **Trust boundaries** — where authority or trust changes, including data flows.
4. **Entry points** — interfaces and inputs crossing those boundaries.
5. **Threats (STRIDE-style)** — spoofing, tampering, repudiation, information
   disclosure, denial of service and elevation of privilege, as applicable.
6. **Mitigations with code references** — existing controls, with file and
   function or section references; distinguish planned controls from current ones.
7. **Residual risk** — threats not fully controlled, assumptions and limitations.

## Models

- [MCP server](mcp-server.md)
- [Session backup and restore](session-backup-restore.md)
- [User functions and triggers](user-functions-triggers.md)
