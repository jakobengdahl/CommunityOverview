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

# Owner decision: bandit stays reporting-only until its last findings on main
# are cleared. Every other scanner blocks. Promoting bandit edits this set.
REPORTING_ONLY_STEPS = {("bandit", "Run bandit (medium+ severity, non-blocking)")}

# A step invokes an audit when a line of its body starts with the scanner, so
# `pip install pip-audit` is not one and a step that stops teeing still is.
AUDIT_INVOCATION = re.compile(r"^\s*(?:pip-audit|npm\s+audit)\b.*$", re.M)

# Any line that could run an audit, however spelled: `python -m pip_audit`,
# `npx audit-ci`, `npm --prefix . audit`, `FOO=1 npm audit`.
AUDIT_MENTION = re.compile(r"\baudit\b|pip[-_]audit", re.I)
AUDIT_INSTALL = "pip install pip-audit"
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


def _run_step(step, tmp_path, scanner_exit):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name in STUBBED:
        stub = stub_dir / name
        stub.write_text(f"#!/bin/sh\necho 'stub {name} report'\nexit {scanner_exit}\n")
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
    # and RUNNER_* variables name that job's real command files.
    inherited = {
        k: v
        for k, v in os.environ.items()
        if k not in SHELL_STARTUP_VARS and not k.startswith(("GITHUB_", "RUNNER_"))
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
        proc.communicate(timeout=30)
    finally:
        drained = _wait_for_process_group(proc.pid, timeout=30)
    assert drained, f"step {step['name']!r} left processes running"
    written = sorted(
        str(f.relative_to(runner_temp))
        for f in runner_temp.rglob("*")
        if f.is_file() and f.stat().st_size
    )
    assert not written, f"step {step['name']!r} wrote runner command files {written}"
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
    for job_id, job in _workflow()["jobs"].items():
        pinned = AUDIT_COMMANDS.get(job_id, [])
        for step in job.get("steps", []):
            body = step.get("run", "").replace("\\\n", "")
            for line in body.splitlines():
                line = line.strip()
                if not AUDIT_MENTION.search(line) or SUMMARY_HEADING.match(line):
                    continue
                if job_id == "pip-audit" and line == AUDIT_INSTALL:
                    continue
                assert line in pinned, f"{job_id}:{step['name']} runs {line!r}"


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


def _npmrc_keys(text):
    for line in text.splitlines():
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


def test_bandit_reporting_only_state_is_marked_temporary():
    # Parsed YAML drops comments, so read the raw text around the key.
    lines = WORKFLOW.read_text().splitlines()
    step = lines.index("      - name: Run bandit (medium+ severity, non-blocking)")
    assert lines[step + 1].strip().startswith("# Temporary: reporting-only until")
    assert lines[step + 2].strip() == "continue-on-error: true"


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
