# Secret Provider Contract

**Contract version:** v1
**Status:** Accepted (design) with a reference implementation. This document is
the source of truth for how agent and tool code obtains secrets in the open
core. The seam lives in `backend/agents/secrets/`; its reference adapter is
`EnvSecretProvider`.
**Scope:** Open-source core only. Tenant policy, access control, audit, rotation
and the choice of a managed secret backend are **out of scope** here — see
[§5 Public/private boundary](#5-publicprivate-boundary).

---

## 1. Motivation

Agent and tool code needs credentials at runtime: LLM API keys, a search API
token, environment variables handed to a stdio MCP tool subprocess. Historically
those values were read directly from `os.environ` at the point of need and, worse,
**inlined into configuration objects** (e.g. an MCP integration's `env` dict held
the raw Brave key). Inlined secrets leak through any surface that serialises
config — `to_dict`, API responses, logs — and tie the core to a single source
(the process environment).

The core needs a **replaceable secret-resolution seam** so that:

- configuration and tool wiring hold opaque **references**, never secret values;
- resolution happens once, at the point of use, through a provider;
- standalone/file-only mode keeps working with zero external dependencies; and
- a hosted layer can swap in a managed secret backend without touching core code.

## 2. The seam

```python
@runtime_checkable
class SecretProvider(Protocol):
    def get_secret(self, name: str) -> Optional[str]: ...
```

A provider maps a **name** to a secret value, or `None` when the name is not
configured. It is read-only and side-effect free, and MUST NOT log or echo
resolved values.

The reference adapter, `EnvSecretProvider`, resolves names from a mapping that
defaults to `os.environ`, with an optional `prefix` for namespacing. It treats an
empty string as "not configured".

## 3. Secret references

Configuration holds **secret references**, not values. A reference is a string:

```
secret://<name>
```

Any other string is a literal and passes through resolution untouched, so a
config field may freely mix literals and references. Helpers in
`backend/agents/secrets/provider.py`:

| Helper | Purpose |
|---|---|
| `is_secret_ref(value)` | True iff `value` is a `secret://<name>` string |
| `parse_secret_ref(value)` | `SecretRef` or `None` |
| `resolve_secret(value, provider, *, required=True)` | resolve one value |
| `resolve_secret_mapping(mapping, provider, *, required=True)` | resolve a dict of values |

## 4. Resolution semantics

- A **literal** value (including `None`) is returned unchanged.
- A **reference** is looked up through the provider by its name.
- A reference that resolves to nothing raises `SecretNotFoundError` when
  `required=True` (the default), or resolves to `None` when `required=False` —
  the mechanism for genuinely optional secrets in standalone mode.
- `MCPIntegration.resolved_env(provider, *, required=True)` is the concrete
  consumption path: it resolves the integration's subprocess `env` just before
  the environment is handed to a stdio tool, dropping optional keys that are
  absent rather than passing `None` into the environment.

The built-in `SEARCH` (Brave) integration demonstrates the pattern: its `env`
holds `secret://BRAVE_API_KEY`, resolved through the default provider only when
the subprocess is launched. No secret value is ever inlined into config.

## 5. Public/private boundary

The open core defines the seam, the reference format, the resolution helpers and
the environment-backed default. It does **not** define, and must not grow:

- multi-tenant secret isolation or per-tenant access policy;
- a binding to any specific managed backend (cloud Secret Manager, Vault, …);
- rotation, leasing, auditing or a process/approval engine around secrets.

Those live in the SaaS/infra layer, which implements `SecretProvider` against a
managed store and injects it in place of `EnvSecretProvider`. Because the core
only ever calls `get_secret`, that substitution requires no core change.

See [`adr/0002-env-backed-secret-provider.md`](adr/0002-env-backed-secret-provider.md)
for the decision that fixed the environment-backed default.
