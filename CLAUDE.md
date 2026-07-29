# Claude Code — Project Guidelines

This file configures how Claude Code should work in this repository.
It is safe to commit and expose publicly — it contains no secrets.

---

## MCP-first planning

Planning for this product — goals, initiatives, activities, decisions,
dependencies, priorities — lives in the **Corp graph**, which is the system of
record. This repository holds code, ADRs, technical contracts and code-near
documentation. It must not grow a parallel plan with its own status.

Before planning anything:

1. Connect to the Corp graph over MCP (`https://mcp.corp.communityoverview.tjoo.se`;
   dev: `https://mcp-preview.corp.communityoverview.tjoo.se`) and read the current
   goals, initiatives, activities, decisions and dependencies.
2. Then look at this repo, its issues and open PRs.
3. Create and update planning **in the graph**, not in repo files.
4. Do the code work here through a branch and a PR.
5. Write the PR, commit SHA, implementation evidence and verification results back
   onto the relevant graph nodes.
6. Keep ADRs and technical contracts here, and link them from the graph.
7. Never maintain planning status in both places.
8. Do not write secrets or sensitive working material into the graph; use
   correlation IDs on writes and avoid event loops.
9. If MCP is unavailable, do not start a standalone plan here — record the
   interruption and resume when the graph is reachable.

The Corp graph is private; this repository stays public. Only general technical
enablement is described in commits and PR bodies here.

---

## Feature Planning & Routing

When Jakob wants to think through a feature, discuss it naturally — he does not
need to name files, systems, or workflows explicitly.

**Routing rules (apply silently):**

- **Belongs in this repo** — feature clearly fits the public open-source core →
  plan and implement here.
- **Broader scope** — feature spans more than this repo → keep changes in this
  repo limited to what genuinely belongs here; do not make assumptions about
  systems or context beyond the public codebase.
- **Follow-up execution slices for this repo** → scope work to what belongs here;
  do not require Jakob to cite external file paths or document names.

All repo-managed content (docs, comments, PR bodies) remains in English.

---

## Branch & Environment Strategy

```
main        ← production deployments (pilots)
  ↑
preview     ← staging deployments; periodic merge from dev for integration testing
  ↑
dev         ← integration branch; all features land here first
  ↑
feature/*   ← one branch per task; PR always targets dev
claude/*    ← branches created by Claude agents (same rules apply)
```

**Do not merge into `main` or `preview` on your own initiative.** Those merges are
deployment actions, done infrequently by the project owner:
- `dev → preview` when a batch of features is ready for deployment testing
- `preview → main` when preview has been validated and a prod release is approved

**Explicit exception:** Claude may perform a `dev → preview` or `preview → main`
merge when — and only when — the project owner explicitly asks for it in that turn
(e.g. "merge with preview", "merge dev into preview", "promote preview to main",
"release to main", or an equivalent Swedish phrasing like "merga till preview").
See "Explicit merge requests" below for how to carry this out. Absent such an
explicit instruction, never initiate or propose these merges yourself.

Docker images are built and published **only** when `preview` or `main` receives a
push — never on feature branch pushes or PRs. Merging to `preview` or `main` is a
deployment action, not a code review action — treat it as one even when explicitly
authorised.

### Hotfix path

If a critical bug must bypass the dev/preview queue:
1. Branch off `main`: `git checkout -b hotfix/<description> origin/main`
2. Fix and test.
3. Open a PR against `main` with explicit justification.
4. After merge, immediately backmerge into `dev`: `git checkout dev && git merge main`.

This is the only legitimate exception to the "PRs target dev" rule.

### Explicit merge requests (`preview` / `main`)

Claude may merge into `preview` or `main` **only** when the project owner explicitly
requests that merge in the current turn. A phrase such as "merge with preview",
"merge dev into preview", "promote preview to main", "release to main", or an
equivalent Swedish phrasing ("merga dev till preview", "släpp till main") is the
trigger. A general instruction like "ship it" or "merge the PR" does **not** count —
if the target branch is ambiguous, ask before acting.

When explicitly authorised:
1. Confirm the source and target are what was asked for (`dev → preview` or
   `preview → main`) and that the source branch is green in CI.
2. Perform the merge that was requested — never substitute a different source or
   target, and never chain an additional merge that was not asked for (e.g. do not
   also push `preview → main` when only `dev → preview` was requested).
3. Report exactly what was merged and remind the owner that this triggers a Docker
   build/deploy for that environment.

If any part of the request is unclear, stop and ask rather than assume.

---

## What Claude Must Never Do

- Open a PR against `main` or `preview` (except hotfixes, or an explicitly
  requested merge that you choose to route through a PR — see above).
- Push directly to `dev`.
- Merge into `preview` or `main` on your own initiative — do so only when the
  project owner explicitly asks for that specific merge (see "Explicit merge
  requests" above).
- Add features beyond what the task requires. If you discover a related bug or
  improvement, log it in `SMALL_FIXES.md` (see below) and stop — never fix it
  in the same branch.
- Fix pre-existing bugs in the active branch. Pre-existing means: the problem
  existed before you started working, or is in code you did not change. Log it
  in `SMALL_FIXES.md` with file, line, and context, then continue.
- Skip the review loop for non-trivial changes.
- Merge PRs against `main` or `preview` unless explicitly asked to in that turn —
  those gates belong to the project owner (see "Explicit merge requests" above).
- Stage debug artifacts: `print()` statements, `breakpoint()`, `pdb.set_trace()`,
  hardcoded test credentials, or generated data files left in source paths.
- Stage files that are not source code and larger than ~50 KB without explicit
  justification. If an unexpected binary or data file appears in `git status`, pause
  and confirm it belongs in the repo before staging it.
- Commit with `git add -A` or `git add .` — always stage files by name.

---

## Standard Development Workflow

Follow this process for every feature or bug fix, regardless of size.

**Default session ownership (end-to-end).** Unless the task says otherwise, a
Claude session owns the whole cycle for the chosen task: solve it, open a PR
targeting `dev`, run the review loop with a subagent, update any documentation
the change affects, and — once every review point is resolved and the
definition-of-done checklist in step 10 passes — merge the PR to `dev` itself.
Do not stop at "PR opened" and wait for the owner to merge; merging a clean,
green `dev` PR is part of the job. The only branches Claude never merges on its
own initiative are `preview` and `main` (see the branch strategy above). If the
tooling can't delete the feature branch after merge, leave it — the owner
removes it manually.

### 1. Orient — start from a fresh dev

Always start with a current copy of `dev`:

```bash
git fetch origin dev
git checkout -b claude/<short-description> origin/dev
```

### 2. Explore

- Read the relevant files before touching anything.
- For broad questions, spawn an `Explore` subagent rather than grepping blindly.
- Identify which files need changing and what the impact radius is.

### 3. Plan

- For non-trivial changes, write out the plan in the conversation before coding.
- Flag any architectural decisions or trade-offs to the user before proceeding.
- "Explore" is about reading existing code. "Plan" is about writing down what you
  will change and why — before touching any file.

### 4. Implement

- Make the smallest change that solves the problem. No speculative abstractions.
- Prefer editing existing files over creating new ones.
- Do not add comments that describe *what* the code does — only *why* when the
  reason is non-obvious.

### 5. Test

CI runs the full suite in three attributable jobs (see `.github/workflows/ci.yml`):
the complete backend pytest suite, the frontend vitest workspaces, and the OAuth
gateway tests. Two further **non-required** jobs (`python-lint`, `frontend-lint`)
run the ruff / eslint / prettier gates — see the Lint & format tooling note under
Code Style. Reproduce what CI validates locally:

```bash
pytest backend/ -q          # backend-tests job (base/ML-free install)
npm run test:unit           # frontend-tests job (all workspaces)
pytest services/mcp_oauth_gateway/test_oauth_flow.py -q   # gateway-tests job
```

The backend job installs only the base requirements (no ML stack); semantic search
and chat fall back to their mock paths, so no model is downloaded in CI.

During active iteration, run just the tests for the module you changed:

```bash
pytest backend/<module>/tests/ -q
```

Run the full backend suite before opening the PR:

```bash
pytest backend/ -q
```

Tests live alongside the code they cover:

```
backend/core/tests/
backend/service/tests/
backend/federation/tests/
backend/agents/tests/
backend/api_host/tests/
backend/ui/tests/
backend/tests/
```

If a test breaks that you did not touch, investigate before continuing — it may
indicate an unexpected side-effect.

**Async tests:** use `pytest-asyncio` with `@pytest.mark.asyncio`. See existing
tests in `backend/ui/tests/` and `backend/api_host/tests/` for the pattern.

**Tests that need LLM calls:** mock the LLM provider rather than using real API
keys. See `backend/ui/tests/` for existing mock patterns.

### 5b. Capture Pre-existing Issues

During Explore, Test, and Review, you will sometimes discover problems that are
clearly pre-existing: failing tests you didn't touch, inconsistencies between
parallel code paths, dead code, stale TODO comments, or obvious bugs outside
your change radius.

**Do not fix them now.** Instead, append an entry to `SMALL_FIXES.md` in the
repo root:

```markdown
### [YYYY-MM-DD] Short description
- **File(s):** `path/to/file.py:line`
- **Context:** Discovered during <branch-name>
- **Issue:** What the problem is and why it matters
- **Effort:** XS | S | M
```

Use XS for a single-line fix, S for up to ~30 lines / one file, M for
multi-file or logic-heavy changes. Commit the updated `SMALL_FIXES.md` as part
of your final commit on the branch (or as a standalone `chore:` commit).

At end of session, sweep any notes from conversation context into `SMALL_FIXES.md`
before closing. The goal: nothing is lost between sessions.

### 6. Commit

- Write descriptive commit messages focused on *why*, not *what*.
- Use the conventional prefix: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
- Prefer one commit per logical change. Do not squash before pushing — the project
  owner squash-merges if desired.
- Stage only the files relevant to the change, by name:
  ```bash
  git add backend/core/storage.py backend/core/tests/test_storage.py
  ```

### 7. Push & Open PR

```bash
git push -u origin claude/<short-description>
```

Open a PR targeting `dev` using `gh pr create` (token at `~/.gh_token`, load with `GH_TOKEN=$(cat ~/.gh_token)`). In remote environments without the `gh` CLI, use the GitHub MCP tools instead.

The PR body follows `.github/pull_request_template.md`:
- **Summary** — what changed and why.
- **What was not changed (scope)** — explicit boundaries.
- **Test plan** — which tests cover this, and how to verify manually.
- **Screenshots affected** — see the Documentation section.

### 8. Review Loop

After the PR is open, spawn a subagent with the full diff from dev:

```
Agent(
  prompt="""Review the diff below from branch <name> in /path/to/repo.
            Full diff vs dev: run `git diff origin/dev...HEAD` in the repo.

            Look for: correctness bugs, edge cases, test gaps, consistency
            between related files (e.g. local vs. federation paths doing
            the same thing differently), dead code, pre-filter/scorer
            mismatches.

            Context from previous round (if any): <summarise what was
            fixed since the last review>.

            Report file:line references. Flag only real issues."""
)
```

Address every finding that is a real bug or a meaningful gap. Then spawn another
review subagent (briefing it on what changed between rounds). Repeat until the
review comes back with no actionable findings.

Typical issues caught in review:
- Additive scoring / ranking bugs that only appear with combined signals
- Inconsistency between parallel implementations (e.g. local vs. federation paths)
- Dead code introduced by refactoring
- Pre-filter/scorer mismatches (filter excludes nodes the scorer would reward)
- Secrets or debug artifacts accidentally staged

### 9. Resolve branch divergence

If `dev` has advanced while your branch is in the review loop, bring the branch
up to date before marking it ready:

```bash
git fetch origin dev
git merge origin/dev      # merge, not rebase — avoid rewriting shared history
pytest backend/ -q        # re-run full suite after merge
git push
```

Resolve conflicts by understanding both sides — never blindly accept all-incoming
or all-outgoing changes.

### 10. Merge to dev — definition of done

When the review loop is clean, merge the PR to `dev` autonomously:

```bash
gh pr merge <number> \
  --repo jakobengdahl/CommunityOverview \
  --squash \
  --subject "<feat|fix|...>: <title> (#<number>)" \
  --delete-branch
```

If `gh` is unavailable in the active environment, use the equivalent GitHub
API/MCP merge path instead. If the merge tool cannot delete the branch, leave it
for the owner to remove manually.

#### Waiting for CI before merge — never poll with a scheduled self-wakeup

CI takes a few minutes. **Do not** bridge that wait with a scheduled self-wakeup
(`send_later` / `ScheduleWakeup` / a self-firing trigger). Each such wake fires as
a *new* turn, which spawns a redundant approval/confirmation for Jakob — the exact
"double approval" to avoid.

Prefer GitHub-native auto-merge whenever the base branch is protected and has
required checks:

1. **Protected base branch with required checks (the intended path).**
   - Mark the PR ready (if draft), then enable auto-merge (squash).
   - Subscribe to PR activity if that tooling exists in the current environment,
     then end the turn.
   - GitHub merges the moment CI is green. On a CI failure or review comment,
     fix the branch and re-enable auto-merge if needed.

2. **Base branch without effective required checks.**
   - Auto-merge may be unavailable or pointless because the PR is already
     immediately mergeable.
   - In that case, check CI once; if it is already green, merge now and report.
   - If CI is still pending, do **not** poll it with a scheduled wakeup.

Current repo state (verified 2026-07-14):
- repo setting **Allow auto-merge** is enabled
- `dev` branch protection is enabled with strict required checks:
  - `Backend tests`
  - `Frontend tests`
  - `Gateway tests`
  - `Python lint (ruff)`
  - `Frontend lint (eslint + prettier)`
- admins are also subject to that protection

Merge only when **all** of the following are true:

- [ ] All tests pass locally (`pytest backend/ -q`)
- [ ] CI is green on the PR (not red, not pending)
- [ ] Review loop is clean (last subagent round raised no actionable findings)
- [ ] Documentation affected by the change is updated in the same PR (see the
      Documentation section for which files map to which changes)
- [ ] No debug artifacts in the diff (`print`, `pdb`, hardcoded credentials)
- [ ] PR body documents what was *not* changed (scope)
- [ ] Dependency changes, if any, follow the rules below

---

## Testing Philosophy

- **Unit tests** live in `*/tests/` next to the code they test.
- **Do not write tests for things that cannot fail** — trust framework guarantees.
- **Do write tests for the exact scenario that motivated a change.** If a bug
  existed, add a regression test that would have caught it.
- **Score / ranking logic**: always add tests that cover cross-tier scenarios
  (can secondary signals beat a stronger primary signal?), not just happy-path order.
- Tests are documentation. The test name should describe the invariant, not the steps.

---

## Small-Fix Sessions

A small-fix session is started by the instruction **"kör small-fix-sessionen"**
(or equivalent). Its only goal is to drain items from `SMALL_FIXES.md`.

### Entry checklist before starting

- Pull latest `dev`.
- Read `SMALL_FIXES.md` in full.
- Confirm no items are already addressed by recent commits (check `git log --oneline origin/dev -20`).

### Batch selection

Group items by locality (same file or module) and combined effort. A good batch:

- Total effort ≤ ~M (several XS/S items, or one M item).
- Items that touch overlapping files go in the **same** batch — one branch,
  one PR, one review loop.
- Items that touch unrelated areas go in **separate** batches → separate
  branches, separate PRs, merged independently.

Pick the highest-value batch first. If uncertain, ask Jakob before starting.

### Execution per batch

Follow the full Standard Development Workflow (steps 1–10), with these additions:

1. **Branch name:** `fix/small-fixes-<YYYY-MM-DD>` or `fix/small-fixes-<topic>`
   if the batch has a clear theme.
2. **After Implement:** re-run the test suite for every file touched. If a new
   test failure appears that is unrelated to your batch, log it in `SMALL_FIXES.md`
   and do not fix it here.
3. **PR body:** list each `SMALL_FIXES.md` entry being resolved. Note items
   explicitly **not** addressed.
4. **Review loop:** spawn the review subagent as described in step 8. Because
   these are small, isolated fixes the loop typically converges in one round —
   but repeat until clean, same as any other PR.
5. **After merge:** remove the resolved entries from `SMALL_FIXES.md`, commit
   the update directly on `dev` via a standalone `chore: update small-fixes backlog`
   commit (no separate branch needed for the file update).
6. If time and context permit, move on to the next batch in the same session.
   Otherwise stop — the backlog is in `SMALL_FIXES.md` for next time.

### What never belongs in a small-fix batch

- New features, even small ones.
- Refactors that change public API or data model.
- Anything that requires a design decision — surface it to Jakob instead.

---

## What to Do When CI Is Red

1. Read the CI failure output before doing anything else.
2. If the failure is in your code: fix it locally, run the failing tests, commit,
   and push. Do not mark the PR ready while CI is red.
3. If the failure appears to be infrastructure (flaky runner, network timeout, unrelated
   broken dependency): note this clearly in a PR comment and ping the project owner.
   Do not mark the PR ready.
4. Never use `--no-verify` or skip test steps to bypass a CI failure.

---

## Dependency Changes

- Runtime dependencies go in `backend/requirements.txt` with a minimum version pin
  and no upper bound (e.g. `httpx>=0.27.0`).
- Development/test-only dependencies go in `backend/requirements-dev.txt`.
- Before adding a new dependency, check whether an equivalent utility already exists
  in the project (`httpx` is already present, `pydantic` is already present, etc.).
- Never add ML/heavy dependencies (torch, sentence-transformers, etc.) to the base
  `requirements.txt` — they belong in `requirements-ml.txt`.

---

## Schema and Config Changes

`config/default/schema_config.json` is the migration surface for this system.
Changes to node types, relationship types, or required field names are **breaking
changes** — existing graph data may fail validation or silently lose meaning.

Before modifying the schema:
- Document in the PR body exactly what changes and what existing data is affected.
- Check whether any existing graph data (e.g. `config/stat-metadata/graph.json`)
  would fail validation after the change.
- If data migration is needed, add a migration script under `scripts/` and document
  it in the PR.

---

## Secrets Handling

- Never hardcode API keys, tokens, passwords, or other credentials in any source file.
- Use environment variables loaded via `python-dotenv` (see `.env.example`).
- If a test needs an LLM response, mock the provider — do not use a real API key.
- If you accidentally commit a secret, tell the project owner immediately so the
  credential can be rotated before the commit is pushed.

---

## Code Style

- Python: standard library style, no decorator-heavy abstractions.
- No comments on obvious code. One-line comments only when the *why* is surprising.
- No half-finished stubs, no `# TODO` left in committed code.
- Security: never introduce raw SQL, shell injection, or unvalidated external data
  into logic paths. Validate at system boundaries only.

### Lint & format tooling

Mechanical gates run in CI (jobs `python-lint` and `frontend-lint`) and locally:

```bash
ruff check backend scripts          # Python lint (replaces flake8-style checks)
ruff format backend scripts         # Python formatter (replaces black)
npm run lint                        # ESLint (flat config, react + react-hooks)
npm run format                      # Prettier (JS/JSX in the workspaces)
```

Config lives in root `pyproject.toml` (ruff), `eslint.config.mjs`, and
`.prettierrc.json`. On `dev` PRs, the lint jobs are part of the branch-protection
required checks (STRUCTURE_REVIEW A4), so a lint failure is a real merge blocker.
`react-hooks/rules-of-hooks` violations are errors — treat them as real bugs.
`services/mcp_oauth_gateway/` is outside the ruff scope.

---

## Documentation

All documentation lives in `docs/` and must be in **English** — including PR bodies,
commit messages, and code comments.

### When to update docs alongside code

- **API changes** (`rest_api.py`, `mcp_tools.py`): update `backend/DEVELOPMENT.md`
  endpoint table immediately. Wrong docs are worse than no docs.
- **New features / UI changes**: update `docs/USER_GUIDE.md`. If the change affects
  a screenshot, note it in the PR body — Jakob captures screenshots separately.
- **New node types, relationship types, or profile changes**: update `docs/PROFILES.md`.
- **Federation config or behaviour changes**: update `docs/FEDERATED_GRAPH_DESIGN.md`
  Implementation Status section.
- **Agent or subscription system changes**: update `docs/EVENT_SUBSCRIPTIONS.md`.

### What never belongs in docs

- Future proposals or TODOs in current-state documents — file them in `SMALL_FIXES.md`
  or discuss in the PR body instead.
- Swedish text in any file under `docs/` or in code comments — English only.

### Screenshot workflow (USER_GUIDE.md)

The guide references images in `docs/images/`. Screenshots are captured by Jakob,
not generated programmatically. When a code change would invalidate an existing
screenshot or require a new one, add a note to the PR body:

> **Screenshots affected:** `docs/images/<filename>.png` — <why it changed>

Do not remove `![alt](images/filename.png)` references from the guide just because
the image file doesn't exist yet; Jakob will add it after deployment.

---

## Internationalisation (i18n)

The UI supports multiple languages. English is the default; Swedish (`sv`) is the
only other language with full coverage today.

### Rules

- **Never hardcode display strings** in React components. Use the `useI18n()` hook
  and look up a key from the JSON files.
- **Always add new keys to both** `frontend/web/src/i18n/en.json` **and**
  `frontend/web/src/i18n/sv.json`. Missing a language file key causes the UI to
  fall back to the key name, not English.
- **`packages/ui-graph-canvas`** has no access to the host app's i18n system.
  All user-visible text in that package must be accepted as props with English
  defaults. Wire new props through `App.jsx` (translating with `t()`) and add the
  corresponding keys to both JSON files.
- The `contextMenuLabels` prop on `GraphCanvas` is the established pattern for this —
  see `GraphCanvas.jsx` and `App.jsx` for the implementation.
- Backend-side strings (error messages, log output) stay in English — they are
  developer-facing, not end-user-facing.

### Adding support for a new language

1. Create `frontend/web/src/i18n/<lang>.json` mirroring the structure of `en.json`.
2. Add `'<lang>'` to `SUPPORTED_LANGUAGES` in `frontend/web/src/i18n/index.jsx`.
3. Add a `menu.language_<lang>` key to both `en.json` and `sv.json` (and the new file).
4. The language selector in `FloatingHeader.jsx` will pick it up automatically.

---

## Key Files

| Path | Purpose |
|---|---|
| `backend/core/storage.py` | Core graph storage, search, CRUD |
| `backend/core/storage_backends.py` | Persistence backend abstraction |
| `backend/federation/manager.py` | Federated graph cache and search |
| `backend/service/service.py` | GraphService orchestration layer |
| `backend/service/rest_api.py` | REST API routes |
| `backend/service/mcp_tools.py` | MCP tool definitions |
| `backend/config/config_loader.py` | Schema and config loading |
| `config/default/schema_config.json` | Node types, relationships, labels (multilingual) — breaking-change surface |
| `config/default/federation_config.json` | Federation topology |
| `.github/workflows/ci.yml` | CI: tests on PRs, Docker build on preview/main push |
| `backend/DEVELOPMENT.md` | Architecture overview and API docs |
| `frontend/web/src/i18n/en.json` | English UI strings (source of truth for keys) |
| `frontend/web/src/i18n/sv.json` | Swedish UI strings (must mirror en.json structure) |
| `frontend/web/src/i18n/index.jsx` | i18n provider, `useI18n()` hook, language detection |
| `packages/ui-graph-canvas/src/components/GraphCanvas.jsx` | Canvas + all context menus (text via props) |
| `docs/USER_GUIDE.md` | End-user guide with screenshot references |
