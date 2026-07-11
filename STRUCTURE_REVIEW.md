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

### B5. Split `packages/ui-graph-canvas/src/components/GraphCanvas.jsx` (1 463)

- **Problem:** canvas rendering, all context menus, remote-position handling
  and annotation logic in one component in the shared package.
- **Proposed change:** extract context menus into their own components (props
  contract already exists via `contextMenuLabels`), and remote-position/
  pending-position logic into a hook (which is also where the open
  `SMALL_FIXES.md` pruning entry for `pendingRemotePositionsRef` lives —
  resolve it in the extraction).
- **Effort:** M

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
  `requests` is *also* in the base requirements (used for document processing).
  The gateway pins exact versions while the backend policy is `>=` minimums.
  The gateway's `python-jose 3.3.0` should be reviewed against its known CVEs
  (CVE-2024-33663/33664) or replaced with `pyjwt`.
- **Proposed change:** standardize on `httpx2` in both components; drop
  `requests` if its uses migrate; align the gateway's pin policy with the repo
  rule; evaluate the `python-jose` → `pyjwt` swap.
- **Effort:** S–M

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

- **Problem:** no `package-lock.json` is tracked for the npm workspaces, so the
  frontend CI job introduced in A1 uses `npm install` and resolves fresh
  transitive versions on every run. This defeats the reproducibility the safety
  net is meant to provide — a green run does not guarantee the next run installs
  the same tree, and it prevents `setup-node`'s npm cache (which keys off a
  lockfile) from working. Discovered while implementing A1 (2026-07-11).
- **Proposed change:** generate and commit a root `package-lock.json`, switch the
  frontend CI job from `npm install` to `npm ci`, and enable `cache: 'npm'` on
  `setup-node`. Verify the committed lockfile installs the same versions the
  suite currently passes on.
- **Effort:** S

---

## Priority 4 — documentation and knowledge structure

Docs are the interface agents (and new humans) load first; drift here has
outsized cost.

### D1. Realign the architecture docs with the tree

- **Problem:** the DEVELOPMENT.md architecture overview omits
  `backend/federation/`, `backend/agents/`, `backend/skills/`,
  `backend/core/events/` and all the flat modules, and still lists
  `backend/graph.json` as the data file. README's project structure omits
  `services/mcp_oauth_gateway`. The two "enablement plan" docs and
  `docs/sprint_documentation/` mix design-proposal and historical material into
  the same namespace as current-state guides with no status marking.
- **Proposed change:** update both structure trees (after B3/C1 land, or as
  part of them); add a `docs/README.md` index that tags every doc as
  **current** / **design (target state)** / **historical**, and move
  `sprint_documentation/` under a clearly historical heading. This lets an
  agent session skip stale material instead of reconciling it.
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
| 5 | C1 stale data removal + script fix | S | — | open |
| 6 | B3 home the flat backend modules | M | A1 | open |
| 7 | B2 decompose server.py | M | A1 | open |
| 8 | B1 decompose App.jsx (slice 1: shared-session hook) | M | — | open |
| 9 | C2 lint gates | M | A1 | open |
| 10 | A3 step 2 (stream token scheme) | M | A3 step 1 | open |
| 11 | B1 remaining slices | M×2 | B1 slice 1 | open |
| 12 | B5 GraphCanvas decomposition | M | — | open |
| 13 | C3 HTTP client + dependency policy | S–M | — | open |
| 14 | C4 Node 18 → 20 build image | XS | — | open |
| 15 | C5 security scanning in CI | S | A1 | open |
| 16 | C6 start-script consolidation | S | — | open |
| 17 | D1 docs realignment + index | S–M | B3, C1 | open |
| 18 | D2 CLAUDE.md truth verification pass | XS | A1, A2, A4 | open |
| 19 | B4 service.py / storage.py split | L | A1, next feature touching them | open |
| 20 | C7 commit root lockfile for reproducible frontend CI | S | A1 | open |

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
