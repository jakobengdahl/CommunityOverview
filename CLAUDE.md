# Claude Code — Project Guidelines

How Claude Code works in this public repository. All repo content — code
comments, docs, commits, PR bodies — is in English.

## Planning lives in the graph, not here

Goals, initiatives, tasks, decisions, dependencies and priorities live in a private
planning graph reached over MCP; the owner configures its endpoint in the MCP
client, never in this repo. This repo holds code, ADRs, technical contracts and
code-near docs, and never a parallel plan with its own status. If no planning MCP
server is configured in the session, skip this section.

- Read the graph's goals, initiatives, tasks and decisions before planning; then
  this repo's issues and open PRs. Plan in the graph, attaching every item in the
  write that creates it: a Task gets a `PART_OF` parent (the larger work item or
  body of work it serves — create that node if missing); a capability is attached
  to the value it delivers. Small fixes and review residue attach to the graph's
  standing maintenance bucket (ask the graph for it). If nothing fits, the item is
  a feature request: name the capability it delivers and create that too.
- Write PR, commit SHA and verification evidence back onto the graph nodes; link
  ADRs and contracts kept here from the graph.
- No secrets in the graph; use correlation IDs on writes, avoid event loops. If
  it is unreachable, record that and resume later — never plan standalone here.

## Public / private boundary

Describe general technical enablement only — never name private deployments,
hosts, tenants, customers or commercial plans in files, commits or PR bodies.
Re-read every PR body and commit message for hostnames, tenant names, tokens and
internal URLs before publishing, above all when the change removes one: describe
it ("a personal lab hostname"), never quote it. A feature spanning more than this
repo changes only what belongs in the open-source core here; assume nothing about
systems beyond the public codebase, and never ask the owner to name files or
documents to route a feature.

## Branches and environments

```
prod        ← deploys to the prod environment
preview     ← deploys to the preview environment; merged from main periodically
main        ← integration branch; every PR targets it; deploys nowhere
claude/*, feature/*  ← one branch per task
```

- Environments are **preview** and **prod** ("main" as a deploy target is a naming
  bug); Docker images are built only on pushes to them and on `v*` tags.
- Any merge into `preview` or `prod` is the owner's deployment action; never
  propose one. Merge only when the owner explicitly asks for that merge in the
  current turn ("merge main into preview", "släpp till prod"); "ship it" or "merge
  the PR" does not count — if the target is ambiguous, ask. Then confirm the source
  is green, merge exactly what was asked (never chain a second merge), and report
  what merged and that it builds and deploys that environment.
- **Hotfix path**, only when a critical bug must bypass the main/preview queue: PR
  `hotfix/<desc>` (off `origin/prod`) into `prod` with the justification — the only
  PR into `prod` you open unasked; the owner merges it; then merge `prod` into `main`.

## Development workflow

A session owns its task end to end — solve, PR, review loop, docs, merge to `main`.

1. **Start fresh:** `git fetch origin main && git checkout -b claude/<desc> origin/main`.
2. **Read before changing** (an `Explore` subagent for broad questions). For a
   non-trivial change, write the plan first and flag architectural trade-offs.
3. **Smallest correct change.** No speculative abstractions; prefer editing
   existing files; comment only *why*, when it is not obvious.
4. **Regression test** for the exact scenario that motivated the change.
5. **Run the checks CI runs**, all before the PR is opened:
   ```bash
   pytest backend/ -q                  # pytest backend/<module>/tests/ -q while iterating
   npm run test:unit
   (cd services/mcp_oauth_gateway && pytest test_oauth_flow.py test_upstream_auth.py test_lockfiles.py -q)
   python3 -m ruff check backend scripts && python3 -m ruff format --check backend scripts
   npm run lint && npm run format:check
   ```
   A test you did not touch breaking is a signal: investigate before continuing.
6. **Commit** per logical change, staged by name, with a conventional prefix
   (`feat:` `fix:` `refactor:` `test:` `docs:` `chore:`) and a message about *why*.
   Do not squash before pushing; the PR is squash-merged.
7. **Push once per review round**, not once per fix: `ci.yml` cancels the
   in-flight run on every push. Fix a round's findings, verify locally, push once.
8. **Open the PR ready for review, never as a draft**, against `main` (`gh` or the
   GitHub MCP tools), filling every section of `.github/pull_request_template.md`.
   A hook denies draft PR creation.
9. **Review loop:** run `/review-loop` (`.claude/skills/review-loop/SKILL.md`).
10. **Divergence:** `git merge origin/main` (never rebase a pushed branch),
    resolve conflicts by understanding both sides, re-run the suite.
11. **Merge:** when `/review-loop` allows it, enable auto-merge (squash); GitHub
    merges on green. No scheduled self-wakeups to wait for CI.

Required checks on `main`: the split worker+gate checks `Backend tests`,
`Frontend tests`, `Gateway tests`, `Python lint (ruff)`, plus the unconditional
`Frontend lint (eslint + prettier)`. On a draft touching service code the suites
skip and their gates fail on purpose, mailing the owner on every push; never
"fix" that in the workflow — the PR belongs in ready-for-review.

**Done:** local checks pass; CI green on the merging head; review loop ended per
its skill, residue logged; docs updated in the same PR; scope stated in the PR body.

**CI red:** read the output first. Your code → fix, verify, push. Infrastructure
(flaky runner, network, unrelated dependency) → PR comment for the owner; no merge.

## Fix, don't log

A problem you find while working that is inside the change radius and takes under
about 30 minutes is fixed in the same PR and named in the PR body (review findings
follow `/review-loop` instead). Anything else becomes one `small-fix`-tagged Task
node (attached as above): **name**; **file:line**; **context** (branch); **issue**
(what, and why it matters); **effort** XS (one line) | S (≤ ~30 lines, one file) |
M. Without a planning MCP, list it in the PR body and flag it to the owner. Before
a session ends, sweep unlogged notes into the graph. Never add to `SMALL_FIXES.md`
(historical archive) or put proposals or TODOs into current-state docs.

## Never

- Push directly to `main`, or PR or merge into `preview`/`prod` outside the rules above.
- Add features beyond the task. `git add -A` / `git add .` — stage by name; no
  non-source file over ~50 KB without saying why.
- Leave `print()`, `breakpoint()`, `pdb`, test credentials, generated data in
  source paths, half-finished stubs or `# TODO` in a commit.
- Skip, weaken or quarantine a test to get green; use `--no-verify`.
- Write an identifier you did not resolve from the thing itself this session (image
  digest, checksum, SHA, package hash, URL with an id). If it cannot be resolved,
  use the mutable reference with a comment saying why it is unpinned.
- Commit a secret: credentials come from env vars (`python-dotenv`,
  `.env.example`). If one slips in, tell the owner at once so it is rotated.

## Testing

- Tests live next to their code in `*/tests/`; a test's name states the invariant.
- Don't test what cannot fail. Ranking/scoring logic gets cross-tier tests (can a
  secondary signal beat a stronger primary one?), not just happy-path order.
- Async tests use `pytest-asyncio`; LLM calls are mocked, never a real key. CI
  installs base requirements only, so ML paths run their mocks.

## Code style and lint

- Python: standard-library style, no decorator-heavy abstractions. Never raw SQL,
  shell injection or unvalidated external data in logic; validate at boundaries.
- Config: `pyproject.toml` (ruff; `services/mcp_oauth_gateway/` is outside its
  scope), `eslint.config.mjs`, `.prettierrc.json`. `rules-of-hooks` errors are bugs.
- ruff is pinned below 0.16 in `backend/requirements-dev.txt`; run that install as
  `python3 -m ruff` (a newer ruff reformats Markdown fences CI never asks for).
- A pre-push hook blocks `git push` while committed Python or JS files fail
  `ruff format`/`ruff check`/`prettier --check` (`npm run format` fixes JS).

## Documentation

| Change | Update |
|---|---|
| API (`rest_api.py`, `mcp_tools.py`) | `backend/DEVELOPMENT.md` endpoint table |
| New feature or UI change | `docs/USER_GUIDE.md` |
| Node/relationship types, profiles | `docs/PROFILES.md` |
| Federation config or behaviour | `docs/FEDERATED_GRAPH_DESIGN.md` Implementation Status |
| Agents or event subscriptions | `docs/EVENT_SUBSCRIPTIONS.md` |
| i18n mechanics, adding a language | `docs/I18N.md` |

Wrong docs are worse than none. Screenshots are captured by the owner: when a
change invalidates or needs one, add `**Screenshots affected:**
docs/images/<file>.png — <why>` to the PR body; keep `![alt](images/…)`
references even when the image does not exist yet.

## i18n

- No hardcoded display strings in React: use `useI18n()` and a key.
- Add every key to both `frontend/web/src/i18n/en.json` and `sv.json`.
- `packages/ui-graph-canvas` has no i18n: its text comes in as props with English
  defaults, translated in `App.jsx` (pattern: `contextMenuLabels` on `GraphCanvas`).
- Backend strings (errors, logs) stay English. Adding a language: `docs/I18N.md`.

## Schema, dependencies

- `config/default/schema_config.json` is a breaking-change surface. Changing node
  types, relationship types or required fields: state the affected data in the PR
  body, check existing graph data (e.g. `config/stat-metadata/graph.json`) still
  validates, and add a migration script under `scripts/` if data must move.
- `config/<profile>/graph.json` is example data: run
  `scripts/strip_profile_runtime_nodes.py` over an instance export before
  committing it (see `docs/PROFILES.md`).
- Runtime deps in `backend/requirements.txt` with a minimum pin and no upper bound;
  dev/test deps in `requirements-dev.txt`; ML/heavy deps only in
  `requirements-ml.txt`. Reuse what exists (`httpx`, `pydantic`) before adding.

## Key files

| Path | Purpose |
|---|---|
| `backend/core/storage.py` / `storage_backends.py` | Graph storage, search, CRUD / persistence backends |
| `backend/federation/manager.py` | Federated graph cache and search |
| `backend/service/` `service.py` / `rest_api.py` / `mcp_tools.py` | Orchestration / REST routes / MCP tools |
| `backend/config/config_loader.py` | Schema and config loading |
| `config/default/` `schema_config.json` / `federation_config.json` | Schema (breaking-change surface) / federation topology |
| `.github/workflows/ci.yml` | CI: tests and lint on PRs; images on `preview`/`prod`/`v*` push |
| `.claude/settings.json`, `.claude/hooks/` | Session hooks (pre-push format check, no draft PRs) |
| `frontend/web/src/i18n/` | `en.json` (key source of truth), `sv.json`, `index.jsx` (`useI18n()`) |
| `packages/ui-graph-canvas/src/components/GraphCanvas.jsx` | Canvas and context menus (text via props) |
| `docs/USER_GUIDE.md` | End-user guide with screenshot references |

Skills: `/review-loop` (review before merge), `/small-fix-session` (drain
`small-fix` tasks; "kör small-fix-sessionen"). Docs index: `docs/README.md`.
