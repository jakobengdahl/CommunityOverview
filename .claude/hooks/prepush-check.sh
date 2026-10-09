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
# One Python pass reads the hook's stdin and answers two questions with shell
# quoting respected: what is the command about to do (nothing we care about,
# a draft PR, or a push), and which directory is that push run from. It
# prints the verdict on line 1 and the directory on line 2, so a path with
# spaces survives.
verdict="$(python3 -c '
import json, os, re, shlex, sys

SEPARATORS = set("&|;()<>")
WRAPPERS = {"then", "do", "else", "elif", "time", "command", "sudo", "env", "exec", "nice", "nohup"}
GIT_OPTS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
GH_OPTS_WITH_VALUE = {"-t", "--title", "-b", "--body", "-B", "--base", "-H", "--head", "-F", "--body-file",
                      "-r", "--reviewer", "-a", "--assignee", "-l", "--label", "-m", "--milestone",
                      "-p", "--project", "-T", "--template", "-R", "--repo"}

def tokenize(cmd):
    lex = shlex.shlex(cmd.replace("\r", "\n").replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""          # "a#b" is a word in bash, and a comment must not eat the next line
    return list(lex)

def segments(tokens):
    out, seg = [], []
    for t in tokens:
        if t and set(t) <= SEPARATORS:   # "&&", ";", "&&(" ... all separate commands
            if seg: out.append(seg)
            seg = []
        else:
            seg.append(t)
    if seg: out.append(seg)
    return out

def resolve(path, base):
    path = os.path.expandvars(os.path.expanduser(path))
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))

def strip_prefix(seg):
    """Drop VAR=value assignments and wrapper words before the command."""
    i = 0
    while i < len(seg) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]) or seg[i] in WRAPPERS):
        i += 1
    return seg[i:]

def git_push_target(seg, here):
    rest, base = seg[1:], here
    while rest and rest[0].startswith("-"):
        opt = rest[0]
        if opt in GIT_OPTS_WITH_VALUE and len(rest) > 1:
            if opt == "-C": base = resolve(rest[1], base)
            rest = rest[2:]
        elif opt.startswith("-C") and len(opt) > 2:
            base, rest = resolve(opt[2:], base), rest[1:]
        else:
            rest = rest[1:]
    return base if rest and rest[0] == "push" else None

def gh_draft(seg):
    args, i = seg[1:], 0
    while i < len(args) and args[i] != "pr":          # skip global options such as -R owner/repo
        i += 2 if args[i] in GH_OPTS_WITH_VALUE else 1
    if args[i:i + 2] != ["pr", "create"]: return False
    i += 2
    while i < len(args):
        a = args[i]
        if a in GH_OPTS_WITH_VALUE: i += 2; continue     # "--body -d": the -d is a value
        if a in ("--draft", "-d") or a.startswith("--draft="): return True
        i += 1
    return False

def analyse(cmd, here, depth=0):
    """Return (verdict, target). Walks the command carrying the cwd through every cd."""
    try:
        segs = segments(tokenize(cmd))
    except ValueError:
        return "none", ""
    verdict, target = "none", ""
    for seg in segs:
        seg = strip_prefix(seg)
        if not seg: continue
        name = os.path.basename(seg[0])
        if name == "cd":
            args = [a for a in seg[1:] if not a.startswith("-")]
            here = resolve(args[0], here) if args else os.path.expanduser("~")
            continue
        if name in ("bash", "sh", "zsh") and "-c" in seg and depth < 2:
            inner = seg[seg.index("-c") + 1:seg.index("-c") + 2]
            if inner:
                v, t = analyse(inner[0], here, depth + 1)
                if v == "draft": return v, t
                if v == "push": verdict, target = v, t
            continue
        if name == "eval" and depth < 2:
            v, t = analyse(" ".join(seg[1:]), here, depth + 1)
            if v == "draft": return v, t
            if v == "push": verdict, target = v, t
            continue
        if name == "gh" and gh_draft(seg):
            return "draft", ""
        if name == "git":
            t = git_push_target(seg, here)
            if t is not None: verdict, target = "push", t
    return verdict, target

try:
    d = json.load(sys.stdin)
    cmd = d.get("tool_input", {}).get("command", "")
    cwd = d.get("cwd", "") or os.getcwd()
    if not isinstance(cmd, str) or not isinstance(cwd, str) or not cmd.strip():
        raise ValueError
except Exception:
    print("none"); sys.exit(0)
v, t = analyse(cmd, cwd)
print(v); print(t)
' <<<"$input" 2>/dev/null)" || exit 0
action="${verdict%%$'\n'*}"
target="${verdict#*$'\n'}"
case "$action" in
  draft)
    echo "Blocked: open the PR ready for review, not as a draft. On a draft the required checks are red by design (the suites skip and the gates fail that skip), which hides real failures and mails the owner on every push. Run the local checks first, then open it ready." >&2
    exit 2 ;;
  push) ;;
  *) exit 0 ;;
esac
[ -n "$target" ] && [ -d "$target" ] || exit 0
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
