# Claude Code — Project Guidelines

This file configures how Claude Code should work in this repository.
It is safe to commit and expose publicly — it contains no secrets.

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

**Never open a PR directly against `main` or `preview`.** Those merges are done
manually and infrequently by the project owner:
- `dev → preview` when a batch of features is ready for deployment testing
- `preview → main` when preview has been validated and a prod release is approved

---

## Standard Development Workflow

Follow this process for every feature or bug fix, regardless of size.

### 1. Orient

- Fetch and check out the latest `dev` branch as your starting point.
- Create a feature branch from `dev`:
  ```
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

### 4. Implement

- Make the smallest change that solves the problem. No speculative abstractions.
- Prefer editing existing files over creating new ones.
- Do not add comments that describe *what* the code does — only *why* when the reason is non-obvious.

### 5. Test

Run the backend test suite after every change:

```bash
# Backend unit and integration tests
pytest backend/ -q

# Focused — only files you changed (faster feedback loop)
pytest backend/core/tests/test_storage.py backend/service/tests/test_federated_search.py -q
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

All tests must pass before opening a PR. If a test breaks that you did not touch,
investigate before continuing — it may indicate an unexpected side-effect.

### 6. Commit

- Write descriptive commit messages focused on *why*, not *what*.
- Stage only the files relevant to the change. Never use `git add -A` blindly.
- Use the conventional prefix that matches the work: `feat:`, `fix:`, `refactor:`,
  `test:`, `docs:`, `chore:`.

### 7. Push & Open PR

```bash
git push -u origin claude/<short-description>
```

Open a **draft** PR targeting `dev`. Include in the PR body:
- A short summary of what changed and why
- What was explicitly *not* changed (scope)
- Test plan (which tests cover this, how to verify manually)

### 8. Review Loop

After the PR is open, spawn a subagent to review the diff:

```
Agent(
  subagent_type="general-purpose",
  prompt="Review the diff on branch X in /path/to/repo.
          Look for: correctness bugs, edge cases, test gaps, consistency
          between related files. Report file:line references. Flag only
          real issues."
)
```

Address every finding that is a real bug or a meaningful gap. Then spawn another
review subagent. Repeat until the review comes back clean (no actionable findings).

Typical issues caught in review:
- Additive scoring / ranking bugs that only appear with combined signals
- Inconsistency between parallel implementations (e.g. local vs. federation paths)
- Dead code introduced by refactoring
- Pre-filter/scorer mismatches (filter excludes nodes the scorer would reward)

### 9. Mark PR Ready

Once the review loop is clean and CI is green, remove the draft status. The project
owner will merge to `dev` when appropriate (manually, or via automation they control).

---

## Testing Philosophy

- **Unit tests** live in `*/tests/` next to the code they test.
- **Do not write tests for things that cannot fail** — trust framework guarantees.
- **Do write tests for the exact scenario that motivated a change.** If a bug existed,
  add a regression test that would have caught it.
- **Score / ranking logic**: always add tests that cover cross-tier scenarios
  (can secondary signals beat a stronger primary signal?), not just happy-path order.
- Tests are documentation. The test name should describe the invariant, not the steps.

---

## Code Style

- Python: standard library style, no decorator-heavy abstractions.
- No comments on obvious code. One-line comments only when the *why* is surprising.
- No half-finished stubs, no `# TODO` left in committed code.
- Security: never introduce raw SQL, shell injection, or unvalidated external data
  into logic paths. Validate at system boundaries only.

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
| `backend/config_loader.py` | Schema and config loading |
| `config/default/schema_config.json` | Node types, relationships, labels (multilingual) |
| `config/default/federation_config.json` | Federation topology |
| `backend/DEVELOPMENT.md` | Architecture overview and API docs |

---

## What Claude Should Never Do

- Open a PR against `main` or `preview` — these are controlled by the project owner.
- Push directly to `dev`, `preview`, or `main`.
- Add features beyond what the task requires.
- Skip the review loop for non-trivial changes.
- Merge PRs — that decision belongs to the project owner.
