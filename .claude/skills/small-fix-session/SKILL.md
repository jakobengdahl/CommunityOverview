---
name: small-fix-session
description: Drain small-fix-tagged Task nodes from the planning graph in locality batches, one branch and PR per batch. Use when the owner says "kör small-fix-sessionen" or otherwise asks for a small-fix session.
---

# Small-fix session

Started by **"kör small-fix-sessionen"** (or equivalent). The only goal is to
resolve open `small-fix`-tagged Task nodes in the planning graph.

## Entry

1. `git fetch origin main`.
2. Read the open `small-fix`-tagged Task nodes in full via the planning MCP. If
   no planning MCP is reachable, tell the owner and stop — never fall back to
   `SMALL_FIXES.md`, which is a historical archive.
3. Drop items already fixed by recent commits (`git log --oneline origin/main -20`).

## Batches

- Group by locality (same file or module); total effort per batch ≤ ~M
  (several XS/S items, or one M).
- Items touching overlapping files share a batch: one branch, one PR, one
  review loop. Unrelated areas get separate batches and separate PRs.
- Take the highest-value batch first; if the choice is unclear, ask the owner.
- Never in a batch: new features, however small; refactors that change a public
  API or the data model; anything needing a design decision — surface those to
  the owner instead.

## Per batch

Follow the development workflow in `CLAUDE.md`, with:

1. Branch `fix/small-fixes-<YYYY-MM-DD>`, or `fix/small-fixes-<topic>` when the
   batch has a clear theme.
2. After implementing, re-run the tests for every touched file. A new failure
   unrelated to the batch follows `CLAUDE.md`'s "Fix, don't log" rule.
3. PR body: list each resolved node (id and name) and the items explicitly not
   addressed.
4. Review with `/review-loop`, at the tier the batch's content calls for: a
   batch that changes executable code is production code (or security-sensitive),
   even though it came from the residue — a dead-code removal is wrong exactly
   when the code was not dead.
5. After merge, mark the resolved nodes done in the graph, recording the PR and
   commit on each.

Continue with the next batch while time and context allow; the rest of the
backlog stays in the graph for next time.
