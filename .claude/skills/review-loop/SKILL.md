---
name: review-loop
description: Review a change before merge with fresh-context subagent reviewers — pick the tier, brief reviewers with the change's guarantees, label findings, and stop at the first round with no production-defect. Use for every non-trivial PR in this repo, after the local checks pass and before enabling auto-merge.
---

# Review loop

A loop of fresh-context reviewers asked to "find problems" has no fixed point:
a reviewer briefed to find something always can. So this loop has a tier, a
round cap, and one blocking label.

## 1. Write the guarantees

Before the first round, write G1…Gn: what the change must hold true, one
testable line each. Brief every reviewer, every round, with them, and tell it to
flag only correctness gaps against them — not style, not hypothetical future
edits. Every reviewer gets the full diff (`git diff origin/main...HEAD`) and,
from round 2, a summary of what changed since the last round. Review the fixes,
not only the original change. When a change spans this repo and another one,
review the pair: an invariant can live between them.

## 2. Labels — exactly one per finding

| Label | Question | Blocks the merge? |
|---|---|---|
| `production-defect` | Is the changed production artifact wrong? | Yes |
| `test-durability` | Would the tests miss a future regression? | No |
| `unfalsifiable` | Could a different harness behave differently? | No |

The production artifact is what the change ships: the module, script or config
it edits. Edited text (docs, `CLAUDE.md`, skills, any normative prose) is always
a production artifact too: a wrong, contradicted or unactionable rule or fact
is a `production-defect`, even alongside code. Where the change has no
executable code, its tests and fixtures are the production artifact: an
assertion that is wrong, or that passes against the break it claims to catch,
is a `production-defect`. Where it does have executable code, a test gap is
`test-durability`. A round whose findings come back unlabelled is not complete:
ask for the labels (never guess them); the round still counts toward the cap.

## 3. Tiers — pick the highest that applies

| Tier | Applies to | Reviewers per round | Max rounds |
|---|---|---|---|
| Text & tests | docs-only, test-only, fixture-only, draining a residue node | one correctness reviewer | 2 |
| Production code | any executable change not in the next tier | finder, then a separate classification step | 4 |
| Security-sensitive | authorization, SSRF and fetchers, the gateway and authentication, secrets handling | finder + classification + mutation reviewer | 10 |

- **Correctness reviewer / finder:** reports every issue against G1…Gn with
  file:line, a confidence and a severity. In the first tier it labels its own
  findings; in the other two a separate classification step (its own subagent,
  briefed with G1…Gn and the definitions above) labels each finding. Typical
  catches: ranking bugs that only show with combined signals, parallel paths
  (local vs. federation) that disagree, pre-filter/scorer mismatches, dead code
  left by a refactor, staged secrets or debug artifacts.
- **Mutation reviewer** (security tier only): runs with `isolation: "worktree"`,
  never in the correctness reviewer's checkout. It edits the changed executable
  code (module, script or config — never prose, never tests or fixtures) to
  violate a named guarantee, reports which edits the suite lets through, labels
  each survivor `test-durability` or `unfalsifiable` with what would close it,
  and reverts every edit. A mutation counts only if it violates a named
  guarantee; harness identity is out of scope.

Reviewer models: correctness reviewer / finder `claude-opus-5-5` at medium
effort; mutation reviewer and classification/triage `claude-sonnet-5-5` or
`claude-haiku-5-5`.

## 4. Rounds and termination

- Fix a round's accepted `production-defect` findings as one batch, validate
  locally once, push once, then run the next round.
- **The loop ends at the first round with no `production-defect`.**
- Residue: every surviving `test-durability` and `unfalsifiable` finding goes
  into **one** `small-fix`-tagged Task node for the whole residue, with a
  `PART_OF` parent, and the merge proceeds. If the planning MCP is unreachable,
  record the residue in the PR body under its own heading and flag it to the
  owner. Exception: on a first-tier PR that drains a residue node, non-blocking
  findings are not logged again.

## 5. At the cap

If the cap is reached and the last round's `production-defect` findings are all
fixed, CI is green, and the change is not in the security tier: merge, with a
note in the PR body saying what the last round found and that its fix was not
re-reviewed. Otherwise stop and report to the owner. Never merge with an unfixed
`production-defect`.

## 6. No subagents, no review

If subagents are unavailable, the work is unreviewed: say so, leave the PR
open, and do not merge.
