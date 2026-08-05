# MCP Visualization Session Lifecycle & Canonical Deep-Link Contract

**Contract version:** v1
**Status:** Accepted (design) — implementation pending. This document defines the
public contract that the MCP session-CRUD tools and the canonical deep-link
generation implement. It is the source of truth for *semantics*; the wire shapes
of individual MCP tools are documented at implementation time in
`backend/DEVELOPMENT.md` and the tool docstrings.
**Scope:** Open-source core only. Tenant ownership binding, session
time-to-live, and soft-delete retention are **SaaS-layer** concerns and are
explicitly out of scope here — see [§7 Public/private boundary](#7-publicprivate-boundary).

This contract complements [`MULTI_USER_SESSIONS_DESIGN.md`](MULTI_USER_SESSIONS_DESIGN.md),
which owns the realtime op protocol, storage and presence model. Where the two
overlap, the session-store data model in that document is authoritative; this
document adds the **lifecycle, ownership seam and canonical-URL** semantics that
an MCP-connected assistant relies on.

---

## 1. Motivation

An MCP-connected assistant can already inspect and lay out an *open* browser
visualization session (`connect_to_visualization_session`,
`get_visualization_session_state`, `get_visualization_layout`,
`apply_visualization_layout`, `clear_visualization`). It cannot yet **create** a
named session from scratch, **enumerate** sessions, **rename** or **delete** one,
or obtain a **direct link** it can hand to a user. Those gaps force the human to
manually open the app and read a session ID out of the header before the
assistant can do anything.

This contract closes the loop: an assistant creates a named session, populates
and arranges it, and returns a canonical URL that opens that exact session
directly — end to end, without the user reading IDs off a screen.

## 2. The session resource (MCP projection)

A session is a first-class server-side entity (see
`MULTI_USER_SESSIONS_DESIGN.md` §3.1). MCP tools return a **stable projection**
of it. Fields marked *(reserved)* are part of the contract shape but always carry
their default in the open core; the SaaS layer populates them.

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | Stable identity, `DDDD-DDDD-DDDD-DDDD` (legacy `DDDD-DDDD` still resolves). The id, never the name, is the identity. |
| `name` | string \| null | Free-text display name. **Non-unique** (§4). |
| `lifecycle_state` | enum | `active` in the open core. *(reserved)* values `expired`, `deleted` are defined for the SaaS layer; core never emits them. |
| `owner` | string \| null | *(reserved)* Optional actor/owner identifier. `null` in the anonymous open core (§7). |
| `workspace` | string \| null | *(reserved)* Optional workspace/tenant identifier. `null` in the open core. |
| `created_at` | iso8601 | |
| `updated_at` | iso8601 | Advances on every applied op and on rename. |
| `revision` | int | The session `seq` — monotonic op revision, for optimistic concurrency. |
| `node_count` | int | Number of graph nodes currently referenced by the session. |
| `capabilities` | string[] | What the caller may do on this session given the active authorization decision (e.g. `["read","rename","delete","layout"]`). In the open core all capabilities are granted; the SaaS layer narrows the list per the authorization decision (§7). |
| `session_url` | string \| null | Canonical direct link (§5). `null` only when no public base URL is configured. |

Callers **must** treat unknown fields as forward-compatible additions and must
not depend on field order.

## 3. Lifecycle operations (MCP tool surface)

The MCP tools are thin, authorization-checked projections over the existing REST
session endpoints (`POST/GET/PATCH/DELETE /sessions`, see
`backend/service/rest_api.py`). They add no new persistence — they reuse
`SessionManager`.

| Operation | Semantics |
|---|---|
| **create** | Create a new empty session. Optional `name`; when omitted the server assigns a default (§4). Returns the full resource projection incl. `session_url`. Subject to the existing `max_sessions` cap (503-equivalent error). |
| **list** | Return session projections the caller is authorized to see, newest-`updated_at` first. In the open core this is all sessions; the SaaS layer scopes it to the caller's workspace. |
| **get / inspect** | Return one session's projection by `session_id`. `404`-equivalent when absent. |
| **rename** | Set/clear the `name`. Get-or-create semantics (as REST `PATCH`): renaming an id that only exists client-side materialises it rather than failing, so the name is not lost when the session later saves (see `MULTI_USER_SESSIONS_DESIGN.md` R7/R8). Last-write-wins (§4). |
| **delete** | **Confirmed hard delete** (§6). Requires explicit confirmation; enforces authorization; emits an audit event; broadcasts `session_deleted` to connected clients. No recovery in the open core. |

Every operation runs through the authorization seam (§7) and, for mutations,
emits an attributed mutation/audit event using the existing request-actor and
audit seams (M1 core enablement).

## 4. Naming

- **Names are non-unique.** The `session_id` is the identity; two sessions may
  share a display name. No uniqueness index is maintained in the open core.
- **Default name.** When `create` is called without a `name`, the server fills a
  default (e.g. `"Untitled session"` or a timestamped default). The value is a
  server responsibility so assistants need not invent one.
- **Rename is last-write-wins.** Rename is routed through the op-sequence path
  (`session_renamed` state op), giving it a `seq` and a ring-buffer entry so a
  reconnecting client observes it via catch-up. Concurrent renames resolve by
  server arrival order — the last applied op wins. No rename-conflict error is
  raised.
- Per-workspace name uniqueness, if ever required, is a **SaaS option** layered
  on top; it is not part of the core contract.

## 5. Canonical deep-link (`session_url`)

The single most important rule: **the server generates the URL; the assistant
never constructs or guesses it.**

### 5.1 Shape

The canonical URL keeps the established query-parameter form the frontend
already reads and reflects (`App.jsx` `reflectSessionUrl` / `?session=` load
path):

```
<public-base-url>/?session=<session_id>
```

A path-based `/s/<id>` form was evaluated against every SaaS v1 work item and
rejected: URL consumers (the session context-menu "copy/share link" surface and
the deep-link opener) are shape-agnostic, and SaaS tenant routing is **host-based**
(`app.<tenant>.<domain>`, `preview.<tenant>.<domain>`), so a per-tenant link is
already fully expressed as `https://app.<tenant>.<domain>/?session=<id>`. A path
segment would add a frontend routing change for no functional or routing gain.

### 5.2 Where the base URL comes from

The public base URL is **server-side configuration**, read the same way as the
existing tenant context (`config_loader.get_tenant_context()`):

- A new env var (e.g. `COMMUNITYOVERVIEW_PUBLIC_BASE_URL`) supplies the
  externally reachable origin (scheme + host + optional base path).
- When unset (pure standalone/local use with no configured public origin),
  `session_url` is `null` and the tool result explains that a base URL is not
  configured, rather than emitting a guessed or `localhost` link.
- In hosted deployments the value is set per environment/tenant, so the URL is
  correct for prod vs preview and for the tenant's host without the assistant
  knowing any of that.

### 5.3 Opening a link

Opening `session_url` must:

1. Navigate directly to the intended session (load its content from the server).
2. Run through the deployment's normal authentication/authorization on the way in
   (IAP / OAuth in hosted environments; none in standalone core). Authorization
   for *viewing* is the deployment's existing gate — this contract does not add a
   new one for the open core.
3. Preserve safe return routing (no open-redirect: only same-origin/relative
   return targets are honoured).
4. Show a **clear not-found / expired state** when the session is absent
   (hard-deleted) or, in the SaaS layer, expired — never a blank canvas that
   looks like an empty-but-valid session.

## 6. Deletion, recovery and expiry

- **Confirmed delete.** The MCP delete tool requires an explicit confirmation
  signal (e.g. a `confirm=true` flag or an echo of the `session_id`); a bare
  delete call without it is refused with guidance. This prevents an assistant
  from deleting a session on a loose instruction.
- **Hard delete in the core.** Deletion removes the session; there is **no
  recovery** and **no soft-delete tombstone** in the open core. The existing
  `session_deleted` broadcast notifies connected clients.
- **Audit.** Deletion emits an attributed audit event (who/when/which session)
  via the core audit seam.
- **No expiry (TTL) in the core.** Sessions persist until explicitly deleted,
  bounded only by `max_sessions`. The `lifecycle_state` field reserves
  `expired` so the **SaaS layer** can add idle-TTL and soft-delete retention
  **without a core breaking change**; the deep-link's expired-state handling
  (§5.3.4) is already specified so that layer needs no contract change.

## 7. Public/private boundary

This is the governing split for the whole feature.

- **Open core (this repo):** exposes the *generic* session resource — including
  the *optional* `owner`/`workspace` fields and the `capabilities` list — and
  routes every session operation through the existing **authorization hook** and
  **request-actor** seams (M1 core enablement, already implemented). The default
  authorization decision is **permissive/anonymous**: `owner`/`workspace` stay
  `null`, all capabilities are granted, and behaviour is unchanged from today's
  id-guarded sessions.
- **SaaS layer (private repo):** binds real tenant/workspace **ownership** on
  create, **enforces** access in the authorization hook (scoping `list`,
  narrowing `capabilities`, rejecting cross-tenant `get`/`rename`/`delete`), and
  layers TTL/soft-delete retention. None of that enforcement logic lives in this
  repo.

The core therefore ships only the *seam*; the commercial enforcement is private.
This keeps premium multi-tenant behaviour out of the public codebase while
letting the SaaS layer reuse the exact same session resource and MCP tools.

## 8. Error model

Tool results are structured (never a bare exception). At minimum:

| Condition | Result |
|---|---|
| Invalid `session_id` format | `{"success": false, "error": "invalid session id format …"}` |
| Session not found | `{"success": false, "error": "session '<id>' not found …"}` |
| Delete without confirmation | `{"success": false, "error": "deletion requires explicit confirmation …"}` |
| Not authorized (SaaS) | `{"success": false, "error": "not authorized for this session"}` |
| Session cap reached | `{"success": false, "error": "too many sessions"}` |
| Rate limited | `{"success": false, "error": "rate limit exceeded"}` |
| No public base URL configured | success, with `session_url: null` and an explanatory `message` |

Results mirror the REST endpoints' existing status semantics (400/404/429/503)
so the two surfaces stay consistent.

## 9. Versioning & change policy

- This is contract **v1**. Additive fields (new *(reserved)* metadata, new
  capabilities) are non-breaking and do not bump the version.
- Changing an existing field's meaning, the URL shape, or the delete/confirm
  semantics is a **breaking change** and requires a new contract version plus a
  migration note.
- The requirement node `req-mcp-session-management-contract` and decision
  `dec-session-resource-deeplink-contract` in the Corp planning graph govern this
  document; status and evidence are tracked there, not here.

## 10. Realizing tasks

| Task (Corp graph) | Realizes |
|---|---|
| `task-implement-mcp-session-crud` | §2–§4, §6, §8 — the create/list/get/rename/delete MCP tools |
| `task-implement-session-deeplinks` | §5 — canonical `session_url` generation and the open/not-found/expired path |
| `task-document-agent-visualization-tools` | Tool docstrings + `backend/DEVELOPMENT.md` wire shapes |
