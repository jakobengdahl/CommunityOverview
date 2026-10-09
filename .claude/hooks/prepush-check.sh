#!/usr/bin/env bash
# PreToolUse hook (matcher: Bash). Blocks two things a session otherwise does
# by habit and that CLAUDE.md can only advise against:
#   1. `git push` while the pushed Python or JS files fail the formatters CI
#      requires (ruff format, prettier) - 12 of the 28 real CI failures last
#      month were format drift, each one a red run and a mail.
#   2. `gh pr create --draft` - the draft gate fails the required checks by
#      design, so a draft PR is a red PR until it is marked ready.
# The repo being pushed is read from the command (a leading `cd` or `git -C`)
# or the session's cwd, and only what that repo itself configures is checked:
# ruff where its pyproject.toml has [tool.ruff], prettier where its
# package.json has a format:check script. Other repos in the same workspace
# pass through untouched.
# Exit 2 blocks the call and feeds stderr back to the model; exit 0 lets it run.
set -u
input="$(cat)"
read -r cmd cwd < <(printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(); sys.exit(0)
cmd = d.get("tool_input", {}).get("command", "") or ""
cwd = d.get("cwd", "") or ""
# one line each, newlines collapsed, so the shell read below gets both
print(cmd.replace("\n", " ").replace("\r", " ").replace("\t", " "), cwd)
' 2>/dev/null | python3 -c '
import sys
line = sys.stdin.read().rstrip("\n")
# the command may contain spaces, so split on the LAST space: cwd has none
if " " in line:
    cmd, cwd = line.rsplit(" ", 1)
else:
    cmd, cwd = line, ""
print(cmd.replace(" ", "\x01") + " " + cwd)
') || exit 0
cmd="${cmd//$'\x01'/ }"
[ -n "$cmd" ] || exit 0

# Quoted text is not an argument: `--body "never use --draft here"` must not
# match, so strip quoted strings before looking at the arguments.
args_only="$(printf '%s' "$cmd" | sed -E "s/\"[^\"]*\"//g; s/'[^']*'//g")"

if printf '%s' "$args_only" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+create([[:space:]]|$)' \
   && printf '%s' "$args_only" | grep -Eq '(^|[[:space:]])--draft([[:space:]=]|$)'; then
  echo "Blocked: open the PR ready for review, not as a draft. On a draft the required checks are red by design (the suites skip and the gates fail that skip), which hides real failures and mails the owner on every push. Run the local checks first, then open it ready." >&2
  exit 2
fi

printf '%s' "$args_only" | grep -Eq '(^|[;&|[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push([[:space:]]|$)' || exit 0

# Which repo is being pushed: `git -C <path> push`, a leading `cd <path> &&`,
# else the session's cwd.
target=""
if [[ "$args_only" =~ git[[:space:]]+-C[[:space:]]+([^[:space:]]+)[[:space:]]+push ]]; then
  target="${BASH_REMATCH[1]}"
elif [[ "$cmd" =~ ^[[:space:]]*cd[[:space:]]+([^[:space:];&|]+)[[:space:]]*(\&\&|\;) ]]; then
  target="${BASH_REMATCH[1]}"
fi
target="${target%\"}"; target="${target#\"}"; target="${target%\'}"; target="${target#\'}"
case "$target" in
  "") target="$cwd" ;;
  /*) ;;
  *) target="${cwd:-.}/$target" ;;
esac
[ -d "$target" ] || exit 0
root="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$root" || exit 0

check_py=0; check_js=0
[ -f pyproject.toml ] && grep -q '^\[tool\.ruff\]' pyproject.toml && check_py=1
[ -f package.json ] && grep -q '"format:check"' package.json && check_js=1
[ "$check_py" = 1 ] || [ "$check_js" = 1 ] || exit 0

base="$(git merge-base HEAD origin/main 2>/dev/null)" || exit 0
# Only what the push carries: committed files, read at HEAD, so an unrelated
# uncommitted edit neither blocks the push nor hides a bad commit.
pushed="$(git diff --name-only --diff-filter=AMR "$base" HEAD)"
fail=0

if [ "$check_py" = 1 ]; then
  if python3 -m ruff --version >/dev/null 2>&1; then
    RUFF="python3 -m ruff"   # the pinned one, same line as CI
  elif command -v ruff >/dev/null 2>&1; then
    RUFF="ruff"
  else
    RUFF=""
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ -z "$RUFF" ]; then
      echo "ruff is not installed; CI's 'Python lint (ruff)' check will run it. Install it (pip install -r backend/requirements-dev.txt) and re-run." >&2
      fail=1; break
    fi
    git show "HEAD:$f" | $RUFF format --check --stdin-filename "$f" - >/dev/null 2>&1 \
      || { echo "ruff format: $f would be reformatted" >&2; fail=1; }
    out="$(git show "HEAD:$f" | $RUFF check --stdin-filename "$f" - 2>&1)" \
      || { printf '%s\n' "$out" >&2; fail=1; }
  done < <(printf '%s\n' "$pushed" | grep -E '^(backend|scripts)/.*\.py$')
fi

if [ "$check_js" = 1 ]; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ ! -x node_modules/.bin/prettier ]; then
      echo "prettier is not installed; CI's 'Frontend lint (eslint + prettier)' check will run it. Run npm ci and re-run." >&2
      fail=1; break
    fi
    git show "HEAD:$f" | node_modules/.bin/prettier --check --stdin-filepath "$f" >/dev/null 2>&1 \
      || { echo "prettier: $f would be reformatted" >&2; fail=1; }
  done < <(printf '%s\n' "$pushed" | grep -E '^(frontend|packages)/.*\.(js|jsx|css)$')
fi

if [ "$fail" -ne 0 ]; then
  echo "Blocked: fix the formatting above (ruff format / npm run format), commit, then push. A push that CI rejects costs a run and a mail; pushing once after the local checks pass is the rule." >&2
  exit 2
fi
exit 0
