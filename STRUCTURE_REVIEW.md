# Project Structure Review — 2026-07-11

A full-structure review of this repository covering file/module placement,
technology choices, CI and tooling, security posture, and how well the codebase
supports continued AI-agent-driven development.

**How to use this document:** each action item below is written to be executable
as a standalone Claude session. Pick the highest-priority open item, start a
session with the *Session brief* text, and follow the Standard Development
Workflow in `CLAUDE.md` (branch → PR → review loop → merge to `dev`). When an
item is completed, move it to the *Completed* section at the bottom with the PR
number. Items are intentionally sliced so no single session needs more context
than its own brief.

This is a working backlog document (like `SMALL_FIXES.md`), not current-state
documentation — items describe *desired* changes, not the present system.

---

## Overall assessment

The fundamentals are healthy. The layered backend architecture
(`core → service → {rest_api, mcp_tools, ui} → api_host`) is sound and
documented; REST and MCP share one service layer with integration tests
asserting parity; the event/webhook system has real SSRF protection; auth
comparison uses `secrets.compare_digest`; tests exist for every backend module
and all three frontend workspaces; `SMALL_FIXES.md` plus the CLAUDE.md workflow
is an unusually good fit for agent-driven maintenance.

The problems are concentrated in four areas:

1. **The safety net has holes.** CI runs only `backend/core/tests/` — roughly
   80% of the backend test files and 100% of the frontend tests never run in
   CI, yet the merge workflow treats "CI green" as the gate.
2. **A few god files carry most of the change traffic.** `App.jsx` (1 865
   lines, 106 hook call sites), `server.py` (1 180 lines), `service.py`
   (1 575), `storage.py` (1 370), `GraphCanvas.jsx` (1 463),
   `chat_logic.py` (1 076). Nearly every entry in `SMALL_FIXES.md` points into
   one of these files. For agent sessions this is the single biggest cost:
   every small fix pays the full context price of the whole file.
3. **Repo hygiene drift.** Stale tracked data (`backend/graph.json`, 1.4 MB),
   compatibility shims at the root, ML dependencies in the base requirements
   contradicting the repo's own rules, docs that describe a structure that no
   longer matches the tree.
4. **Security posture is fine for pilots but has two known soft spots** that
   should be fixed before wider exposure: 8-digit session IDs acting as the
   sole credential on auth-bypassed endpoints, and a wildcard CORS default.

Technology choices (FastAPI + FastMCP, NetworkX + JSON persistence, React +
React Flow + Zustand, npm workspaces) are appropriate for the current scale and
need no replacement. The JSON-file storage is the known long-term scaling
limit, but the `storage_backends.py` abstraction is the right seam and the
right move now is to strengthen the contract around it, not to migrate.

---

## Priority 1 — restore the safety net

These come first because every other item relies on CI actually validating
changes.

### A1. Run the full test suite in CI

- **Problem:** `.github/workflows/ci.yml` runs only
  `pytest backend/core/tests/`. Tests under `backend/service/`,
  `backend/api_host/`, `backend/ui/`, `backend/agents/`,
  `backend/federation/`, `backend/core/events/`, `backend/tests/`, the three
  frontend vitest suites, and `services/mcp_oauth_gateway/test_oauth_flow.py`
  never run in CI. The definition-of-done in `CLAUDE.md` requires "CI is green"
  before merge, so today that gate certifies only a fraction of the system.
- **Proposed change:** extend the `test` job to run `pytest backend/ -q`
  (requires A2 first, or install with the ML mock path) and add a Node job
  that runs `npm ci && npm test` across the workspaces. Keep the jobs separate
  so failures are attributable. Update the *Test* section of `CLAUDE.md` to
  match ("CI runs the full suite"). Consider adding the gateway test as a third
  matrix entry.
- **Watch out for:** tests that pass locally but need network (embedding model
  download) — the mock-embedding fallback must be exercised in CI; Playwright
  e2e should stay out of the required path initially (flaky-risk) and can be a
  non-required job.
- **Definition of done:** a PR that breaks a `backend/service/` or frontend
  test goes red in CI; docs updated; `dev` branch protection marks the new
  checks required (see A4).
- **Effort:** M

> **Session brief:** In CommunityOverview, extend `.github/workflows/ci.yml` so
> CI runs the complete backend pytest suite and all frontend vitest workspaces
> (separate jobs), keeping Docker build behavior unchanged. Ensure the
> embedding-model mock path is used in CI (no network downloads). Update
> CLAUDE.md's Test section and backend/DEVELOPMENT.md accordingly. Verify by
> temporarily breaking one service test in a scratch commit and confirming CI
> fails, then removing it.

### A2. Move ML dependencies out of the base requirements

- **Problem:** `torch`, `sentence-transformers`, `scikit-learn` and the
  `--extra-index-url https://download.pytorch.org/whl/cpu` line sit in
  `backend/requirements.txt`, while `requirements-ml.txt` is marked
  "DEPRECATED". This directly contradicts the Dependency Changes rule in
  `CLAUDE.md`, bloats every install and Docker image, and the pytorch extra
  index makes `pip install -r requirements.txt` fail entirely on
  PyPI-only networks (already logged 2026-07-06 in `SMALL_FIXES.md`, effort M —
  this item supersedes that entry).
- **Proposed change:** move the three packages and the extra-index line to
  `requirements-ml.txt` (un-deprecate it); make `vector_store.py` /similarity
  degrade gracefully (lazy imports already exist — verify and test the
  without-ML path); decide explicitly whether the production Docker image
  installs the ML extras (probably yes, via a build arg) and document it.
- **Definition of done:** `pip install -r backend/requirements.txt` succeeds on
  a PyPI-only network; full test suite passes with and without the ML extras
  installed; the deprecated-file comment and CLAUDE.md rule agree with reality.
- **Effort:** M

> **Session brief:** In CommunityOverview, move torch, sentence-transformers,
> scikit-learn and the pytorch extra-index-url from backend/requirements.txt
> into backend/requirements-ml.txt (removing its DEPRECATED header). Verify the
> app and full test suite run without the ML stack (mock/fallback paths) and
> with it. Update the Dockerfile to install ML extras explicitly, and update
> LLM_PROVIDERS.md / DEVELOPMENT.md install instructions. Remove the superseded
> 2026-07-06 torch entry from SMALL_FIXES.md.

### A3. Harden session-ID-protected endpoints

- **Problem:** the auth middleware in `backend/api_host/server.py` bypasses
  authentication for everything under `/sessions/` and for
  `/api/sessions/{id}/stream`, protected only by the session ID. IDs are
  generated as `DDDD-DDDD` (`backend/core/session_store.py:350`) — 10^8
  combinations ≈ 26.6 bits of entropy. That is far below credential strength;
  an unauthenticated attacker can enumerate the space, and the default CORS
  policy is `*` (`backend/api_host/config.py:45`). This was a reasonable
  pilot-stage trade-off (documented in the design), but it is the weakest link
  once instances face the internet.
- **Proposed change (in order of preference):**
  1. Keep the human-friendly short ID for *display/join UX*, but derive the
     stream URL from a separate high-entropy token (`secrets.token_urlsafe`,
     ≥128 bits) stored on the session and returned only to authenticated
     creators/joiners.
  2. Add rate limiting / lockout on session-ID lookups (the ops endpoint
     already has a token bucket — extend the same mechanism to session
     resolution and the stream handshake).
  3. Change the CORS default from `*` to same-origin, keeping
     `CORS_ALLOWED_ORIGINS` for explicit opt-in.
- **Watch out for:** EventSource cannot send Authorization headers — that
  constraint is why the bypass exists; the token-in-URL approach preserves it.
  Backwards compatibility for existing stored sessions.
- **Definition of done:** brute-forcing an 8-digit ID no longer grants stream
  access on a default deployment; tests in `test_auth_middleware.py` /
  `test_session_api.py` cover the new token path; `MULTI_USER_SESSIONS_DESIGN.md`
  §3.9 updated.
- **Effort:** M–L (can be split: rate limiting + CORS default first as S, token
  scheme second)

> **Session brief:** In CommunityOverview, harden the shared-session endpoints
> that bypass Basic Auth. Step 1 (this session): add rate limiting to
> session-ID resolution and the SSE stream handshake, and change the CORS
> default in api_host/config.py from "*" to no cross-origin unless
> CORS_ALLOWED_ORIGINS is set. Step 2 (follow-up session): introduce a
> high-entropy per-session stream token so the 8-digit id is only a join code,
> per STRUCTURE_REVIEW.md item A3. Update MULTI_USER_SESSIONS_DESIGN.md and the
> auth-middleware tests.

**Update (2026-07-12) — step 2 (stream token) is blocked on a design decision;
reclassified as (owner action).** A session picking up row 10 traced the actual
session-creation flow and found the step-2 premise no longer matches the code:

- Step 2 as written assumes the token is *"returned only to authenticated
  creators/joiners"* — i.e. an authenticated creator mints the session (the §3.6
  *auto-create via `POST /api/sessions`* flow) and receives the token. But the
  browser **never calls `POST /api/sessions`**: `createServerSession` in
  `frontend/web/src/services/api.js` has zero callers. Sessions are created with
  a **client-generated id** (`api.generateVisualizationSessionId`) and
  materialised **lazily over the auth-bypassed stream** (`get_or_create` inside
  `stream_session`, `backend/service/rest_api.py`) or the first ops POST — the
  "lazy connect preserved for locally generated ids" behaviour in the design
  D-notes.
- Consequence: on the **creator** path there is no authenticated call before
  materialisation, so there is no authenticated channel to hand the creator its
  token — and delivering the token over the stream (the first channel the creator
  touches) would hand it to the very unauthenticated attacker the token is meant
  to stop (chicken-and-egg).
- The token's value is also **conditional on Basic Auth being active**: with auth
  off (a common pilot/dev posture) `GET /api/sessions/{id}` is itself unguarded
  and leaks the token, so the token adds nothing on those deployments.

Implementing step 2 is therefore a product/security decision, not a mechanical
change. The fork:
  - **(a) authenticated-`POST`-first creation** — the creator mints the session
    server-side (server-assigned id + token) before sharing; changes the
    client-generated-id + share-by-8-digit-code lifecycle (§3.6) and the
    share-URL-creates-session behaviour.
  - **(b) keep lazy materialisation, token-gates-joiners-only** — unauthenticated
    clients can still materialise/join empty sessions; the token only protects an
    already-materialised session's content, and only when auth is active. Does not
    fully meet the original DoD ("brute-forcing an 8-digit ID no longer grants
    stream access") for the first unauthenticated reader.
  - **(c) defer to SaaS-tier authorization** — treat real per-session auth as a
    premium-layer concern and close it there rather than in the open core.

Step 1's per-source rate limit + same-origin CORS default (PR #222) remain the
shipped mitigation until this is decided. An incidental §3.6 doc↔implementation
drift found en route (auto-create described as `POST /api/sessions` vs the actual
lazy client-side create) is corrected in this same PR to keep the design doc
internally consistent.

### A4. Protect `dev` and make the required checks real

- **Problem:** `CLAUDE.md` itself documents that auto-merge cannot arm because
  `dev` is unprotected. Combined with A1, nothing enforces that merged code
  passed any tests.
- **Proposed change:** (owner action, but a session can prepare it) add a
  branch-protection rule on `dev` requiring the CI check(s) from A1; enable
  auto-merge repo-wide. Document the setting in `CLAUDE.md` (it already
  describes the target state).
- **Definition of done:** `enable_pr_auto_merge` works on a `dev` PR; a red
  PR cannot merge.
- **Effort:** XS (settings) — mostly a Jakob action; do together with A1.
- **When marking checks required (noted during C5, PR #234):** every workflow
  here — `ci.yml` (test + lint jobs) and the C5 `Security Scan` — sets
  `paths-ignore` (`**.md`, `docs/**`, `SMALL_FIXES.md`) on its triggers, so a
  docs-only PR never runs them. That is fine while the checks are non-required,
  but GitHub treats a *required* check that is skipped (never reported) as
  perpetually pending, which blocks merge — the standard `required check` +
  `paths-ignore` footgun. Before promoting any check to required, drop
  `paths-ignore` from that workflow (or add a required-status shim job that
  reports success on the skipped paths); note this affects the test jobs too,
  not just the security/lint ones.

---

## Priority 2 — structural decomposition (agent-effectiveness)

The god files are where agent sessions burn context and where review findings
cluster. Decompose them behavior-preservingly, one slice per PR.

### B1. Decompose `frontend/web/src/App.jsx` (1 865 lines)

- **Problem:** App.jsx owns canvas wiring, shared-session lifecycle
  (`applyServerSession`, `ensureSyncConnected`, auto-save), dialog
  orchestration, chat wiring, i18n plumbing and URL handling in one component
  with ~106 hook call sites. Two of the seven open `SMALL_FIXES.md` items are
  App.jsx session-lifecycle bugs — symptoms of state transitions that are hard
  to see in a file this size.
- **Proposed change:** extract in this order (one PR each):
  1. `useSharedSession()` hook module — server-session load/apply/switch logic
     (`applyServerSession`, `loadSessionFromServer`, `serverStateToMirror`) —
     this is where the known bugs live; extraction plus tests can resolve both
     open SMALL_FIXES entries in the process (they stop being "pre-existing"
     when the code is being rewritten — coordinate with the backlog).
  2. `useSyncConnection()` — sessionSyncClient lifecycle (`ensureSyncConnected`,
     reconnect, teardown).
  3. Dialog orchestration — a `dialogs/` registry component so App.jsx stops
     tracking a dozen open/close states.
  4. Chat/proposal wiring into a container component.
- **Definition of done (per slice):** App.jsx shrinks; extracted module has its
  own test file; no behavior change (existing vitest suites green).
- **Effort:** M per slice, 3–4 slices

> **Session brief (slice 1):** In CommunityOverview, extract the shared-session
> lifecycle logic from frontend/web/src/App.jsx into a dedicated
> `src/hooks/useSharedSession.js` (or equivalent) module with unit tests:
> applyServerSession, loadSessionFromServer, serverStateToMirror and the
> session-switch error handling. Behavior-preserving refactor. While moving the
> code, address the two open SMALL_FIXES.md entries dated 2026-07-10 that live
> in exactly this logic (malformed resolved.edges validation; broken sync
> client installed in syncRef before connect succeeds) and add the regression
> tests described there. Update SMALL_FIXES.md.

### B2. Decompose `backend/api_host/server.py` (1 180 lines)

- **Problem:** `create_app()` inlines the auth middleware, startup diagnostics,
  the legacy SSE endpoint, the MCP mount shim class, `execute_tool`, exports,
  favicon/redirect/health/info/logout/federation/agents endpoints and static
  mounting. Any change to any of these pays for all of them.
- **Proposed change:** split into modules under `backend/api_host/`:
  `middleware.py` (auth), `diagnostics.py` (startup diagnostics builders —
  lines 64–217 are already pure functions), `mcp_mount.py` (the
  MCPBrowserHandler shim), `system_routes.py` (health/info/favicon/logout),
  `agent_routes.py`, `session_stream.py` (legacy SSE). `create_app` becomes
  composition only.
- **Definition of done:** server.py < ~300 lines of composition; api_host tests
  green unchanged; no route path changes.
- **Effort:** M

> **Session brief:** In CommunityOverview, refactor
> backend/api_host/server.py into composable modules (auth middleware, startup
> diagnostics, MCP mount shim, system routes, agent routes, legacy session SSE)
> per STRUCTURE_REVIEW.md item B2. Pure move-and-import refactor: no route,
> behavior, or signature changes. All existing api_host tests must pass
> unmodified.

### B3. Home the stray flat modules under `backend/`

- **Problem:** eight modules sit loose at `backend/` root outside the
  documented package architecture: `chat_logic.py` (1 076 lines — used only by
  `backend/ui/chat_service.py`), `llm_providers.py`, `authorization.py`,
  `request_context.py`, `config_context.py`, `config_loader.py`,
  `document_processor.py`, `language_policy.py`. The architecture diagram in
  DEVELOPMENT.md doesn't mention most of them, so a new session has no map for
  where cross-cutting logic lives.
- **Proposed change:** move `chat_logic.py` → `backend/ui/chat_logic.py`
  (delete the root-level compat shim `./chat_logic.py` at the same time and fix
  its importers); group `llm_providers.py` + `language_policy.py` into
  `backend/llm/`; group `authorization.py` + `request_context.py` +
  `config_context.py` into `backend/runtime/` (or fold into `core`); decide
  whether `document_processor.py` (53 lines) still earns a file vs merging into
  `backend/ui/document_service.py`. Keep import aliases for one release only if
  external consumers exist (they shouldn't — this is an app, not a library).
- **Definition of done:** `backend/` root contains only packages,
  requirements files, `DEVELOPMENT.md` and `Dockerfile`; DEVELOPMENT.md
  architecture tree matches reality; full suite green.
- **Effort:** M (mechanical; can be two PRs: chat/llm first, runtime seam second)
- **Note (PR #224):** the six named modules (`chat_logic`, `llm_providers`,
  `language_policy`, `authorization`, `request_context`, `config_context`) are
  homed and the root shim is gone. `config_loader.py` and `document_processor.py`
  still sit at `backend/` root: the B3 proposal never assigned `config_loader.py`
  a target package, and `document_processor.py` carries an open "merge into
  `backend/ui/document_service.py` vs keep" question — both need a design
  decision, so they are split out as item **B6** below rather than forced here.
  The "root contains only packages" clause is therefore satisfied once B6 lands.

> **Session brief:** In CommunityOverview, relocate the flat modules at
> backend/ root into their owning packages per STRUCTURE_REVIEW.md item B3
> (chat_logic → backend/ui, llm_providers + language_policy → backend/llm,
> authorization/request_context/config_context → backend/runtime). Remove the
> root-level chat_logic.py compatibility shim and update all imports and tests.
> Update the architecture tree in backend/DEVELOPMENT.md and the Key Files
> table in CLAUDE.md. Pure relocation, no logic changes.

### B4. Split `backend/service/service.py` (1 575) and `backend/core/storage.py` (1 370)

- **Problem:** GraphService and GraphStorage each mix several concerns
  (CRUD, search/scoring, similarity, views, federation adoption, session
  push). The review-loop guidance in CLAUDE.md explicitly calls out
  "local vs. federation paths doing the same thing differently" — a symptom of
  both paths living far apart inside huge files.
- **Proposed change:** split by concern into a package
  (`backend/service/graph_ops.py`, `search.py`, `views.py`, …) with
  `GraphService` as a thin facade so callers keep one entry point; same pattern
  for storage (`crud.py`, `search.py`, `persistence.py`). Do only after A1 —
  this refactor needs full CI coverage as the safety net.
- **Effort:** L (2–3 PR slices). Lower urgency than B1–B3; do when a feature
  next forces a change in these files.

### B5. Split `packages/ui-graph-canvas/src/components/GraphCanvas.jsx` (1 589)

- **Problem:** canvas rendering, all context menus, remote-position handling
  and annotation logic in one component in the shared package. (The file had
  grown to 1 636 lines by the time B5 was picked up — the original 1 463 count
  was stale.)
- **Proposed change:** extract context menus into their own components (props
  contract already exists via `contextMenuLabels`), and remote-position/
  pending-position logic into a hook (which is also where the open
  `SMALL_FIXES.md` pruning entry for `pendingRemotePositionsRef` lives —
  resolve it in the extraction).
- **Effort:** M (2 slices)
- **Note (PR #230, slice 1/2):** the remote-position/pending-position logic is
  extracted into `packages/ui-graph-canvas/src/hooks/useRemotePositions.js` and
  the `pendingRemotePositionsRef` pruning entry is resolved (30s TTL on pending
  entries). Remaining slice 2: extract the four inline context menus (and,
  optionally, the `remoteAnnotationOps` effect) out of `GraphCanvas.jsx`.

### B6. Home `config_loader.py` and `document_processor.py`

- **Problem:** the B3 relocation (PR #224) homed six flat modules but left
  `config_loader.py` (22 KB, widely imported) and `document_processor.py`
  (53 lines) at `backend/` root, because neither had a settled destination:
  B3's proposed change never assigned `config_loader.py` a target package, and
  `document_processor.py` carries an open "merge into
  `backend/ui/document_service.py` vs keep as its own file" question. Until
  these land, the B3 "root contains only packages" DoD is not fully met.
- **Proposed change:** decide `config_loader.py`'s home (likely `backend/core/`
  or a new `backend/config/` package — it loads schema/federation config and is
  imported across layers) and relocate it with its importers; decide whether
  `document_processor.py` earns a file or folds into
  `backend/ui/document_service.py`. Update the DEVELOPMENT.md / README trees and
  the CLAUDE.md Key Files entry for `config_loader.py`. Pure relocation, no
  logic changes — but the destination is a design decision, so surface it to
  Jakob before moving `config_loader.py`.
- **Definition of done:** `backend/` root contains only packages, requirements
  files, `DEVELOPMENT.md` and `Dockerfile`; full suite green.
- **Effort:** S

---

## Priority 3 — hygiene, tooling, consistency

### C1. Remove stale tracked data and fix the scripts that point at it

- **Problem:** `backend/graph.json` (1.4 MB) is tracked in git — it is stale
  runtime data from before the `data/active/` scheme (last touched in PR #162);
  DEVELOPMENT.md line 17 still documents it as the data location. A root-level
  `graph.json` stub also lingers. `scripts/generate_embeddings.py` and
  `scripts/migrate_embeddings.py` hardcode `backend/graph.json`, so they
  operate on stale data today.
- **Proposed change:** delete `backend/graph.json` and root `graph.json` from
  tracking (add ignore rules), make both scripts take `--graph-file` defaulting
  to `data/active/graph.json`, fix DEVELOPMENT.md.
- **Definition of done:** `git ls-files '*.json'` shows no runtime graph data
  outside `data/examples/` and `config/*/graph.json` (seed data); scripts run
  against the active file.
- **Effort:** S

> **Session brief:** In CommunityOverview, remove the stale tracked runtime
> data files backend/graph.json and ./graph.json, add gitignore entries, change
> scripts/generate_embeddings.py and scripts/migrate_embeddings.py to accept a
> --graph-file argument defaulting to data/active/graph.json, and correct the
> data-location line in backend/DEVELOPMENT.md. Check nothing else references
> the removed paths.

### C2. Introduce lint/format gates

- **Problem:** no ESLint/Prettier config anywhere in the JS workspaces; Python
  has only `black` in requirements-dev with nothing enforcing it. For a
  codebase developed primarily by AI agents, mechanical style/correctness
  gates (unused imports, undefined vars, hook-rules violations) are the
  cheapest review layer that exists — and it's missing.
- **Proposed change:** add `ruff` (lint + format, replacing black) for Python
  and flat-config ESLint (with `eslint-plugin-react-hooks`) + Prettier for the
  three JS workspaces; wire both into CI as separate non-blocking jobs first,
  then flip to required once the baseline is clean. Expect a large one-time
  autofix commit — keep it separate from any logic change.
- **Effort:** M (baseline cleanup dominates)

> **Session brief:** In CommunityOverview, introduce ruff for backend/ and
> ESLint (flat config, react + react-hooks plugins) + Prettier for the JS
> workspaces. One PR: config files, npm/pip wiring, CI jobs (non-required
> initially), and a separate autofix-only commit bringing the tree to a clean
> baseline. No manual logic changes mixed into the autofix commit.

### C3. Unify HTTP client and dependency policy

- **Problem:** the backend uses `httpx2` (Pydantic's maintained continuation
  of httpx) while `services/mcp_oauth_gateway` pins legacy `httpx==0.28.0`;
  `requests` was *also* in the base requirements (used by the webhook delivery
  worker and the MCP loader — not document processing as originally stated).
  The gateway pins exact versions while the backend policy is `>=` minimums.
  The gateway's `python-jose 3.3.0` should be reviewed against its known CVEs
  (CVE-2024-33663/33664) or replaced with `pyjwt`.
- **Proposed change (sliced):**
  - **Slice 1 — backend HTTP-client unification (done, PR #232):** migrate the
    two remaining `requests` call sites (`backend/core/events/delivery.py`,
    `backend/agents/mcp_loader.py`, plus the `scripts/test-e2e-live.py` dev
    helper) to the already-present `httpx2`, and drop `requests` from
    `backend/requirements.txt`. Pure, behaviour-preserving client swap
    (`follow_redirects=True` matches the old redirect behaviour; exceptions
    mapped `Timeout`→`TimeoutException`, `RequestException`→`RequestError`).
  - **Slice 2 — gateway alignment (open, needs an owner decision):** the
    remaining three sub-parts all touch `services/mcp_oauth_gateway`, a
    separately-deployed component that *deliberately* exact-pins its
    dependencies (see the C2 note) and whose auth path is security-sensitive:
    (a) migrating its `httpx==0.28.0` to `httpx2`, (b) relaxing its exact pins
    toward the repo's `>=` policy — which contradicts the deliberate
    reproducibility pinning, so it is a policy decision, not a mechanical
    change — and (c) the `python-jose` → `pyjwt` swap, a security-sensitive
    change to JWT minting/verification (`auth.py`: `get_unverified_claims`,
    `encode`, `decode`, `JWTError`). These belong together in a gateway-focused
    session with explicit owner sign-off on the pin-policy and JWT-library
    questions.
- **Effort:** S–M (slice 1 S; slice 2 S–M + decision)

### C4. Upgrade the frontend build image off Node 18

- **Problem:** `Dockerfile` builds the frontend on `node:18-alpine`; Node 18
  reached end-of-life in April 2025 and `.nvmrc` already says 20.
- **Proposed change:** bump to `node:20-alpine` (or 22 LTS) and align docs
  (`backend/DEVELOPMENT.md` prerequisites say "Node.js 18+").
- **Effort:** XS

### C5. Add dependency/security scanning to CI

- **Problem:** no `pip-audit`, `npm audit`, or CodeQL runs anywhere; secrets
  scanning relies on GitHub defaults.
- **Proposed change:** add a scheduled + PR-triggered job running `pip-audit`
  against the requirements files and `npm audit --omit=dev` per workspace;
  enable CodeQL default setup in repo settings (owner action). Non-blocking
  reporting first.
- **Effort:** S
- **Note (PR #234):** the `pip-audit` + `npm audit` reporting is delivered as a
  dedicated `.github/workflows/security-scan.yml` (pull-request + weekly
  schedule + manual dispatch), kept out of `ci.yml` so the schedule never
  triggers a build. Each audit step is `continue-on-error` (reporting-first:
  findings show a red step + a job summary but never block a merge). The root
  lockfile covers all three workspaces, so one root `npm audit --omit=dev`
  reports across web/widget/canvas; `requirements-ml.txt` is skipped (its torch
  extra-index is slow/fragile to resolve and adds no default-install coverage).
  **CodeQL default setup remains an owner action** — it is enabled in the repo
  Security settings, not via a committed workflow.

### C6. Consolidate the root start scripts

- **Problem:** five `start-*.sh` scripts at the repo root
  (`start-dev.sh`, `start-federated-dev.sh`, `start-sprint.sh`,
  `start-sspcloud-metadata.sh`, `start-webhook-server.sh`), several of which
  are environment-specific wrappers.
- **Proposed change:** keep `start-dev.sh` at root as the canonical entry;
  move the wrappers under `scripts/` (they mostly compose `start-dev.sh`
  flags); update README/SSPCloud docs.
- **Effort:** S

### C7. Commit a root lockfile so frontend CI is reproducible

- **Problem (premise partly stale — first flagged in the C2/PR #227 note,
  corrected here):** a root `package-lock.json` **is** now tracked (committed
  before the C2 lint work), so the "no lockfile is tracked" half of the original
  premise is stale. What
  remained true: the two frontend CI jobs (`frontend-tests`, `frontend-lint`)
  still ran `npm install`, which resolves fresh transitive versions on every run
  and ignores the committed lockfile, and neither `setup-node` step enabled the
  npm cache (which keys off a lockfile). So a green run still did not guarantee
  the next run installed the same tree.
- **Proposed change:** switch both frontend CI jobs from `npm install` to
  `npm ci` (installs exactly the tracked lockfile, failing loudly if it drifts
  from `package.json`) and enable `cache: 'npm'` on their `setup-node` steps.
  Verify the committed lockfile installs the same versions the suite currently
  passes on. (The "generate and commit a lockfile" step is already done.)
- **Effort:** S

---

## Priority 4 — documentation and knowledge structure

Docs are the interface agents (and new humans) load first; drift here has
outsized cost.

### D1. Realign the architecture docs with the tree

- **Problem (partly resolved en route by B3/C1):** the `backend/DEVELOPMENT.md`
  architecture tree portion of this problem is **already fixed** — B3 (PR #224)
  added `federation/`, `agents/`, `skills/`, `llm/`, `runtime/` to the tree and
  C1 (PR #223) removed the stale `backend/graph.json` data-file line, so that
  tree now matches the package layout. What remained: README's project structure
  omitted `services/mcp_oauth_gateway` (and several backend packages —
  `agents/`, `federation/`, `tests/`, `core/events/`), and the two "enablement
  plan" docs plus `docs/sprint_documentation/` mixed design-proposal and
  historical material into the same namespace as current-state guides with no
  status marking.
- **Proposed change:** bring README's project structure up to date with the tree
  (add `services/mcp_oauth_gateway` and the missing backend packages/modules);
  add a `docs/README.md` index that tags every doc as **current** /
  **design (target state)** / **historical**, with `sprint_documentation/`
  under the historical heading. This lets an agent session skip stale material
  instead of reconciling it. (The `DEVELOPMENT.md` tree needed no further change
  — B3/C1 already realigned it; the remaining `config_loader.py` /
  `document_processor.py` root modules are tracked separately under B6.)
- **Effort:** S–M

### D2. Keep CLAUDE.md, CI, and reality in one truth

- **Problem:** several CLAUDE.md statements are aspirational or stale: "CI runs
  only the core test suite" (fix via A1), the ML-dependency rule (fix via A2),
  the auto-merge setup note (fix via A4). Each mismatch teaches an agent
  session something false.
- **Proposed change:** fold CLAUDE.md updates into each of A1/A2/A4 (their
  briefs already say so); this item is the checklist to verify afterwards that
  no contradiction remains.
- **Effort:** XS (verification pass)

---

## Technology-choice review (no action required)

- **NetworkX + JSON file persistence:** right for the current single-instance
  scale; file locking + atomic writes are implemented. The scaling path is a
  real storage backend behind `storage_backends.py`. Recommended preparation
  (not migration): a backend-agnostic contract test suite that any future
  backend must pass. Do this only when a second backend becomes concrete.
- **FastAPI + FastMCP:** sound; the REST/MCP parity tests are a genuine asset.
- **React 18 + React Flow 11 + Zustand 4:** sound; no framework churn
  warranted. The `ui-graph-canvas` package consumed as raw source
  (`main: src/index.js`, built by the consumer's Vite) is fine while it stays
  in-repo.
- **npm workspaces monorepo + separate Python backend:** fine. The absent piece
  is shared tooling (C2), not structure.
- **Two Docker images (core + gateway) with infra-repo dispatch:** clean
  separation of build vs deploy responsibilities.

## Cross-repo note

Findings that concern the private repos are intentionally not documented here
(public/private boundary); they were reported directly to the project owner in
the review session.

---

## Execution order and status

This table is the source of truth for progress. A session working through this
backlog updates the **Status** column in the same PR that implements the item:
`open` → `in progress (slice X/Y, PR #N)` → `done (PR #N)`. Items marked
*(owner action)* are repo-settings changes only Jakob can make — a session may
prepare and document them but must not block on them; note them in the session
summary instead.

| # | Item | Effort | Depends on | Status |
|---|------|--------|-----------|--------|
| 1 | A2 ML deps out of base requirements | M | — | done (PR #220) |
| 2 | A1 full test suite in CI | M | A2 | done (PR #221) |
| 3 | A4 protect `dev` + auto-merge | XS | A1 | open *(owner action)* |
| 4 | A3 session-ID hardening, step 1 (rate limit + CORS) | S–M | — | done (PR #222) |
| 5 | C1 stale data removal + script fix | S | — | done (PR #223) |
| 6 | B3 home the flat backend modules | M | A1 | done (PR #224) |
| 7 | B2 decompose server.py | M | A1 | done (PR #225) |
| 8 | B1 decompose App.jsx (slice 1: shared-session hook) | M | — | done (PR #226) |
| 9 | C2 lint gates | M | A1 | done (PR #227) |
| 10 | A3 step 2 (stream token scheme) | M | A3 step 1 | open *(owner action — design decision; see A3 Update 2026-07-12, PR #228)* |
| 11 | B1 remaining slices | M×2 | B1 slice 1 | in progress (slice 2/4, PR #229) |
| 12 | B5 GraphCanvas decomposition | M | — | in progress (slice 1/2, PR #230) |
| 13 | C3 HTTP client + dependency policy | S–M | — | in progress (slice 1/2, PR #232) |
| 14 | C4 Node 18 → 20 build image | XS | — | done (PR #233) |
| 15 | C5 security scanning in CI | S | A1 | done (PR #234) — CodeQL default setup still *(owner action)* |
| 16 | C6 start-script consolidation | S | — | done (PR #235) |
| 17 | D1 docs realignment + index | S–M | B3, C1 | done (PR #236) |
| 18 | D2 CLAUDE.md truth verification pass | XS | A1, A2, A4 | open |
| 19 | B4 service.py / storage.py split | L | A1, next feature touching them | open |
| 20 | C7 reproducible frontend CI (`npm ci` + lockfile) | S | A1 | done (PR #237) |
| 21 | B6 home `config_loader.py` + `document_processor.py` | S | B3 | done (PR #238) |

### How a session updates this document

1. Pick the first `open` item (top to bottom) whose dependencies are `done`,
   skipping *(owner action)* rows. Before starting, check
   `git log --oneline origin/dev -20` to confirm the item hasn't already been
   addressed.
2. Implement it per its *Session brief*, following the Standard Development
   Workflow in `CLAUDE.md` (branch → tests → PR to `dev` → review loop → merge).
3. In the same PR, update this file: set the Status cell, and on completion
   move a one-line summary (date, PR number, what changed) to *Completed*
   below.
4. If reality has drifted from what an item describes (already fixed, wrong
   assumption, changed priorities), correct the item text rather than forcing
   the described change — this document must stay true.
5. New structural findings discovered en route: add them as new rows/items
   (or `SMALL_FIXES.md` entries if they are small bugs), never fix them in the
   same branch.

## Completed

- **[2026-07-13] B6 — `config_loader.py` and `document_processor.py` homed into
  packages (PR #238).** Relocated the last two flat modules at `backend/` root,
  completing the B3 "root contains only packages" clause: `config_loader.py` →
  new **`backend/config/`** package and `document_processor.py` →
  **`backend/ui/`** (its sole consumer). config_loader's home was the B6 design
  decision — `backend/config/` was chosen over `backend/core/` because
  config_loader imports `backend.runtime.*` and (mid-module, cycle-breaking)
  `backend.skills.loader`, so homing it in the lowest layer would invert the
  layering; a neutral `backend/config/` package avoids that, and no reverse edge
  exists so no new cycle. document_processor kept as its own file rather than
  folded into `document_service.py`. Pure relocation — no logic/route/signature
  changes; all importers and both import idioms rewritten; the `core.models` /
  `core.storage` config_loader imports stay lazy. README project tree and the
  CLAUDE.md Key Files entry updated. `pytest backend/ -q` 944 passed / 16 skipped
  (baseline unchanged); ruff check + format clean. Opened as a **draft pending
  Jakob's sign-off on the config_loader home** (B6 requires surfacing the
  destination before moving) since the interactive channel was unavailable.

- **[2026-07-13] C7 — reproducible frontend CI via `npm ci` + tracked lockfile
  (PR #237).** The "commit a root lockfile" half of C7 was already stale — a root
  `package-lock.json` had been committed before the C2 lint work — so the item
  text was corrected. The real remaining gap: both frontend CI jobs
  (`frontend-tests`, `frontend-lint`) still ran `npm install`, which re-resolves
  fresh transitive versions each run and ignores the committed lockfile, and
  neither `setup-node` step enabled the lockfile-keyed npm cache. Switched both
  jobs to `npm ci --no-audit --no-fund` (installs exactly the lockfile, fails
  loudly on `package.json` drift) and enabled `cache: 'npm'` on both. CI-YAML +
  docs only, no source change. Verified `npm ci --dry-run` clean (lockfile in
  sync) and the full unit suite green on the `npm ci`-installed tree (canvas 79 /
  web 213 / widget 56 = 348 tests). Added a one-line reproducible-install note to
  `backend/DEVELOPMENT.md`'s CI section.

- **[2026-07-12] D1 — status-tagged docs index + README structure realignment
  (PR #236).** Added `docs/README.md`, an index tagging every document as
  **current** (present-system guides/process docs), **design (target state)**
  (the runtime/extension/plugin enablement plans) or **historical**
  (`sprint_documentation/`, `expert-agents-implementation-plan.md`,
  `MIGRATION_NOTES.md`), so a session can skip stale material instead of
  reconciling it — all 20 indexed docs verified present with resolving links.
  Brought README's `## Project Structure` up to date with the tree: added the
  omitted top-level `services/mcp_oauth_gateway`, the backend packages `agents/`,
  `federation/`, `tests/`, `core/events/`, plus `storage_backends.py` and the two
  root modules (`config_loader.py`, `document_processor.py`), and a pointer to the
  new index. The `backend/DEVELOPMENT.md` tree needed no change — B3 (#224) had
  already added federation/agents/skills/llm/runtime and C1 (#223) removed the
  stale `graph.json` data-file line — so the D1 item text was corrected to reflect
  that and scope the item to the README + index work. `config_loader.py` /
  `document_processor.py` relocation stays tracked under B6. Docs-only, no code
  change.

- **[2026-07-12] C6 — root start scripts consolidated under `scripts/` (PR #235).**
  Kept `start-dev.sh` at the repo root as the canonical entry point and moved the
  four environment-specific wrappers into `scripts/`:
  `start-federated-dev.sh`, `start-sprint.sh`, `start-sspcloud-metadata.sh`, and
  `start-webhook-server.sh`. The three wrappers that source
  `config/profile-utils.sh` derive every path from `SCRIPT_DIR` and rely on the
  profile-utils contract that `SCRIPT_DIR` is the repo root, so each now resolves
  one level up from its own location (`$(dirname "${BASH_SOURCE[0]}")/..`) rather
  than renaming the variable and breaking that sourcing contract — all downstream
  `$SCRIPT_DIR/...` paths and `cd "$SCRIPT_DIR"` are unchanged. `start-webhook-server.sh`
  is self-contained and moved as-is. Pure relocation — no script logic, flags, or
  behaviour changed. Updated every in-repo reference (`README.md`,
  `config/README.md`, the `.gitignore` and `config/profile-utils.sh` comments,
  `docs/sprint_documentation/US-03-IMPLEMENTATION-PLAN.md`) and each moved script's
  own usage header to the `scripts/` path. Verified `bash -n` clean on all five
  scripts and that `SCRIPT_DIR` resolves to the repo root (profile-utils sources
  correctly) from the new location.

- **[2026-07-12] C5 — dependency security scanning added to CI (PR #234).** Added
  `.github/workflows/security-scan.yml`, a standalone workflow (kept out of
  `ci.yml` so its weekly `schedule` never triggers a Docker build) that runs
  `pip-audit` over the backend base, dev and gateway requirement files and one
  root `npm audit --omit=dev` covering all three JS workspaces via the shared
  lockfile. Triggers: `pull_request`, a weekly `schedule` (re-audits unchanged
  deps against fresh advisories), and `workflow_dispatch`. Reporting-first — each
  audit step is `continue-on-error`, so a finding surfaces as a red step plus a
  job-summary table but never blocks a merge (same rollout path as the C2 lint
  gates: flip to blocking + required once the baseline is understood, alongside
  the A4 branch-protection work). `requirements-ml.txt` is intentionally excluded
  (its pytorch extra-index is slow/fragile to resolve and adds no coverage beyond
  the default install). Verified locally: `npm audit --omit=dev` flags the tracked
  `lodash` advisory and the gateway `pip-audit` surfaces the known
  `python-jose 3.3.0` CVEs (aligning with C3). **CodeQL default setup is left as an
  owner action** (enabled in repo Security settings, not a committed workflow), so
  row 15 tracks it as a remaining *(owner action)*.

- **[2026-07-12] C4 — frontend build image upgraded off Node 18 (PR #233).** Bumped
  the `Dockerfile` frontend builder stage from `node:18-alpine` (EOL April 2025) to
  `node:20-alpine`, matching the Node 20 already pinned in `.nvmrc` and both CI
  `setup-node` jobs, and updated the `backend/DEVELOPMENT.md` prerequisite from
  "Node.js 18+" to "Node.js 20+". No application code, dependency, or build-step
  change — a single-version alignment across the documented, CI-tested, and
  image-build toolchains. Stayed on 20 (not 22 LTS) to keep one Node version
  everywhere rather than introduce a new one.

- **[2026-07-12] C3 slice 1 — backend HTTP-client unification, `requests` dropped
  (PR #232).** Migrated the two remaining `requests` call sites off the redundant
  second HTTP client onto the already-present `httpx2`: the SSRF-guarded webhook
  delivery worker (`backend/core/events/delivery.py`) and the MCP loader's info /
  fetch / brave-search GETs (`backend/agents/mcp_loader.py`), plus the
  `scripts/test-e2e-live.py` manual dev helper. Removed `requests>=2.31.0` from
  `backend/requirements.txt`; no app or script source imports `requests` any more.
  Behaviour-preserving: `follow_redirects=True` matches the old `requests`
  redirect-following default and exceptions are mapped (`Timeout` →
  `TimeoutException`, `RequestException`/`.exceptions.RequestException` →
  `RequestError`); the client-independent `is_safe_url` SSRF check is untouched.
  Updated the delivery tests to patch `httpx.post` / raise `httpx.TimeoutException`.
  Review loop caught one real behavioural deviation and fixed it in-slice: the MCP
  loader's `/info` block caught `httpx.RequestError`, but `requests` had folded
  two more cases into `RequestException` that `httpx` surfaces outside it — a
  200/non-JSON body (`json.JSONDecodeError`, a `ValueError`) and a malformed
  configured URL (`httpx.InvalidURL`) — so the handler was broadened to
  `(httpx.RequestError, httpx.InvalidURL, ValueError)` with regression tests for
  both (`TestConnectHttpInfoQuery`). Full backend suite green (944 passed / 16 skipped).
  Logged one finding en route
  (SMALL_FIXES 2026-07-12): the SSRF check is not re-applied across redirects — a
  latent, pre-existing gap preserved by keeping `follow_redirects=True`. C3 slice 2
  (gateway `httpx`→`httpx2`, pin-policy alignment, and the security-sensitive
  `python-jose`→`pyjwt` swap) stays open under row 13 and needs an owner decision.

- **[2026-07-12] B5 slice 1 — remote-position logic extracted from `GraphCanvas.jsx`
  into `useRemotePositions` (PR #230).** Moved the `pendingRemotePositionsRef` and
  the two remote-position effects (apply from another client + catch-up once a
  not-yet-mounted node appears) out of the 1 636-line god component into
  `packages/ui-graph-canvas/src/hooks/useRemotePositions.js` (`GraphCanvas.jsx`
  1 636 → 1 589). Behaviour-preserving — the apply-on-mount catch-up and the
  same-array-reference loop guard are intact. Resolved the open 2026-07-07
  `SMALL_FIXES.md` entry: pending entries now carry an arrival timestamp and are
  pruned after a 30s TTL, so a position whose node was removed or never mounts no
  longer leaks for the session. Added `useRemotePositions` unit tests (5, including
  a TTL-prune regression). Canvas suite green (8 files / 79 tests). B5 slice 2
  (context-menu extraction) remains open under row 12.

- **[2026-07-12] B1 slice 2 — sync-client lifecycle extracted from `App.jsx`
  into `useSyncConnection` (PR #229).** Moved the per-session `SessionSyncClient`
  create/connect/teardown flow out of the god component into
  `frontend/web/src/hooks/useSyncConnection.js`, which now owns `syncRef` +
  `syncHandlersRef`, the connection-scoped state reset together on teardown
  (`remotePositions`, `remoteAnnotationOps`, `roster`, `remoteSelections`,
  `opStreamReady`), `ensureSyncConnected` (lazy create / same-session reconnect
  fast path / previous-client teardown) and the session-change/unmount teardown
  effect. Behaviour-preserving: the connect-before-install guard (a client whose
  `connect()` throws is never left in `syncRef`) and the "only close a client
  that still belongs to the captured session id" teardown check are both intact;
  op-application handlers (`applyRemoteOp`, `applyToolResultCommand`,
  `resyncFromServer`, the handler-wiring effect) stay in `App.jsx` and are still
  routed through `syncHandlersRef`. `App.jsx` drops ~92 lines and no longer
  imports `SessionSyncClient`. Added `useSyncConnection` unit tests (7). Frontend
  suite green (web 213 / canvas 74 / widget 56). B1 slices 3 (dialog
  orchestration) and 4 (chat/proposal wiring) remain open under row 11.

- **[2026-07-12] A3 step 2 review — stream-token scheme reclassified as an owner
  design decision (PR #228).** Tracing the session-creation flow showed the
  step-2 premise (a token minted by an authenticated creator via
  `POST /api/sessions`) does not match the implementation: the browser never
  calls the create endpoint (`createServerSession` is uncalled), ids are
  client-generated (`generateVisualizationSessionId`) and sessions materialise
  lazily over the auth-bypassed stream (`get_or_create`), so there is no
  authenticated channel to deliver the creator's token and the token's value is
  conditional on Basic Auth being active. Corrected the A3 item with the design
  fork (authenticated-POST-first vs joiners-only vs SaaS-tier) and moved row 10 to
  *(owner action)* pending Jakob's call on the creation-flow model; updated
  `MULTI_USER_SESSIONS_DESIGN.md` §3.9 to record the blocker and corrected an
  incidental §3.6 auto-create description to match the lazy client-side flow. No
  code change.

- **[2026-07-12] C2 — ruff / eslint / prettier lint gates introduced
  (PR #227).** `ruff` replaces `black` for the Python backend (lint + format;
  config in root `pyproject.toml`, scoped to `backend/` and `scripts/`); ESLint
  flat config (`eslint.config.mjs`, react + react-hooks) plus Prettier cover the
  three JS workspaces. `react-hooks/rules-of-hooks` is an error; the newer
  React-Compiler-era advisories are warnings so the baseline stays green. Two CI
  jobs (`python-lint`, `frontend-lint`) run the gates but are intentionally left
  out of the `build` job's `needs`, so they are **non-required** until `dev`
  branch protection lands (A4). Delivered as three separated commits — tooling
  config, manual residual fixes (F821 forward-ref `TYPE_CHECKING` imports, two
  E402, dead-assignment removals; no behaviour change), and a pure autofix sweep
  (137 ruff fixes + `ruff format`, `eslint --fix` + `prettier --write`). Backend
  suite 942 passed / 16 skipped and frontend workspaces (canvas 74 / web 206 /
  widget 56) unchanged. `services/mcp_oauth_gateway/` is excluded from ruff on
  purpose (separate component — folds into C3). Noted en route: a root
  `package-lock.json` is already tracked, so C7's "no lockfile is tracked"
  premise is partly stale (its `npm install` → `npm ci` point still stands).

- **[2026-07-12] B1 slice 1 — shared-session lifecycle extracted from `App.jsx`
  into `useSharedSession` (PR #226).** Moved `applyServerSession`,
  `loadSessionFromServer` and `serverStateToMirror` out of the 1 875-line
  `App.jsx` god component (~140 lines lighter) into a testable
  `frontend/web/src/hooks/useSharedSession.js` with injected dependencies; homed
  the four pure annotation⇄canvas transforms in
  `frontend/web/src/utils/sessionAnnotations.js`, shared by the hook and by
  App's incremental-op / snapshot paths (the `annotationTranslation` test now
  imports them directly, dropping the test-only re-export from `App.jsx`).
  Behaviour-preserving. Resolved the two open 2026-07-10 `SMALL_FIXES.md`
  entries that lived in this logic: `applyServerSession` now validates the
  resolved node/edge shape before its first mutating call (a malformed payload
  fails the switch atomically instead of half-clearing the canvas), and
  `ensureSyncConnected` connects before installing the client in `syncRef`,
  returning `null` on failure so a persistent connect error neither sticks a
  dead client on the fast path nor throws out of the auto-save call site.
  Added `useSharedSession` unit tests plus a `sessionFlow` integration
  regression for the contained connect failure (verified failing pre-fix).
  `ensureSyncConnected`'s own extraction into a `useSyncConnection` hook landed
  as B1 slice 2 (PR #229). Frontend suite green (canvas 74 / web 206 / widget 56).

- **[2026-07-12] B2 — `api_host/server.py` decomposed into composable modules
  (PR #225).** Split the 1 180-line `create_app()` monolith into dedicated
  modules under `backend/api_host/`: `middleware.py` (auth + CORS install),
  `diagnostics.py` (startup/readiness builders + public-path constants),
  `mcp_mount.py` (MCP instructions, request-auth ASGI binder, `MCPBrowserHandler`,
  `mount_mcp`), `session_stream.py` (visualization-session registry lifecycle +
  legacy `/sessions/{id}/stream` SSE), `tool_routes.py` (`/execute_tool`,
  `/export_graph`, `SAFE_TOOLS`), `system_routes.py` (favicon/collect, health/
  ready/startup-diagnostics, root, logout, info) and `agent_routes.py`
  (`/federation/*` + `/agents/*`). `server.py` is now composition only
  (335 lines). Pure move-and-import refactor — no route paths, behaviour or
  signatures change; middleware order (auth before CORS) and route/mount order
  preserved; `FastMCP` stays importable from `backend.api_host.server` for the
  MCP-transport test. Full backend suite unchanged (942 passed / 16 skipped).

- **[2026-07-11] B3 — flat backend modules homed into owning packages
  (PR #224).** Relocated the loose modules at `backend/` root:
  `chat_logic.py` → `backend/ui/` (removing the root-level `./chat_logic.py`
  compatibility shim), `llm_providers.py` + `language_policy.py` →
  `backend/llm/`, and `authorization.py` + `request_context.py` +
  `config_context.py` → `backend/runtime/`. Pure relocation — no logic, route,
  or signature changes; all backend imports and tests updated to the new paths
  (942 passed / 16 skipped on the base install). `backend/DEVELOPMENT.md`
  architecture tree and the `README.md` project structure now match the tree;
  stale path references in `LLM_PROVIDERS.md` and `SMALL_FIXES.md` corrected.
  `config_loader.py` and `document_processor.py` were intentionally left at
  root (no assigned target / open merge question) and split out as new item
  B6, so B3's "root contains only packages" clause completes with B6.

- **[2026-07-11] C1 — stale tracked graph data removed + embedding scripts
  parameterised (PR #223).** Untracked and deleted `backend/graph.json` (1.4 MB
  stale runtime data) and the root `graph.json` stub, adding gitignore rules so
  neither is re-tracked. `scripts/generate_embeddings.py` and
  `scripts/migrate_embeddings.py` now take a `--graph-file` argument defaulting
  to `data/active/graph.json` instead of hardcoding `backend/graph.json`
  (migrate derives its pickle path next to the graph file). Removed the stale
  `backend/graph.json` data-location line from the `backend/DEVELOPMENT.md`
  architecture tree; the `GRAPH_FILE` env-var table already documents the real
  `data/active/graph.json` location. `git ls-files '*graph.json'` now shows only
  seed data (`config/stat-metadata/graph.json`).

- **[2026-07-11] A3 step 1 — session-id lookup throttle + same-origin CORS
  default (PR #222).** Added a per-source token bucket
  (`SessionManager.check_lookup_rate`, 60 burst + 2/s, keyed on client address)
  on the auth-bypassed `GET /api/sessions/{id}` and SSE stream-handshake
  endpoints, returning `429` when exhausted — the check runs before the
  never-returning SSE generator starts. Changed the CORS default from `*` to no
  cross-origin access (unset `CORS_ALLOWED_ORIGINS` → same-origin only). Updated
  `MULTI_USER_SESSIONS_DESIGN.md` §3.9. Step 2 (high-entropy per-session stream
  token, row 10) remains open.

- **[2026-07-11] A1 — full test suite in CI (PR #221).** Split the single
  `test` job into three attributable jobs: `backend-tests` (`pytest backend/ -q`
  on the base ML-free install, mock embedding path — no network download),
  `frontend-tests` (`npm run test:unit` across web/widget/canvas), and
  `gateway-tests` (the OAuth gateway suite, isolated with its own pins). The
  build/publish job now depends on all three. Updated CLAUDE.md's Test section
  and added a CI section to `backend/DEVELOPMENT.md`. Frontend job uses
  `npm install` pending a committed lockfile — logged as new item C7.

- **[2026-07-11] A2 — ML deps out of base requirements (PR #220).** Moved
  `torch` + `sentence-transformers` and the pytorch extra index into
  `requirements-ml.txt` (un-deprecated); replaced the sole `scikit-learn` use
  (cosine similarity) with numpy so semantic search runs on the base install;
  `search()` degrades gracefully without the ML stack. Base `pip install` now
  succeeds on PyPI-only networks; full backend suite passes without ML.
  Dockerfile installs the extras by default via an `INSTALL_ML` build arg.
