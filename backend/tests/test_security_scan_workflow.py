"""Pins that the Security Scan workflow reports what its scanners found.

Two layers once hid every finding. `continue-on-error` kept a hit from failing
the workflow, and underneath it each scanner step piped its report through
`tee -a "$GITHUB_STEP_SUMMARY"` with no pipefail, so the step took tee's exit
status and reported success on its own account. Removing `continue-on-error`
alone would therefore still leave a green run over a real finding.

The step bodies are shell, so they are extracted from the parsed workflow and
EXECUTED against stub scanners, under the same `bash -e` GitHub uses for a
`run:` step with no `shell:`. The set of tee'd steps is discovered from the
workflow rather than listed here, so a new scanner step cannot ship the same
masking unnoticed.
"""

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security-scan.yml"

SCANNERS = ("pip-audit", "npm", "bandit")
# Stubbed too, so every `run:` step - the installs included - can be executed.
STUBBED = SCANNERS + ("pip",)
# The only real tools on a step's PATH. Anything else - git, curl, rm - is not
# found, so a step that starts using one fails here until it is stubbed or
# listed, instead of running for real inside the test process.
REAL_TOOLS = ("tee",)

# `|tee`, `| tee` and `|& tee` alike; a spelling the discovery missed would
# escape every exit-status check below.
TEE = re.compile(r"\|&?\s*tee\b")

# Bash reads these at startup. SHELLOPTS and BASH_ENV can switch pipefail on
# without a `shell:` key - and under pipefail `-e` kills the bandit step at the
# pipeline, before it closes its summary fence. BASHOPTS carries only `shopt`
# options, not pipefail, but it too changes the shell the steps run under.
SHELL_STARTUP_VARS = ("SHELLOPTS", "BASHOPTS", "BASH_ENV")

# The reporting-first stage is over: every scanner in this workflow blocks, so
# nothing here may carry `continue-on-error`. Re-introducing one - at workflow,
# job or step level - has to add it to this set deliberately, which is what
# test_exactly_the_expected_steps_are_reporting_only compares against.
REPORTING_ONLY_STEPS: set = set()

# A step invokes an audit when a line of its body starts with the scanner, so
# `pip install pip-audit` is not one and a step that stops teeing still is.
AUDIT_INVOCATION = re.compile(r"^\s*(?:pip-audit|npm\s+audit)\b.*$", re.M)

# Any line that could run an audit, however spelled: `python -m pip_audit`,
# `npx audit-ci`, `npm --prefix . audit`, `FOO=1 npm audit`. Every npm and npx
# line counts too: `npm config set omit=peer` narrows a later audit, and npm
# accepts abbreviations such as `npm aud`. So does EVERY pip line: `pip config
# set global.no-deps true` narrows a later audit, and an option may sit before
# the subcommand (`pip --isolated config set`, `python -m pip -v config`), which
# no `pip <subcommand>` pattern can enumerate. So the pinned pip lines below are
# the allowlist and every other pip spelling - `pip3.11`, `python3 -m pip` - is
# audit-relevant by default.
AUDIT_MENTION = re.compile(
    r"\baudit\b|pip[-_]audit|\bnpm\b|\bnpx\b|\bpip[0-9.]*\b|-m\s*pip", re.I
)
# The exact pip lines each job may run, by job. A second install, a `pip config`
# or a `pip download` has to be added here deliberately.
PINNED_PIP_LINES = {
    "pip-audit": ["pip install pip-audit"],
    "bandit": ["pip install bandit"],
}
# A literal summary heading: no expansion or substitution inside the quotes.
SUMMARY_HEADING = re.compile(r'^echo "## [^"$`\\]*" >> "\$GITHUB_STEP_SUMMARY"$')

# Configuration that narrows an audit without touching its command line: npm
# reads `npm_config_*` from the environment and the project `.npmrc`, pip-audit
# reads `PIP_AUDIT_*` and pip's own `PIP_*`.
AUDIT_CONFIG_VAR = re.compile(r"npm_config_|\bPIP_", re.I)
NPMRC = REPO_ROOT / ".npmrc"
# An allowlist: npm's ini parser takes `omit[]=peer`, `"audit-level"=critical`,
# scoped registries and bare keys, too many spellings for a denylist to cover.
NPMRC_HARMLESS_KEYS = {
    "engine-strict",
    "fund",
    "loglevel",
    "save-exact",
    "save-prefix",
    "update-notifier",
}

# The exact audit command lines, per job. Scope is what makes an audit blocking
# mean anything: a swapped `-r` file, `--no-deps`, `--audit-level=critical` or a
# single `--workspace` each keep the job green over advisories it no longer sees.
AUDIT_COMMANDS = {
    "pip-audit": [
        'pip-audit -r backend/requirements.txt -f markdown 2>&1 | tee -a "$GITHUB_STEP_SUMMARY"',
        'pip-audit -r backend/requirements-dev.txt -f markdown 2>&1 | tee -a "$GITHUB_STEP_SUMMARY"',
        'pip-audit -r services/mcp_oauth_gateway/requirements.txt -f markdown 2>&1 | tee -a "$GITHUB_STEP_SUMMARY"',
    ],
    "npm-audit": ['npm audit --omit=dev 2>&1 | tee -a "$GITHUB_STEP_SUMMARY"'],
}

# Actions the workflow may use. A third-party action can export SHELLOPTS or
# BASH_ENV through GITHUB_ENV where no `run:` body shows it.
ALLOWED_ACTIONS = {
    "actions/checkout",
    "actions/setup-python",
    "actions/setup-node",
    "gitleaks/gitleaks-action",
}

# The setup actions' inputs in the audit jobs, exactly. setup-node's
# `registry-url` writes an .npmrc and exports NPM_CONFIG_USERCONFIG through
# GITHUB_ENV, where no `run:` body shows it.
AUDIT_JOB_ACTION_INPUTS = {
    "pip-audit": [
        ("actions/checkout", {}),
        ("actions/setup-python", {"python-version": "3.11", "cache": "pip"}),
    ],
    "npm-audit": [
        ("actions/checkout", {}),
        ("actions/setup-node", {"node-version": "20"}),
    ],
}

# Job control (`set -m`, `set -mE`, `set -o monitor`) puts each background job
# in its own process group, out of reach of the group drain in _run_step.
JOB_CONTROL = re.compile(r"\bset\b[^;&|\n]*\s(?:-[a-zA-Z]*m|-o\s+monitor\b)")
# `command -p` searches bash's default PATH and `hash -p` binds a name to any
# file, both reaching real tools past the stubs. The group names which one
# matched, so the failure below does not report the wrong builtin.
COMMAND_P = re.compile(r"\b(command|hash)\b[^;&|\n]*\s-[a-zA-Z]*p")
# An absolute path as a command word runs the real tool whatever PATH holds, so
# `/usr/bin/npm audit` never meets the stubs. Only the command word counts: the
# audits name relative paths as arguments, and the summary headings name paths in
# prose.
ASSIGNMENT_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# An assignment to PATH puts the real tools back within reach of the very next
# command, with no absolute path anywhere for the check above to see.
PATH_ASSIGNMENT = re.compile(r"^PATH\+?=")
# Builtins whose arguments are assignments rather than a command.
ASSIGNMENT_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})
# Tokens after which a new command begins.
COMMAND_OPERATORS = frozenset({";", ";;", "&", "&&", "|", "|&", "||", "(", ")"})
# Words that stand in front of the real command: shell keywords, and the
# builtins and wrappers that take a command as their argument. `then /bin/cat`
# and `command /bin/cat` reach the real tool exactly as a bare `/bin/cat` does,
# so the scan has to look past them rather than stop at the first word.
COMMAND_PREFIXES = frozenset(
    {
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "for",
        "in",
        "select",
        "{",
        "}",
        "!",
        "time",
        "coproc",
        "env",
        "exec",
        "eval",
        "nohup",
        "nice",
        "sudo",
        "xargs",
        "command",
        "builtin",
        "source",
        ".",
    }
)


def _strip_comment(line):
    """``line`` without its bash comment: an unquoted `#` that BEGINS a word.

    shlex's own `commenters` cuts a token at `#` anywhere inside a word, which
    bash does not, and that difference was a hole rather than a nuisance: it made
    `pip install pip-audit#x; pip config set global.no-deps true` normalise to
    exactly the pinned install, so the allowlist passed a line bash runs as two
    commands, the second narrowing every later audit in the job. `main` caught
    that line; normalising without this would not.
    """
    quote = None
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _shell_tokens(line):
    r"""One line of a `run:` body, tokenised the way bash reads it.

    Resolves all THREE of bash's quoting mechanisms - `'`, `"` and backslash -
    so the checks below never see raw text. Quoting is the obvious way past a
    check that matches characters, and each spelling below is a real instruction
    bash obeys: `set -\m` turns on job control, `p''ip config set` runs pip, and
    `'/usr/bin/npm'` is the absolute path it looks like.
    """
    lexer = shlex.shlex(_strip_comment(line), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # Comments are handled above, bash's way. Left to shlex, `#` would truncate a
    # token mid-word and hide the rest of the line from every check below.
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        # A line bash cannot parse would not run either, so failing is right.
        # Returning no tokens instead would silently disarm every check below.
        raise AssertionError(f"{line!r} does not tokenise as shell: {exc}") from exc


def _normalised(line):
    """``line`` with quoting resolved and spacing collapsed, for comparison.

    Both a workflow line and the command it is pinned against go through this,
    so they are compared as what bash would run rather than as how it was typed.
    """
    return " ".join(_shell_tokens(line))


def _shell_words(body):
    """Every token of every line of ``body``."""
    for line in body.splitlines():
        yield from _shell_tokens(line)


def _assignments(body):
    """Every assignment ``body`` would actually perform.

    Assignment position only: a leading `VAR=x` on a command, a bare `VAR=x`
    statement, or one handed to `export` and friends. A `VAR=x` that is an
    ARGUMENT assigns nothing - `echo "PATH=/usr/bin"` prints a string - so
    reading every token that merely looks like one would cry wolf on it.
    """
    for line in body.splitlines():
        expect_command = True
        exporting = False
        for token in _shell_tokens(line):
            if token in COMMAND_OPERATORS:
                expect_command, exporting = True, False
            elif (expect_command or exporting) and ASSIGNMENT_PREFIX.match(token):
                yield token
            elif expect_command and token in ASSIGNMENT_BUILTINS:
                expect_command, exporting = False, True
            elif expect_command and token in COMMAND_PREFIXES:
                continue  # `env PATH=x cmd`, `then PATH=x cmd`: still assigning
            elif expect_command:
                expect_command = False


def _command_words(body):
    """Every word of ``body`` that is in a command position.

    A line starts one command and each operator starts another; an assignment
    prefix or a keyword is passed over rather than mistaken for the command.
    """
    for line in body.splitlines():
        expect_command = True
        for token in _shell_tokens(line):
            if token in COMMAND_OPERATORS:
                expect_command = True
            elif not expect_command:
                continue
            elif ASSIGNMENT_PREFIX.match(token) or token in COMMAND_PREFIXES:
                continue
            else:
                yield token
                expect_command = False


# The only file a `run:` body may write to. Only `N>&M` / `>&-` is an fd dup;
# `N>file`, `&>file` and `>&file` all write to a file.
REDIRECT = re.compile(r">>?\|?\s*(&?)\s*([^\s;|&]+)")
FD_DUP = re.compile(r"^[0-9]*-?$")
TEE_OPERANDS = re.compile(r"\btee\b([^|;&\n]*)")
STEP_SUMMARY = '"$GITHUB_STEP_SUMMARY"'

# Files the runner reads back after a step: they change later steps' env, PATH
# (a no-op `pip-audit` shim) or outputs.
RUNNER_COMMAND_FILES = ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STATE")


def _write_targets(body):
    targets = [t for dup, t in REDIRECT.findall(body) if not (dup and FD_DUP.match(t))]
    for operands in TEE_OPERANDS.findall(body):
        targets += [o for o in operands.split() if not o.startswith("-")]
    return targets


def _run_steps():
    return [
        pytest.param(step, id=f"{job_id}:{step['name']}")
        for job_id, job in _workflow()["jobs"].items()
        for step in job.get("steps", [])
        if "run" in step
    ]


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _teed_steps():
    steps = []
    for job_id, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            if TEE.search(step.get("run", "")):
                steps.append(pytest.param(step, id=f"{job_id}:{step['name']}"))
    return steps


def test_every_scanner_report_step_is_discovered():
    # pip-audit x3, npm audit, bandit. A drop here means a step stopped teeing
    # (fine) or the discovery broke (not fine) - either way, look.
    assert len(_teed_steps()) == 5


def _wait_for_process_group(pgid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return False


def _run_step(step, tmp_path, scanner_exit, timeout=30):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    # Each stub records the environment it was started with: configuration a
    # static check cannot see - a quote-split name, `declare -x` - reaches here.
    # Raw, not `export -p`, which omits names that are not shell identifiers
    # such as `npm_config_audit-level`.
    env_capture = tmp_path / "env"
    env_capture.mkdir()
    for name in STUBBED:
        stub = stub_dir / name
        stub.write_text(
            f"#!/bin/sh\n/bin/cat /proc/$$/environ > '{env_capture}/{name}.'$$\n"
            f"echo 'stub {name} report'\nexit {scanner_exit}\n"
        )
        stub.chmod(0o755)
    for name in REAL_TOOLS:
        (stub_dir / name).symlink_to(shutil.which(name))
    summary = tmp_path / "summary.md"
    summary.touch()
    # The runner's own layout: HOME/work/_temp is RUNNER_TEMP, holding the
    # command files, and the checkout sits beside it. A step that reaches them
    # through RUNNER_TEMP, HOME or a relative path rather than GITHUB_ENV writes
    # where the check below looks.
    home = tmp_path / "home"
    runner_temp = home / "work" / "_temp"
    commands = runner_temp / "_runner_file_commands"
    commands.mkdir(parents=True)
    command_files = {}
    for var in RUNNER_COMMAND_FILES:
        command_files[var] = commands / f"{var.lower()}_stub"
        command_files[var].touch()
    workdir = home / "work" / "repo" / "repo"
    workdir.mkdir(parents=True)
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    # Nothing of the job running this test may leak in: under CI its GITHUB_*
    # and RUNNER_* variables name that job's real command files, and its own
    # audit configuration would read as the step's.
    inherited = {
        k: v
        for k, v in os.environ.items()
        if k not in SHELL_STARTUP_VARS
        and not k.startswith(("GITHUB_", "RUNNER_"))
        and not AUDIT_CONFIG_VAR.match(k)
    }
    env = dict(
        inherited,
        PATH=str(stub_dir),
        HOME=str(home),
        GITHUB_STEP_SUMMARY=str(summary),
        GITHUB_WORKSPACE=str(workdir),
        RUNNER_TEMP=str(runner_temp),
        **{var: str(path) for var, path in command_files.items()},
    )
    # Its own process group, waited on until empty: a background writer that
    # closed its fds would otherwise land after the check below.
    proc = subprocess.Popen(
        [shutil.which("bash"), "-e", str(script)],
        cwd=workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The whole group, not just the leader: a background child still holds
        # stdout, and the reaping communicate() would wait on it.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    finally:
        drained = _wait_for_process_group(proc.pid, timeout=30)
    assert drained, f"step {step['name']!r} left processes running"
    written = sorted(
        str(f.relative_to(runner_temp))
        for f in runner_temp.rglob("*")
        if f.is_file() and f.stat().st_size
    )
    assert not written, f"step {step['name']!r} wrote runner command files {written}"
    for record in env_capture.iterdir():
        scanner = record.name.split(".")[0]
        if scanner not in ("npm", "pip-audit"):
            continue
        for entry in record.read_bytes().split(b"\0"):
            name = entry.split(b"=", 1)[0].decode(errors="replace")
            assert not AUDIT_CONFIG_VAR.match(name), (
                f"step {step['name']!r} hands {scanner} {name}"
            )
    return proc.returncode, summary.read_text()


@pytest.mark.parametrize("step", _teed_steps())
@pytest.mark.parametrize("scanner_exit", [0, 1, 2])
def test_step_exits_with_the_scanners_status_not_tees(step, tmp_path, scanner_exit):
    returncode, summary = _run_step(step, tmp_path, scanner_exit)
    assert returncode == scanner_exit, (
        f"step {step['name']!r} exited {returncode} for a scanner that exited "
        f"{scanner_exit}; a tee'd scanner step must exit with the scanner's status"
    )
    assert "stub" in summary and "report" in summary


@pytest.mark.parametrize("step", _teed_steps())
def test_code_fences_opened_in_the_summary_are_closed_on_a_finding(step, tmp_path):
    _, summary = _run_step(step, tmp_path, scanner_exit=1)
    assert summary.count("```") % 2 == 0


def test_gitleaks_is_blocking():
    steps = _workflow()["jobs"]["gitleaks"]["steps"]
    scan = [
        s for s in steps if s.get("uses", "").startswith("gitleaks/gitleaks-action")
    ]
    assert len(scan) == 1
    assert not scan[0].get("continue-on-error", False)
    assert not _workflow()["jobs"]["gitleaks"].get("continue-on-error", False)


def _audit_steps(job):
    return [s for s in job["steps"] if AUDIT_INVOCATION.search(s.get("run", ""))]


@pytest.mark.parametrize("job_id", sorted(AUDIT_COMMANDS))
def test_dependency_audits_are_blocking(job_id):
    workflow = _workflow()
    job = workflow["jobs"][job_id]
    assert not job.get("continue-on-error", False)
    audits = _audit_steps(job)
    assert audits, f"no audit step found in job {job_id!r}"
    for step in audits:
        assert not step.get("continue-on-error", False), (
            f"{job_id}:{step['name']} is reporting-only; dependency audits block"
        )
    assert "--ignore-vuln" not in str(job)


@pytest.mark.parametrize("job_id", sorted(AUDIT_COMMANDS))
def test_dependency_audits_cannot_be_skipped(job_id):
    # A skipped job or step reports success, so a condition is a quiet off-switch.
    job = _workflow()["jobs"][job_id]
    assert "if" not in job, f"job {job_id!r} is conditional"
    assert "needs" not in job, f"job {job_id!r} is skipped when what it needs fails"
    for step in job["steps"]:
        assert "if" not in step, f"{job_id}:{step.get('name')} is conditional"


@pytest.mark.parametrize("job_id", sorted(AUDIT_COMMANDS))
def test_dependency_audit_scope_is_pinned(job_id):
    job = _workflow()["jobs"][job_id]
    commands = [
        m.group(0).strip()
        for step in _audit_steps(job)
        for m in AUDIT_INVOCATION.finditer(step["run"])
    ]
    assert sorted(commands) == sorted(AUDIT_COMMANDS[job_id])


def test_every_line_that_could_run_an_audit_is_a_pinned_command():
    # AUDIT_INVOCATION only finds audits spelled the way the pinned ones are.
    # Both sides are normalised, so a line is compared as what bash would run:
    # matching raw text let `p''ip config set global.no-deps true` - a real pip
    # invocation that narrows every later audit in its job - match nothing at all.
    for job_id, job in _workflow()["jobs"].items():
        pinned = {
            _normalised(c)
            for c in AUDIT_COMMANDS.get(job_id, []) + PINNED_PIP_LINES.get(job_id, [])
        }
        for step in job.get("steps", []):
            body = step.get("run", "").replace("\\\n", "")
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                normalised = _normalised(line)
                if not AUDIT_MENTION.search(normalised) or SUMMARY_HEADING.match(line):
                    continue
                assert normalised in pinned, f"{job_id}:{step['name']} runs {line!r}"


def test_audit_mention_sees_every_pip_spelling_an_option_can_hide():
    for line in (
        "pip --isolated config set global.no-deps true",
        "python -m pip -v config set global.no-deps true",
        "python3.11 -m pip config set global.no-deps true",
        "pip3.11 config set global.no-deps true",
        "pip3 download requests",
        "pip install --upgrade pip",
        # No space after -m, so no word boundary before `pip`.
        "python -mpip config set global.no-deps true",
    ):
        assert AUDIT_MENTION.search(line), line


@pytest.mark.parametrize(
    "line",
    [
        "p''ip config set global.no-deps true",
        'p"i"p config set global.no-deps true',
        "pi\\p config set global.no-deps true",
        "n''pm config set omit=peer --location=project",
        'n""pm audit --audit-level=critical',
        "np\\m config set omit=peer",
    ],
)
def test_audit_mention_sees_a_quote_split_or_escaped_invocation(line):
    # Bash runs all of these; only the normalised form shows what they are.
    assert AUDIT_MENTION.search(_normalised(line)), line


def test_audit_mention_leaves_the_non_pip_scanner_lines_alone():
    # The broadened pattern must not swallow the bandit invocation, which is
    # neither an audit nor a pip line and is pinned nowhere.
    for line in (
        "bandit -r backend services -x '*/tests/*,*/test_*.py' -ll",
        "echo '```' >> \"$GITHUB_STEP_SUMMARY\"",
        'status="${PIPESTATUS[0]}"',
        'exit "${PIPESTATUS[0]}"',
    ):
        assert not AUDIT_MENTION.search(line), line


@pytest.mark.parametrize("job_id", sorted(AUDIT_COMMANDS))
def test_dependency_audit_scope_is_not_narrowed_by_configuration(job_id):
    workflow = _workflow()
    job = workflow["jobs"][job_id]
    assert not job.get("env"), f"job {job_id!r} sets env"
    assert not job.get("defaults"), f"job {job_id!r} sets defaults"
    for step in job["steps"]:
        where = f"{job_id}:{step.get('name')}"
        assert not step.get("env"), f"{where} sets env"
        # npm run from a workspace directory audits only that workspace.
        assert "working-directory" not in step, f"{where} sets working-directory"
        body = step.get("run", "")
        assert not AUDIT_CONFIG_VAR.search(body), f"{job_id}:{step['name']}"
    for var in workflow.get("env") or {}:
        assert not AUDIT_CONFIG_VAR.match(var), f"workflow env sets {var}"
    run_defaults = (workflow.get("defaults") or {}).get("run") or {}
    assert "working-directory" not in run_defaults, "workflow sets working-directory"


def _npmrc_keys(text):
    for line in text.removeprefix("\ufeff").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key = line.split("=", 1)[0].strip().strip("\"'")
        yield key.removesuffix("[]").strip().lower()


def test_npmrc_does_not_narrow_the_npm_audit():
    if NPMRC.exists():
        keys = set(_npmrc_keys(NPMRC.read_text()))
        assert keys <= NPMRC_HARMLESS_KEYS, f".npmrc sets {keys - NPMRC_HARMLESS_KEYS}"


def test_npmrc_key_parsing_sees_every_spelling_npm_accepts():
    text = 'omit[]=peer\n"audit-level"=critical\n@x:registry=https://x/\nglobal\n# c\n'
    assert set(_npmrc_keys(text)) == {"omit", "audit-level", "@x:registry", "global"}
    # A byte-order mark saved by an editor is not part of the first key.
    assert set(_npmrc_keys("\ufefffund=false\n")) == {"fund"}


def test_audits_run_nowhere_but_their_own_jobs():
    for job_id, job in _workflow()["jobs"].items():
        if job_id not in AUDIT_COMMANDS:
            assert not _audit_steps(job), f"{job_id!r} runs an unpinned audit"


def _triggers(workflow):
    # PyYAML reads the bare `on:` key as boolean True.
    return workflow.get("on", workflow.get(True))


def test_pull_request_trigger_names_no_retired_branch():
    workflow = _workflow()
    assert "dev" not in _triggers(workflow)["pull_request"]["branches"]


def test_trigger_set_is_pinned():
    triggers = _triggers(_workflow())
    assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
    assert triggers["pull_request"] == {"branches": ["main", "preview"]}
    assert triggers["schedule"], "the weekly re-audit of unchanged deps is gone"


def test_exactly_the_expected_steps_are_reporting_only():
    workflow = _workflow()
    assert not workflow.get("continue-on-error", False)
    reporting_only = set()
    for job_id, job in workflow["jobs"].items():
        assert not job.get("continue-on-error", False), (
            f"job {job_id!r} is reporting-only"
        )
        for step in job.get("steps", []):
            if step.get("continue-on-error", False):
                reporting_only.add((job_id, step.get("name")))
    assert reporting_only == REPORTING_ONLY_STEPS


def test_gitleaks_cannot_be_switched_off_by_a_condition():
    job = _workflow()["jobs"]["gitleaks"]
    assert "if" not in job
    for step in job["steps"]:
        assert "if" not in step, f"gitleaks:{step.get('name')} is conditional"


def test_gitleaks_checks_out_the_full_history():
    # A shallow clone leaves the scheduled scan nothing but the tip commit.
    steps = _workflow()["jobs"]["gitleaks"]["steps"]
    checkout = [s for s in steps if s.get("uses", "").startswith("actions/checkout")]
    assert len(checkout) == 1
    assert checkout[0].get("with", {}).get("fetch-depth") == 0


def test_no_env_switches_on_pipefail_behind_the_default_shell():
    workflow = _workflow()
    scopes = [("workflow", workflow)]
    for job_id, job in workflow["jobs"].items():
        scopes.append((job_id, job))
        for step in job.get("steps", []):
            scopes.append((f"{job_id}:{step.get('name')}", step))
    for where, scope in scopes:
        for var in SHELL_STARTUP_VARS:
            assert var not in (scope.get("env") or {}), f"{where} sets {var}"
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            body = step.get("run", "")
            where = f"{job_id}:{step.get('name')}"
            # A static first line only: shell can reach a file through too many
            # spellings, so the runtime test below executes every step.
            for target in _write_targets(body):
                assert target == STEP_SUMMARY, f"{where} writes to {target}"
            assert "GITHUB_ENV" not in body and "github.env" not in body, where
            # The runner's command files by their on-disk location.
            assert "_runner_file_commands" not in body, where
            assert "/home/runner" not in body, where
            assert not re.search(r"\bGITHUB_STEP_SUMMARY\s*=", body), where


@pytest.mark.parametrize("step", _run_steps())
@pytest.mark.parametrize("scanner_exit", [0, 1])
def test_no_step_writes_runner_command_files_when_executed(
    step, tmp_path, scanner_exit
):
    # _run_step fails on any write under RUNNER_TEMP, however the body names
    # the file. On success the step must also run to completion: one that aborts
    # early - on a tool this harness lacks, say - never reaches a write placed
    # after it. The failing run reaches a write behind `||`.
    returncode, _ = _run_step(step, tmp_path, scanner_exit)
    if scanner_exit == 0:
        assert returncode == 0, f"step {step['name']!r} did not run to completion"


def test_only_known_actions_are_used():
    used = {
        step["uses"].split("@")[0]
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    }
    assert used == ALLOWED_ACTIONS


def test_bandit_is_blocking():
    # Located by its invocation, not its name: a renamed step must still be
    # found, or this passes vacuously the moment someone retitles it.
    workflow = _workflow()
    job = workflow["jobs"]["bandit"]
    assert not job.get("continue-on-error", False)
    assert "if" not in job, "the bandit job is conditional"
    assert "needs" not in job, "the bandit job is skipped when what it needs fails"
    scans = [
        s for s in job["steps"] if re.search(r"^\s*bandit\b", s.get("run", ""), re.M)
    ]
    assert len(scans) == 1, f"expected one bandit invocation, found {len(scans)}"
    step = scans[0]
    assert not step.get("continue-on-error", False), (
        "bandit is reporting-only again; a medium+ finding must fail the workflow"
    )
    assert "if" not in step, "the bandit step is conditional"


def test_no_step_is_marked_temporarily_reporting_only():
    # Parsed YAML drops comments, so read the raw text. A leftover "Temporary:
    # reporting-only" note would outlive the key it described and mislead.
    text = WORKFLOW.read_text()
    assert "reporting-only until" not in text
    assert "non-blocking" not in text


def test_teed_steps_run_under_the_default_shell_the_test_executes():
    # The steps above are executed under `bash -e`, GitHub's default. An explicit
    # `shell: bash` adds pipefail, under which `-e` kills the bandit step at the
    # pipeline and its summary fence is never closed - so pin the default.
    workflow = _workflow()
    assert "shell" not in workflow.get("defaults", {}).get("run", {})
    for job in workflow["jobs"].values():
        assert "shell" not in job.get("defaults", {}).get("run", {})
    for param in _teed_steps():
        assert "shell" not in param.values[0]


@pytest.mark.parametrize("job_id", sorted(AUDIT_JOB_ACTION_INPUTS))
def test_audit_job_setup_action_inputs_are_pinned(job_id):
    used = [
        (step["uses"].split("@")[0], step.get("with") or {})
        for step in _workflow()["jobs"][job_id]["steps"]
        if "uses" in step
    ]
    assert used == AUDIT_JOB_ACTION_INPUTS[job_id]


def _run_bodies():
    for job_id, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            if "run" in step:
                yield f"{job_id}:{step.get('name')}", step["run"].replace("\\\n", "")


def test_no_step_turns_on_job_control_or_reaches_past_the_stubs():
    for where, body in _run_bodies():
        normalised = "\n".join(_normalised(line) for line in body.splitlines())
        assert not JOB_CONTROL.search(normalised), f"{where} turns on job control"
        reaches = COMMAND_P.search(normalised)
        assert not reaches, f"{where} runs `{reaches.group(1)} -p`"
        for word in _command_words(body):
            assert not word.startswith("/"), (
                f"{where} runs {word} by absolute path, past the stubs"
            )
        for token in _assignments(body):
            assert not PATH_ASSIGNMENT.match(token), (
                f"{where} sets {token}, putting the real tools back on PATH"
            )


@pytest.mark.parametrize(
    "body",
    [
        "set -m",
        "set -mE",
        "set -Em",
        "set -e -m",
        "set -o monitor",
        "  set  -o   monitor",
        # Quoted and escaped: the same instructions, invisible to a pattern
        # matching characters. `set -\m` really does turn job control on.
        "set -o 'monitor'",
        'set -o "monitor"',
        'set "-m"',
        "set '-m'",
        "set -o mon'itor'",
        "set -\\m",
        "set -o \\monitor",
        "set -o mon\\itor",
    ],
)
def test_job_control_check_sees_every_spelling(body):
    assert JOB_CONTROL.search(_normalised(body))


@pytest.mark.parametrize("body", ["set -e", "set -o pipefail", "set +x", "echo -m"])
def test_job_control_check_passes_other_options(body):
    assert not JOB_CONTROL.search(_normalised(body))


@pytest.mark.parametrize(
    "body",
    ["command -p git", "command -pv git", "command -v -p git", "hash -p /x/git git"],
)
def test_command_p_check_sees_every_spelling(body):
    assert COMMAND_P.search(_normalised(body))


@pytest.mark.parametrize(
    "body,builtin",
    [
        ('command "-p" git', "command"),
        ("command '-p' git", "command"),
        ('hash "-p" /x/git git', "hash"),
        ("hash -p /x/git git", "hash"),
        ("command -\\p git", "command"),
        ("hash -\\p /x/git git", "hash"),
    ],
)
def test_command_p_check_names_the_builtin_that_matched(body, builtin):
    # The failure message used to say `command -p` for a `hash -p` hit.
    match = COMMAND_P.search(_normalised(body))
    assert match and match.group(1) == builtin


@pytest.mark.parametrize(
    "body",
    [
        "/usr/bin/npm audit",
        "/bin/sh -c 'npm audit'",
        "FOO=1 /usr/bin/pip-audit -r r.txt",
        "echo hi; /usr/bin/pip-audit",
        "echo hi && /sbin/x",
        "true | /bin/cat",
        "(/usr/bin/npm audit)",
        "'/usr/bin/npm' audit",
        'FOO=1 "/usr/bin/npm" audit',
        "\\/usr/bin/npm audit",
        # Behind a keyword or a wrapper builtin, which is always available
        # whatever the stub PATH holds.
        "if true; then /bin/cat /etc/hostname; fi",
        "command /bin/cat /etc/hostname",
        "builtin . /bin/x",
        "! /usr/bin/npm audit",
        "{ /usr/bin/npm audit; }",
        "env /usr/bin/npm audit",
        "exec /bin/sh",
        "eval /bin/sh",
        "time /usr/bin/npm audit",
        "xargs /bin/cat",
        "sudo /bin/cat",
        # On the second line, where a line-oriented scan must still look.
        "echo hi\n/bin/cat x",
    ],
)
def test_absolute_path_command_words_are_seen(body):
    assert any(w.startswith("/") for w in _command_words(body))


@pytest.mark.parametrize(
    "body",
    [
        "npm audit --omit=dev",
        "pip-audit -r backend/requirements.txt -f markdown",
        "pip-audit -r services/mcp_oauth_gateway/requirements.txt -f markdown",
        'echo "## pip-audit — /usr/bin is not a command here" >> "$GITHUB_STEP_SUMMARY"',
        # Quoted parens stay inside their word, so the heading is one argument
        # and `/backend` never reaches a command position.
        'echo "## bandit (/backend + /services)" >> "$GITHUB_STEP_SUMMARY"',
        "bandit -r backend services -x '*/tests/*,*/test_*.py' -ll",
        'status="${PIPESTATUS[0]}"',
        'exit "${PIPESTATUS[0]}"',
        'pip-audit -r r.txt 2>&1 | tee -a "$GITHUB_STEP_SUMMARY"',
        "echo hi > /dev/null",
    ],
)
def test_relative_command_words_are_not_flagged(body):
    assert not any(w.startswith("/") for w in _command_words(body))


@pytest.mark.parametrize(
    "body",
    [
        "PATH=/usr/bin npm audit",
        "PATH=/usr/bin:$PATH",
        "export PATH=/usr/bin:$PATH",
        "PATH='/usr/bin' npm audit",
        # `+=` appends, which puts the real tools back just as surely.
        "PATH+=:/usr/bin",
        "export PATH+=:/usr/bin",
        # Behind a wrapper or a keyword. `env` execvp's with the new PATH, so
        # this one genuinely reaches the real npm.
        "env PATH=/usr/bin npm audit",
        "if true; then PATH=/usr/bin npm audit; fi",
        "time PATH=/usr/bin npm audit",
        "! PATH=/usr/bin npm audit",
    ],
)
def test_a_path_assignment_is_seen(body):
    # An absolute path in a command word is not the only way back to the real
    # tools: re-pointing PATH reaches them with no absolute path to find.
    assert any(PATH_ASSIGNMENT.match(tok) for tok in _assignments(body))


@pytest.mark.parametrize(
    "body",
    [
        "npm audit --omit=dev",
        'status="${PIPESTATUS[0]}"',
        # An argument, not an assignment: this prints a string.
        'echo "PATH=/usr/bin" >> "$GITHUB_STEP_SUMMARY"',
        "pip-audit -r backend/requirements.txt -f markdown",
    ],
)
def test_a_path_assignment_check_leaves_other_lines_alone(body):
    assert not any(PATH_ASSIGNMENT.match(tok) for tok in _assignments(body))


@pytest.mark.parametrize(
    "line,stripped",
    [
        # A `#` mid-word is a literal, so the rest of the line still runs.
        ("echo a#b", "echo a#b"),
        (
            "pip install pip-audit#x; pip config set global.no-deps true",
            "pip install pip-audit#x; pip config set global.no-deps true",
        ),
        # A `#` that begins a word starts a comment, wherever on the line.
        ("npm audit --omit=dev # note", "npm audit --omit=dev "),
        ("# whole line", ""),
        ("  # indented", "  "),
        # Quoted, so not a comment at all.
        (
            'echo "## heading" >> "$GITHUB_STEP_SUMMARY"',
            'echo "## heading" >> "$GITHUB_STEP_SUMMARY"',
        ),
        ("echo '#hash'", "echo '#hash'"),
        ("echo \\#escaped", "echo \\#escaped"),
    ],
)
def test_comments_are_stripped_the_way_bash_strips_them(line, stripped):
    assert _strip_comment(line) == stripped


@pytest.mark.parametrize(
    "line",
    [
        "pip install pip-audit#x; pip config set global.no-deps true",
        "pip install pip-audit#x; PATH=/usr/bin:$PATH",
        "npm audit --omit=dev#x; npm config set omit=peer",
    ],
)
def test_a_mid_word_hash_does_not_hide_the_rest_of_the_line(line):
    # Letting shlex cut the token at `#` made each of these normalise to exactly
    # a pinned command, so the allowlist passed a line bash runs as two.
    normalised = _normalised(line)
    assert ";" in normalised, normalised
    for pinned in AUDIT_COMMANDS["pip-audit"] + PINNED_PIP_LINES["pip-audit"]:
        assert normalised != _normalised(pinned)


def test_an_unparsable_line_fails_rather_than_passing_vacuously():
    with pytest.raises(AssertionError, match="does not tokenise"):
        list(_command_words("echo 'unbalanced"))


def test_audit_mention_sees_npm_configuration_and_abbreviations():
    for line in (
        "npm config set omit=peer --location=project",
        "npm aud",
        "npx x",
        "pip config set global.no-deps true",
    ):
        assert AUDIT_MENTION.search(line), line


@pytest.mark.parametrize(
    "body",
    [
        "npm_config_omit=dev npm audit",
        'declare -x "npm""_config_omit=dev"; npm audit',
        "export PIP_NO_DEPS=1; pip-audit",
        "/usr/bin/env npm_config_audit-level=critical npm audit",
    ],
)
def test_stubs_catch_audit_configuration_in_their_environment(body, tmp_path):
    step = {"name": "self-test", "run": body}
    with pytest.raises(AssertionError, match="hands"):
        _run_step(step, tmp_path, scanner_exit=0)


@pytest.mark.timeout(60)
def test_a_timed_out_step_is_killed_with_its_background_children(tmp_path):
    # With only the leader killed, the child still holds stdout and the
    # reaping communicate() never returns; the marker turns that hang into a
    # failure.
    step = {
        "name": "self-test",
        "run": "(while :; do :; done) &\nwhile :; do :; done\n",
    }
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_step(step, tmp_path, scanner_exit=0, timeout=1)
    # Well inside the 30 s group drain: the timeout path killed the group itself.
    assert time.monotonic() - started < 10
