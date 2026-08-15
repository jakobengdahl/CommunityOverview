# ADR 0002 — Environment-backed default secret provider

- **Status:** Accepted
- **Date:** 2026-08-12
- **Scope:** Open-source core only
- **Related:** [`SECRET_PROVIDER_CONTRACT.md`](../SECRET_PROVIDER_CONTRACT.md)
  (the seam this decision picks a default adapter for)

## Context

Agent and tool code in the core needs secrets at runtime (LLM API keys, a search
token, env vars for stdio MCP tool subprocesses). Those values were read straight
from `os.environ` at the point of need and, in at least one case, **inlined into
a configuration object** — the built-in Brave `SEARCH` integration stored the raw
`BRAVE_API_KEY` in its `env` dict, which any serialisation of that config
(`to_dict`, API responses, logs) would expose.

The [secret-provider contract](../SECRET_PROVIDER_CONTRACT.md) defines a
replaceable seam, `SecretProvider`, and a `secret://<name>` reference format so
config can hold references instead of values. This ADR fixes the **default
adapter** the core ships and the **resolution posture**, so later slices (hosted
managed-secret backends, additional secret-consuming tools) do not re-litigate
them.

## Decision

1. **Default adapter: `EnvSecretProvider`.** The core's default resolves secret
   names from the process environment (a mapping defaulting to `os.environ`, with
   an optional namespacing `prefix`). This keeps standalone/file-only mode
   working with no external dependency and matches how the project already
   supplies configuration (`.env` via `python-dotenv`).

2. **References, not values, in config.** Configuration and tool wiring hold
   `secret://<name>` references. Values are resolved once, at the point of use,
   through a provider — never inlined. The built-in `SEARCH` integration is
   migrated to a reference as the reference implementation of the pattern.

3. **Required-by-default resolution.** An unresolved reference is an error
   (`SecretNotFoundError`) unless the caller explicitly marks it optional. This
   fails loud on misconfiguration rather than silently launching a tool without
   its credential.

4. **The core binds to no managed backend.** Hosted/SaaS layers implement the
   same `get_secret` contract against a managed secret store and inject it in
   place of the default. The core only ever calls `get_secret`, so that
   substitution needs no core change, and tenant policy/rotation/audit stay out
   of the open core.

## Consequences

- No secret value is inlined into serialisable config, closing the latent
  exposure through the `SEARCH` integration's `env`.
- Standalone runs need only the relevant environment variables set — behaviour
  unchanged from before the seam, now routed through one resolution path.
- A managed-secret hosted deployment is a drop-in provider swap, not a core fork.
- Consumers must resolve references before use; a raw `secret://…` string handed
  to a subprocess would be a bug, not a working credential. `resolved_env` is the
  supported path for integration env.
