"""Pins the shape of the two non-required Playwright e2e jobs in ci.yml.

`desktop-e2e` and `mobile-e2e` are deliberately NOT required checks: a red run
reports a browser regression without blocking a merge or a release. That makes
them easy to break silently. Nothing merge-blocking ever reads their result, so
an edit that stops them running the specs, hides a failure, or quietly promotes
one into the required path would leave every required check green.

`test_ci_gate_semantics.py` only catches edits that break a whole-workflow rule
(an unconditional job without a gate, a job reusing a required check name).
This file pins each job's own contract: when it runs, what it runs and where,
that a failure stays red and leaves a report, and that it stays out of every
dependency chain and out of branch protection.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from backend.tests.test_ci_gate_semantics import BRANCH_PROTECTION_CHECKS

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ROOT_PACKAGE_JSON = REPO_ROOT / "package.json"

# job id -> (check name, the exact Playwright command it runs). The command is
# compared whole, so a dropped or swapped --project, an added --list, --shard,
# --grep-invert or spec path, and a trailing `|| true` all fail here.
E2E_JOBS = {
    "desktop-e2e": (
        "Desktop e2e (non-required)",
        "npx playwright test --project=chromium",
    ),
    "mobile-e2e": (
        "Mobile e2e (non-required)",
        "npx playwright test tests/e2e/mobile-smoke.spec.js"
        " --project=mobile-iphone --project=mobile-pixel",
    ),
}

# Same terms as the heavy test workers: only when service code changed, and not
# on a draft PR. Pushes (no pull_request payload) always qualify.
E2E_IF = (
    "needs.detect-changes.outputs.service_code == 'true' && "
    "(github.event_name != 'pull_request' || "
    "github.event.pull_request.draft == false)"
)

MAX_TIMEOUT_MINUTES = 20

# Workflow-level env reaches every step. playwright.config.js reads CI for
# forbidOnly, retries and reuseExistingServer, so an added `CI: ''` changes how
# the specs run while every step still matches.
WORKFLOW_ENV_KEYS = {"REGISTRY", "CORE_IMAGE", "GATEWAY_IMAGE"}

# Any other job key can change where or whether the specs run: a `container`
# swaps the image, a `strategy` fans the job out, `defaults` re-shells steps.
E2E_JOB_KEYS = {"if", "name", "needs", "runs-on", "steps", "timeout-minutes"}
UPLOAD_STEP_KEYS = {"name", "if", "uses", "with"}
# `if-no-files-found: ignore` would let a failure leave no report and no error.
UPLOAD_WITH_KEYS = {"name", "path", "retention-days"}


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def as_list(needs):
    if needs is None:
        return []
    return [needs] if isinstance(needs, str) else list(needs)


def mentions_playwright(text):
    return "playwright" in text.replace("playwright install", "")


# A script name is matched as a whole shell word: npm allows almost any
# character in one, so a name is never split on anything but shell syntax.
SHELL_BOUNDARY = r"""[\s;&|()<>"'`]"""
# Scripts npm runs without anything naming them: `npm ci` and `npm install`
# run the install and dependencies lifecycles, `npm t`, `npm restart`, `npm
# pack` and their many aliases, abbreviations and flag-first spellings run the
# rest. Some events (install, pack, version and restart among them) also run
# their pre/post pair when the event itself has no script, while `npm test` and
# `npm stop` stop at "Missing script" and `npm start` does unless a server.js
# exists; the set assumes all of them do. Rather than parse npm's command
# grammar, any `npm` invocation counts as running every one of these - that can
# only over-detect, which fails loudly here.
NPM_LIFECYCLE_EVENTS = (
    "install",
    "dependencies",
    "prepublish",
    "prepare",
    "prepublishOnly",
    "pack",
    "publish",
    "version",
    "shrinkwrap",
    "test",
    "start",
    "stop",
    "restart",
    "uninstall",
)
IMPLICIT_SCRIPTS = {
    f"{prefix}{event}"
    for event in NPM_LIFECYCLE_EVENTS
    for prefix in ("", "pre", "post")
}
NPM = re.compile(r"\bnpm\b")


def invoked_scripts(text, scripts):
    names = {
        name
        for name in scripts
        if re.search(
            rf"(?:^|{SHELL_BOUNDARY}){re.escape(name)}(?=$|{SHELL_BOUNDARY})", text
        )
    }
    if NPM.search(text):
        names |= IMPLICIT_SCRIPTS
    return names


def playwright_scripts(scripts):
    """Names of the npm scripts that reach Playwright, directly, through
    another script or through a pre/post hook npm runs with it, so a wrapper
    added under any name is followed too."""
    reaching = {name for name, body in scripts.items() if mentions_playwright(body)}
    while True:
        more = {
            name
            for name, body in scripts.items()
            if name not in reaching
            and (
                invoked_scripts(body, scripts) & reaching
                or {f"pre{name}", f"post{name}"} & reaching
            )
        }
        if not more:
            return reaching
        reaching |= more


@pytest.fixture(scope="module")
def npm_scripts():
    """Scripts of the root and every workspace, merged by name: `npm run X -w`
    may pick any workspace, so a name reaches Playwright if any body does."""
    root = json.loads(ROOT_PACKAGE_JSON.read_text())
    paths = [ROOT_PACKAGE_JSON] + sorted(
        path
        for pattern in root["workspaces"]
        for path in REPO_ROOT.glob(f"{pattern}/package.json")
    )
    scripts = {}
    for path in paths:
        for name, body in json.loads(path.read_text()).get("scripts", {}).items():
            scripts[name] = f"{scripts.get(name, '')}\n{body}"
    return scripts


def runs_playwright_tests(step, scripts):
    """Any step that reaches Playwright other than to install a browser - a
    direct `playwright test` or an npm script that runs it, under any name,
    so a job spelled some other way is still discovered."""
    run = str(step.get("run", ""))
    return mentions_playwright(run) or bool(
        invoked_scripts(run, scripts) & playwright_scripts(scripts)
    )


def playwright_test_steps(job, scripts):
    return [
        step for step in job.get("steps", []) if runs_playwright_tests(step, scripts)
    ]


def upload_steps(job):
    return [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]


def test_the_e2e_job_set_is_discovered_not_only_listed(workflow, npm_scripts):
    """Every parametrised test below runs over E2E_JOBS, so a new Playwright
    job, or a renamed one, must fail here rather than go unchecked."""
    discovered = {
        job_id
        for job_id, job in workflow["jobs"].items()
        if playwright_test_steps(job, npm_scripts)
    }
    assert discovered == set(E2E_JOBS) == {"desktop-e2e", "mobile-e2e"}


@pytest.mark.parametrize(
    "extra, run",
    [
        ({"test:mobile": "playwright test"}, "npm run test:mobile"),
        ({"smoke+ci": "playwright test"}, "npm run smoke+ci -w @community-graph/web"),
        (
            {"test:mobile": "playwright test", "ci:browser": "npm run test:mobile"},
            "npm run ci:browser && echo done",
        ),
        ({"posttest": "playwright test"}, "npm run test:unit"),
        ({"prelint": "playwright test"}, "npm run lint"),
        ({"postinstall": "playwright test"}, "npm ci --no-audit --no-fund"),
        ({"prepare": "playwright test"}, "npm install"),
        ({"test": "playwright test"}, "npm t"),
        ({"test": "playwright test"}, "npm --silent tes"),
        ({"prepublish": "playwright test"}, "npm ci"),
        ({"preprepare": "playwright test"}, "npm ic"),
        ({"postprepare": "playwright test"}, "npm install"),
        ({"postinstall": "playwright test"}, "npm --prefix frontend/web ci"),
        ({"postinstall": "playwright test"}, "npm -w @community-graph/web sit"),
        ({"dependencies": "playwright test"}, "npm ci"),
        ({"predependencies": "playwright test"}, "npm ci"),
        ({"preinstall": "playwright test"}, "npm ci"),
        ({"install": "playwright test"}, "npm ci"),
        ({"prepack": "playwright test"}, "npm pack"),
        ({"stop": "playwright test"}, "npm restart"),
        ({"prestart": "playwright test"}, "npm restart"),
        ({}, "npm run test:e2e"),
    ],
)
def test_discovery_follows_npm_scripts_under_any_name(npm_scripts, extra, run):
    """The real package.json files hold none of these wrappers today, so pin
    that discovery would see each one rather than wait for it to slip past."""
    assert runs_playwright_tests({"run": run}, {**npm_scripts, **extra})


# The lifecycle events npm's scripts documentation lists, current and legacy
# (prepublish, shrinkwrap, uninstall), each with both prefixes as the
# over-approximation above assumes. Written out independently of
# NPM_LIFECYCLE_EVENTS, so dropping an event or a prefix there fails below.
DOCUMENTED_LIFECYCLE_SCRIPTS = sorted(
    f"{prefix}{event}"
    for event in (
        "dependencies",
        "install",
        "pack",
        "prepare",
        "prepublish",
        "prepublishOnly",
        "publish",
        "restart",
        "shrinkwrap",
        "start",
        "stop",
        "test",
        "uninstall",
        "version",
    )
    for prefix in ("", "pre", "post")
)


@pytest.mark.parametrize("name", DOCUMENTED_LIFECYCLE_SCRIPTS)
def test_any_npm_invocation_counts_as_running_every_lifecycle_script(npm_scripts, name):
    assert runs_playwright_tests(
        {"run": "npm ci"}, {**npm_scripts, name: "playwright test"}
    )


@pytest.mark.parametrize(
    "run", ["npx playwright install chromium", "npm run test:unit", "npm ci", "npm t"]
)
def test_discovery_leaves_non_playwright_steps_alone(npm_scripts, run):
    assert not runs_playwright_tests({"run": run}, npm_scripts)


def test_nothing_workflow_level_reaches_the_e2e_steps(workflow):
    """A workflow-level `defaults: run: shell:` re-shells every run step, so
    `bash -c 'exit 0' {0}` would turn both Playwright steps into no-ops."""
    assert "defaults" not in workflow
    assert set(workflow.get("env", {})) == WORKFLOW_ENV_KEYS


@pytest.mark.parametrize("job_id", sorted(E2E_JOBS))
class TestE2EJobShape:
    def test_carries_only_the_pinned_job_keys(self, workflow, job_id):
        assert set(workflow["jobs"][job_id]) == E2E_JOB_KEYS

    def test_runs_only_on_changed_service_code_and_never_on_a_draft(
        self, workflow, job_id
    ):
        job = workflow["jobs"][job_id]
        assert " ".join(str(job["if"]).split()) == E2E_IF
        assert as_list(job["needs"]) == ["detect-changes"]

    def test_runs_the_intended_specs_from_the_web_workspace(
        self, workflow, npm_scripts, job_id
    ):
        steps = playwright_test_steps(workflow["jobs"][job_id], npm_scripts)
        assert len(steps) == 1, f"{job_id}: expected one playwright test step"
        step = steps[0]
        assert step["run"].strip() == E2E_JOBS[job_id][1]
        assert step.get("working-directory") == "frontend/web"
        # Exact keys: an `if` makes the step skippable, and a `shell`, `env` or
        # `continue-on-error` can hide a failure or change which specs run
        # while the command text above still matches.
        assert set(step) == {"name", "run", "working-directory"}, (
            f"{job_id}: unexpected keys on the test step: {sorted(step)}"
        )
        job = workflow["jobs"][job_id]
        assert "env" not in job and "defaults" not in job, (
            f"{job_id}: a job-level env or defaults reaches the test step"
        )

    def test_timeout_is_bounded(self, workflow, job_id):
        timeout = workflow["jobs"][job_id].get("timeout-minutes")
        assert type(timeout) is int and 0 < timeout <= MAX_TIMEOUT_MINUTES, (
            f"{job_id}: timeout-minutes={timeout!r}; without a bound a wedged "
            "browser holds a runner for GitHub's six-hour default"
        )

    def test_a_failure_stays_red(self, workflow, job_id):
        """Non-required already keeps a failure from blocking anything, so
        continue-on-error would only turn the one signal these jobs give green."""
        job = workflow["jobs"][job_id]
        assert not job.get("continue-on-error", False)
        for step in job["steps"]:
            assert not step.get("continue-on-error", False), (
                f"{job_id} step {step.get('name')!r} sets continue-on-error"
            )

    def test_a_failure_uploads_its_own_report(self, workflow, job_id):
        steps = upload_steps(workflow["jobs"][job_id])
        assert len(steps) == 1, f"{job_id}: expected one report upload step"
        step = steps[0]
        assert str(step.get("if", "")).strip() == "failure()"
        assert step["with"]["path"] == "frontend/web/playwright-report/"
        assert step["with"]["name"] == f"{job_id}-report"
        assert set(step) == UPLOAD_STEP_KEYS
        assert set(step["with"]) == UPLOAD_WITH_KEYS

    def test_the_check_name_is_marked_and_not_required(self, workflow, job_id):
        name = workflow["jobs"][job_id]["name"]
        assert name == E2E_JOBS[job_id][0]
        assert name not in BRANCH_PROTECTION_CHECKS
        assert f"`{name}`" not in CLAUDE_MD.read_text(), (
            f"CLAUDE.md names {name!r} as a check; these jobs are non-required"
        )

    def test_nothing_depends_on_it(self, workflow, job_id):
        """A job in any `needs` would either block that job on a browser
        regression (build, notify-infra) or be a gate that makes it required."""
        dependants = [
            other
            for other, job in workflow["jobs"].items()
            if job_id in as_list(job.get("needs"))
        ]
        assert dependants == [], f"{job_id} is listed in needs of {dependants}"


def test_report_artifact_names_are_unique_across_the_workflow(workflow):
    """Two jobs uploading under one name in the same run make the second
    upload fail, and the failure report of one job is lost."""
    names = [
        step["with"]["name"]
        for job in workflow["jobs"].values()
        for step in upload_steps(job)
    ]
    assert len(names) == len(set(names)), f"duplicate artifact names: {names}"
