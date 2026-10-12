"""The PreToolUse hook in .claude/hooks/prepush-check.sh.

It blocks a `git push` whose committed Python files fail the pinned ruff, and
any `gh pr create --draft`, for the repo the command actually pushes from. The
cases here are the forms a Claude Code session writes in its Bash tool; exotic
shells (`xargs git push`, subshell-scoped `cd`) are out of scope and fail open,
because CI remains the gate - the hook only saves the red run and the mail.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".claude" / "hooks" / "prepush-check.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="the hook needs git and bash",
)


def run_hook(command, cwd):
    payload = json.dumps({"cwd": str(cwd), "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True, timeout=120
    )


def git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def make_repo(root, ruff_configured=True):
    """A repo with an origin/main to diff against and one commit on a branch."""
    repo = root / "my repo"  # a space in the path, on purpose
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    if ruff_configured:
        (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    (repo / "backend").mkdir()
    (repo / "backend" / "ok.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "-b", "work")
    return repo


def commit(repo, relpath, content, message="change"):
    (repo / relpath).write_text(content)
    git(repo, "add", relpath)
    git(repo, "commit", "-q", "-m", message)


ruff_available = (
    subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], capture_output=True
    ).returncode
    == 0
    or shutil.which("ruff") is not None
)


class TestDraftPullRequests:
    @pytest.mark.parametrize(
        "command",
        [
            "gh pr create --title t --draft",
            "gh pr create -t x -d -b y",
            "gh pr create --draft=true",
            "gh -R owner/repo pr create --title t --draft",
            'bash -c "gh pr create --draft"',
        ],
    )
    def test_a_draft_pr_is_blocked(self, tmp_path, command):
        result = run_hook(command, tmp_path)
        assert result.returncode == 2, result.stderr
        assert "ready for review" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            'gh pr create --title t --body "never use --draft here"',
            'gh pr create --title t --body "-d"',
            "gh pr create --title t",
            "gh pr view 1",
        ],
    )
    def test_a_ready_pr_or_a_quoted_draft_is_not_blocked(self, tmp_path, command):
        assert run_hook(command, tmp_path).returncode == 0


class TestUnrelatedInput:
    @pytest.mark.parametrize("payload", ["", "not json", "[]", '{"cwd": 1}'])
    def test_unparsable_input_lets_the_call_through(self, payload):
        result = subprocess.run(
            ["bash", str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0

    def test_a_command_that_is_not_a_push_passes(self, tmp_path):
        assert run_hook("ls -la && echo '--draft'", tmp_path).returncode == 0

    def test_a_push_from_a_repo_without_formatter_config_passes(self, tmp_path):
        repo = make_repo(tmp_path, ruff_configured=False)
        commit(repo, "backend/bad.py", "def  f( x ):\n  return   x\n")
        assert run_hook("git push", repo).returncode == 0


@pytest.mark.skipif(not ruff_available, reason="ruff is not installed")
class TestPushFormatting:
    def test_a_committed_unformatted_file_blocks_the_push(self, tmp_path):
        repo = make_repo(tmp_path)
        commit(repo, "backend/bad.py", "def  f( x ):\n  return   x\n")
        result = run_hook("git push -u origin work", repo)
        assert result.returncode == 2
        assert "backend/bad.py would be reformatted" in result.stderr

    def test_a_clean_head_passes_even_with_a_bad_uncommitted_file(self, tmp_path):
        repo = make_repo(tmp_path)
        commit(repo, "backend/bad.py", "def  f( x ):\n  return   x\n", "bad")
        commit(repo, "backend/bad.py", "def f(x):\n    return x\n", "fixed")
        # A tracked file made bad in the working tree, staged and unstaged, and
        # an untracked bad file: none of them is in the push.
        (repo / "backend" / "ok.py").write_text("x  =  1\n")
        git(repo, "add", "backend/ok.py")
        (repo / "backend" / "bad.py").write_text("def  g( ):  pass\n")
        (repo / "backend" / "scratch.py").write_text("y  =  2\n")
        assert run_hook("git push", repo).returncode == 0

    @pytest.mark.parametrize(
        "template",
        [
            "cd {repo} && git push",
            "cd /tmp && cd {repo} && git push",
            "cd {repo}\ngit push",
            "git -C {repo} push",
            "git --no-pager -C {repo} push origin work",
            "GIT_SSH_COMMAND=ssh git -C {repo} push",
            "if true; then git -C {repo} push; fi",
            "git -C {repo} status # check\ngit -C {repo} push",
            "# it's fine\ngit -C {repo} push",
            "git -C {repo} push # don't forget",
            "bash -c 'cd {repo} && git push'",
            "cd {repo} && git -c core.pager=cat push",
        ],
    )
    def test_the_pushed_repo_is_found_through_the_command(self, tmp_path, template):
        repo = make_repo(tmp_path)
        commit(repo, "backend/bad.py", "def  f( x ):\n  return   x\n")
        command = template.format(repo=f'"{repo}"')
        result = run_hook(command, tmp_path)  # cwd is NOT the repo
        assert result.returncode == 2, (command, result.stderr)
        # Blocked for the right reason, not because the hook found no ruff.
        assert "backend/bad.py would be reformatted" in result.stderr, result.stderr

    def test_a_deletion_or_tag_push_carries_no_commits_to_check(self, tmp_path):
        repo = make_repo(tmp_path)
        commit(repo, "backend/bad.py", "def  f( x ):\n  return   x\n")
        assert run_hook("git push origin --delete old", repo).returncode == 0
        assert run_hook("git push --tags", repo).returncode == 0

    def test_a_separator_inside_a_comment_is_not_a_push(self, tmp_path):
        repo = make_repo(tmp_path)
        commit(repo, "backend/bad.py", "def  f( x ):\n  return   x\n")
        assert run_hook("git status # build && git push", repo).returncode == 0

    def test_a_push_run_from_the_cwd_with_a_space_in_its_path_is_checked(
        self, tmp_path
    ):
        repo = make_repo(tmp_path)
        commit(repo, "backend/bad.py", "def  f( x ):\n  return   x\n")
        assert run_hook("git push", repo).returncode == 2
