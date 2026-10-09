#!/usr/bin/env bash
# PreToolUse hook (matcher: Bash). Blocks two things a session otherwise does
# by habit and that CLAUDE.md can only advise against:
#   1. `git push` while the changed Python or JS files fail the formatters CI
#      requires (ruff format, prettier) - 12 of the 28 real CI failures last
#      month were format drift, each one a red run and a mail.
#   2. `gh pr create --draft` - the draft gate fails the required checks by
#      design, so a draft PR is a red PR until it is marked ready.
# Exit 2 blocks the call and feeds stderr back to the model; exit 0 lets it run.
set -u
input="$(cat)"
cmd="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null || true)"
[ -n "$cmd" ] || exit 0

if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+create\b.*--draft'; then
  echo "Blocked: open the PR ready for review, not as a draft. On a draft the required checks are red by design (the suites skip and the gates fail that skip), which hides real failures and mails the owner on every push. Run the local checks first, then open it ready." >&2
  exit 2
fi

printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])git[[:space:]]+push\b' || exit 0

root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$root" || exit 0
base="$(git merge-base HEAD origin/main 2>/dev/null)" || exit 0
changed="$(git diff --name-only "$base" HEAD; git diff --name-only HEAD)"
py="$(printf '%s\n' "$changed" | grep -E '^(backend|scripts)/.*\.py$' | sort -u | while read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done)"
js="$(printf '%s\n' "$changed" | grep -E '^(frontend|packages)/.*\.(js|jsx|css)$' | sort -u | while read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done)"

fail=0
if [ -n "$py" ]; then
  # The pinned ruff (requirements-dev.txt, same line as CI) is the one whose
  # verdict matters; a newer ruff on PATH formats docstring fences differently.
  if python3 -m ruff --version >/dev/null 2>&1; then
    RUFF="python3 -m ruff"
  elif command -v ruff >/dev/null 2>&1; then
    RUFF="ruff"
  else
    RUFF=""
  fi
  if [ -n "$RUFF" ]; then
    # shellcheck disable=SC2086
    $RUFF format --check $py >&2 || fail=1
    # shellcheck disable=SC2086
    $RUFF check $py >&2 || fail=1
  else
    echo "ruff is not installed; CI's 'Python lint (ruff)' check will run it. Install it (pip install -r backend/requirements-dev.txt) and re-run." >&2
    fail=1
  fi
fi
if [ -n "$js" ]; then
  if [ -x node_modules/.bin/prettier ]; then
    # shellcheck disable=SC2086
    node_modules/.bin/prettier --check $js >&2 || fail=1
  else
    echo "prettier is not installed; CI's 'Frontend lint (eslint + prettier)' check will run it. Run npm ci and re-run." >&2
    fail=1
  fi
fi
if [ "$fail" -ne 0 ]; then
  echo "Blocked: fix the formatting above (ruff format / npm run format) before pushing. A push that CI rejects costs a run and a mail; pushing once after the local checks pass is the rule." >&2
  exit 2
fi
exit 0
