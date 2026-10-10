#!/usr/bin/env bash
# PreToolUse hook (matcher: mcp__github__create_pull_request). Denies creating
# a draft PR; see prepush-check.sh for why a draft is a red PR in this repo.
set -u
input="$(cat)"
printf '%s' "$input" | python3 -c '
import json, sys
try:
    inp = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if inp.get("tool_input", {}).get("draft") is True:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "Open the PR ready for review (draft: false). On a draft the required "
            "checks are red by design and every push mails the owner; run the local "
            "checks first, then open it ready."),
    }}))
'
