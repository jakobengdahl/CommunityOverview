"""
The credential guarantee: no credential value lives in this repository, the
harness reads one only from the environment, and no test needs a real one.

This is the guarantee that decides whether the harness is safe to commit to a
public repository and safe to hand to someone who will run it against a paid
endpoint. It is tested rather than asserted in prose because the failure is
silent: a key pasted into a fixture, or echoed into a report, looks like nothing
until the repository is public — which this one is.

Stated precisely. The harness resolves ``credential_ref`` against
``os.environ`` before anything imports the assistant, so the ``load_dotenv()``
that reads a repo-root ``.env`` has not run yet and a value there does NOT
satisfy the credential — pinned below, because the opposite was documented at
one point and it is the claim an operator acts on. ``.env`` does reach
``os.environ`` later in the process, which is why a flat "nothing else is read"
would still be false.

What is tested here is what the harness is responsible for: no credential value
is committed, no code path reads one from a file, it resolves
``credential_ref`` against the environment at call time with no fallback and no
override parameter, nothing it writes carries the value, and the suite runs with
every provider variable cleared. See docs/SKILL_EVALUATION.md for the
operator-facing version.
"""

import json
import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config.model_profiles import (
    MissingCredentialError,
    ModelProfile,
    create_provider_from_profile,
)
from backend.evaluation.cases import FIXTURES_DIR
from backend.evaluation.runner import (
    build_report,
    build_skills_context,
    default_provider_factory,
    load_profiles,
    run_case,
    run_suite,
)

import sys

REPO_ROOT = Path(__file__).resolve().parents[3]

EVAL_DIR = Path(__file__).resolve().parent.parent
# Shaped so the repository scan below does not flag this file: the hyphens stop
# it matching the key patterns, which all require an unbroken alphanumeric run.
# Keep it that way — a sentinel that matched would make the scan fail on itself,
# and the tempting fix is to loosen the patterns.
SENTINEL = "sk-test-SENTINEL-must-never-be-written-anywhere-0123456789"
PROSE_SENTINEL = "ZZQQ-model-prose-marker-ZZQQ"


class TestNoCredentialInTheRepository:
    def test_no_harness_file_contains_a_key_shaped_literal(self):
        """
        Nothing under backend/evaluation/ may carry a provider key.

        Matches the shapes the supported providers issue. A fixture or a
        committed report is where such a value would realistically end up.
        """
        patterns = [
            re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
            re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
            re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
        ]
        offenders = []
        for path in sorted(EVAL_DIR.rglob("*")):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                if pattern.search(text):
                    offenders.append(str(path.relative_to(EVAL_DIR)))
        assert not offenders, f"credential-shaped literal(s) in {offenders}"

    def test_no_fixture_declares_a_credential_field(self):
        """
        A fixture is data the owner edits; it must offer nowhere to put a key.

        Checked on JSON *keys*, not on the prose: a case's notes legitimately
        discuss tokens and secrets, and a substring scan over prose would fail
        on that while missing nothing real.
        """
        banned = re.compile(r"(api[_-]?key|secret|token|password|credential)", re.I)

        def keys(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    yield key
                    yield from keys(value)
            elif isinstance(obj, list):
                for item in obj:
                    yield from keys(item)

        for path in sorted(FIXTURES_DIR.rglob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            offenders = [key for key in keys(document) if banned.search(key)]
            assert not offenders, f"{path.name} declares field(s) {offenders}"


class TestProfileFilesCannotCarryCredentials:
    def test_an_inline_key_in_credential_ref_is_rejected(self):
        """credential_ref names a variable; a value there is the mistake to catch."""
        with pytest.raises(ValidationError, match="never embed one"):
            ModelProfile(
                id="p",
                name="P",
                provider="openai",
                model="m",
                credential_ref=SENTINEL,
            )

    def test_a_secret_looking_option_value_is_rejected(self):
        with pytest.raises(ValidationError, match="inline secret"):
            ModelProfile(
                id="p",
                name="P",
                provider="openai",
                model="m",
                options={"api_key": SENTINEL},
            )

    def test_a_profile_file_carrying_a_key_fails_to_load(self, tmp_path):
        path = tmp_path / "providers.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "id": "p",
                        "name": "P",
                        "provider": "openai",
                        "model": "m",
                        "default": True,
                        "credential_ref": SENTINEL,
                    }
                ]
            )
        )
        with pytest.raises(ValidationError):
            load_profiles(path)

    def test_a_valid_profile_file_loads_and_only_names_a_variable(self, tmp_path):
        path = tmp_path / "providers.json"
        path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "id": "p",
                            "name": "P",
                            "provider": "openai",
                            "model": "m",
                            "default": True,
                            "endpoint": "https://example.invalid/v1",
                            "credential_ref": "EVAL_HARNESS_TEST_KEY",
                        }
                    ]
                }
            )
        )
        profiles = load_profiles(path)
        assert profiles[0].credential_ref == "EVAL_HARNESS_TEST_KEY"

    def test_a_profile_file_that_is_not_a_list_or_profiles_object_is_rejected(
        self, tmp_path
    ):
        path = tmp_path / "providers.json"
        path.write_text(json.dumps("not a profile"))
        with pytest.raises(ValueError, match="must contain a JSON array"):
            load_profiles(path)


class TestCredentialsComeOnlyFromTheEnvironment:
    def test_the_provider_factory_reads_the_named_environment_variable(
        self, monkeypatch, profile
    ):
        """Read at call time, so nothing has to hold the value."""
        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        captured = {}

        class FakeOpenAI:
            def __init__(self, api_key=None, base_url=None):
                captured["api_key"] = api_key
                captured["base_url"] = base_url

        import openai

        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
        default_provider_factory(profile)
        assert captured["api_key"] == SENTINEL
        assert captured["base_url"] == profile.endpoint

    def test_an_unset_variable_fails_loudly_rather_than_defaulting(
        self, monkeypatch, profile
    ):
        """
        No fallback to ANTHROPIC_API_KEY or OPENAI_API_KEY.

        A harness that quietly fell back would attribute one endpoint's
        behaviour to another, and bill a key the operator did not choose.
        """
        monkeypatch.delenv(profile.credential_ref, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
        monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
        with pytest.raises(MissingCredentialError, match=profile.credential_ref):
            default_provider_factory(profile)

    def test_the_factory_exposes_no_way_to_pass_a_literal_key(self):
        """
        An override parameter is the hole someone eventually passes a key to.

        create_provider_from_profile has api_key_override for the chat UI's
        bring-your-own-key path; the harness's factory must not re-export it.
        """
        import inspect

        assert set(inspect.signature(default_provider_factory).parameters) == {
            "profile"
        }
        assert (
            "api_key_override"
            in inspect.signature(create_provider_from_profile).parameters
        )


class TestADotEnvFileIsNotAReliableCredentialSource:
    """
    Pinned because this claim has been documented wrongly three times.

    First: nothing but the shell is read. Then: a repo-root `.env` satisfies a
    `credential_ref`. Then: it never does. Then: `find_dotenv()` walks up from
    the calling file and ignores the cwd — which is only true for a normal
    script. `find_dotenv` falls back to `os.getcwd()` whenever `usecwd` is set,
    under a debugger or coverage (`sys.gettrace()`), when frozen, or when there
    is no `__main__.__file__` at all — which is exactly `python -c`. The first
    version of this test used `python -c`, so it passed *because of* the cwd,
    the mechanism its own docstring named as excluded; it could not have
    detected that the claim was wrong.

    So the honest statement, and what is pinned below: a repo-root `.env` does
    reach `os.environ` once the assistant is imported, by the frame walk for a
    script entry point and by the cwd in the other cases. The CLI refuses
    regardless, because it gates credentials before any import. A library caller
    refuses only while nothing in the process has imported the assistant yet.
    Which is why the operator guidance is "export in your shell", not "`.env`
    does not work".

    These run in subprocesses, because import state and a repo-root file are both
    process-global, and the probe is a real `.py` file run from OUTSIDE the
    repository so the frame walk — not the cwd — is what finds it.
    """

    @staticmethod
    def _run(script: str, env_body: str):
        """Run a script with a real repo-root .env present, then remove it."""
        import subprocess

        dotenv = REPO_ROOT / ".env"
        pre_existing = dotenv.exists()
        if pre_existing:  # never clobber a developer's own file
            pytest.skip("a .env already exists at the repository root")
        dotenv.write_text(env_body, encoding="utf-8")
        try:
            return subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=180,
            )
        finally:
            dotenv.unlink()

    def test_the_frame_walk_finds_the_repo_root_from_outside_the_repository(
        self, tmp_path
    ):
        """
        The documented mechanism, isolated from the cwd fallback.

        Run as a real `.py` file (so `__main__.__file__` exists and
        `find_dotenv` uses the frame walk) from a cwd OUTSIDE the repository,
        with a decoy `.env` sitting in that cwd. If the repo-root value wins,
        the frame walk is what found it. The previous version used `python -c`
        from inside the repo, which took the cwd branch and so proved the
        opposite of what it claimed.
        """
        import subprocess

        (tmp_path / ".env").write_text(
            "SKILL_EVAL_DOTENV_PROBE=from-the-decoy-cwd\n", encoding="utf-8"
        )
        probe = tmp_path / "probe_frame_walk.py"
        probe.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from backend.ui import chat_logic  # runs load_dotenv() at import\n"
            "import os\n"
            "print('FOUND:', os.environ.get('SKILL_EVAL_DOTENV_PROBE'))\n",
            encoding="utf-8",
        )

        dotenv = REPO_ROOT / ".env"
        if dotenv.exists():
            pytest.skip("a .env already exists at the repository root")
        dotenv.write_text("SKILL_EVAL_DOTENV_PROBE=from-repo-root\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, str(probe)],
                cwd=str(tmp_path),
                capture_output=True,
                text=True,
                timeout=180,
                # Strip any tracer so find_dotenv cannot take the cwd branch.
                env={**os.environ, "COVERAGE_PROCESS_START": ""},
            )
        finally:
            dotenv.unlink()

        assert "FOUND: from-repo-root" in result.stdout, (
            "the frame walk did not reach the repository root; got "
            f"{result.stdout!r} {result.stderr[-400:]!r}"
        )
        assert "from-the-decoy-cwd" not in result.stdout

    def test_the_cli_refuses_a_dot_env_credential(self):
        """The reliable half: the gate runs before any import of the assistant."""
        script = (
            "import sys, json, pathlib, tempfile; sys.path.insert(0, %r)\n"
            "from scripts.run_skill_eval import main\n"
            "p = pathlib.Path(tempfile.mkdtemp()) / 'probe-profiles.json'\n"
            "p.write_text(json.dumps([{'id':'probe','name':'P','provider':'openai',"
            "'model':'m','default':True,'credential_ref':'SKILL_EVAL_DOTENV_PROBE'}]))\n"
            "try:\n"
            "    print('EXIT:', main(['--profiles', str(p)]))\n"
            "finally:\n"
            "    p.unlink()\n"
        ) % str(REPO_ROOT)
        result = self._run(script, "SKILL_EVAL_DOTENV_PROBE=from-repo-root\n")
        assert "EXIT: 2" in result.stdout, result.stdout + result.stderr
        assert "SKILL_EVAL_DOTENV_PROBE is not set" in result.stderr

    def test_a_library_caller_refuses_until_the_assistant_has_been_imported(self):
        """
        The conditional half, both sides.

        In a fresh process the provider build refuses, because it happens
        before the assistant is imported. Once something has imported the
        assistant, `load_dotenv()` has run and the same build succeeds. That
        asymmetry is the reason the docs tell the operator to export instead.
        (It drove a whole `run_case` until round 7; that made a real HTTP
        request, and resolution is what the claim was ever about.)
        """
        # `default_provider_factory`, not `run_case`. The claim is about where
        # the credential is RESOLVED, and resolution happens in the provider
        # build — but `run_case` goes on to drive the chat path, so once the
        # second call stopped refusing it built a real client and issued a real
        # HTTP POST. This child process is outside the autouse egress guard,
        # which only covers the test process, so the suite genuinely made
        # outbound calls while three places claimed it never does. Building a
        # client sends nothing, so this drives exactly the asymmetry the docs
        # describe and nothing more.
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from backend.config.model_profiles import ModelProfile\n"
            "from backend.evaluation.runner import default_provider_factory\n"
            "prof = ModelProfile(id='p', name='P', provider='openai', model='m',\n"
            "                    default=True, credential_ref='SKILL_EVAL_DOTENV_PROBE')\n"
            # Reports the outcome, not a boolean: `'MissingCredentialError' in
            # ...` was true of a refusal and false of ANY other outcome,
            # including an ImportError, so the second assertion read "did not
            # refuse for want of a credential" rather than "accepted the value".
            "def outcome():\n"
            "    try:\n"
            "        default_provider_factory(prof)\n"
            "    except Exception as exc:\n"
            "        return type(exc).__name__\n"
            "    return 'built'\n"
            "print('FRESH:', outcome())\n"
            "import backend.ui.chat_logic  # noqa  -- runs load_dotenv()\n"
            "import os\n"
            "print('NOW_IN_ENV:', os.environ.get('SKILL_EVAL_DOTENV_PROBE') is not None)\n"
            "print('SECOND:', outcome())\n"
        ) % str(REPO_ROOT)
        result = self._run(script, "SKILL_EVAL_DOTENV_PROBE=from-repo-root\n")
        assert "FRESH: MissingCredentialError" in result.stdout, (
            result.stdout + result.stderr
        )
        assert "NOW_IN_ENV: True" in result.stdout, result.stdout + result.stderr
        # The operative half of the docs' claim, which was asserted only as
        # "the value is now in the environment": the SECOND call must accept it.
        # A regression that made it refuse would have passed before.
        # The provider was BUILT, not merely "did not refuse for want of a
        # credential" — which is what the docs claim and what the previous
        # boolean could not distinguish from an unrelated exception.
        assert "SECOND: built" in result.stdout, result.stdout + result.stderr


class TestReportsNeverCarryACredential:
    def test_a_report_omits_a_credential_the_provider_really_did_receive(
        self, monkeypatch, profile, case_by_id
    ):
        """
        The guarantee, tested against a provider that actually holds the key.

        The sibling test below injects a scripted provider that never reads the
        credential, so "the key is not in the report" could not fail there. Here
        the real OpenAIProvider is built through default_provider_factory with a
        fake SDK client capturing what it was handed — so the key demonstrably
        reached the provider — and the report is still checked for it.
        """
        from backend.evaluation.runner import default_provider_factory
        from backend.llm.llm_providers import LLMResponse

        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        captured = {}

        class FakeOpenAI:
            def __init__(self, api_key=None, base_url=None):
                captured["api_key"] = api_key
                self.chat = self

            @property
            def completions(self):
                return self

            def create(self, **kwargs):
                raise AssertionError("no network call should be attempted")

        import openai

        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

        real_provider = default_provider_factory(profile)
        assert captured["api_key"] == SENTINEL, "precondition: provider holds the key"

        # Drive a scored run through a recorder wrapping that key-holding provider.
        class Scripted:
            def __init__(self, inner):
                self.inner = inner
                self.calls = 0

            def create_completion(
                self, messages, system_prompt, tools, max_tokens=4096
            ):
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(
                        content=[
                            {
                                "type": "tool_use",
                                "id": "c0",
                                "name": "search_graph",
                                "input": {"query": "Metadata Handbook"},
                            }
                        ],
                        stop_reason="tool_use",
                    )
                return LLMResponse(
                    content=[{"type": "text", "text": f"{PROSE_SENTINEL} done"}],
                    stop_reason="end_turn",
                )

            def format_tool_definitions(self, tools):
                return tools

        recorder_provider = Scripted(real_provider)
        result = run_suite(
            profile,
            cases=[case_by_id["tool-call-validity-read-path"]],
            provider_factory=lambda _p: recorder_provider,
        )
        payload = json.dumps(build_report(result))

        assert SENTINEL not in payload
        assert PROSE_SENTINEL not in payload

    def test_a_report_does_not_contain_the_credential_or_the_prompts(
        self, monkeypatch, profile, case_by_id
    ):
        """
        What makes a report safe to paste into an issue or commit.

        The key is never in the transcript to begin with — it lives inside the
        wrapped provider's SDK client — and the report also leaves the system
        prompt and the model's prose out.
        """
        from backend.evaluation.tests.conftest import ScriptedProvider

        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        provider = ScriptedProvider(
            [
                [("search_graph", {"query": "Metadata Handbook"})],
                f"{PROSE_SENTINEL} {SENTINEL}",
            ]
        )
        result = run_suite(
            profile,
            cases=[case_by_id["tool-call-validity-read-path"]],
            provider_factory=lambda _p: provider,
        )
        payload = json.dumps(build_report(result))

        assert SENTINEL not in payload
        # The system prompt is recorded for assertions but excluded from reports.
        # Phrases unique to the injected skill body — the dimension table in the
        # report legitimately repeats some of the skill's rule names.
        assert "not style preferences" not in payload
        assert "Comprehensive object inspection" not in payload
        assert "Graph Maintenance Protocol" not in payload
        # And the model's own prose stays out too.
        assert PROSE_SENTINEL not in payload

        # The PROMPT, which this test is named for and never checked. Adding a
        # one-line `"prompt": score.prompt` to the case row left the suite
        # green: the only `case.prompt not in payload` assertion ran on the
        # run-error sites, and round 5's leaf-bound walk bounds how LONG a
        # string may be, never which content may appear.
        case = case_by_id["tool-call-validity-read-path"]
        assert case.prompt not in payload

        # So the durable half is an allowlist: a new field in the case row has
        # to be added here deliberately, rather than inheriting whatever the
        # scorer happens to carry.
        #
        # Nested too. A top-level key set alone let a field be added INSIDE an
        # allowed key: `cases[N].dimensions` is per-run territory, and a
        # `tool_arguments` entry there carried 450 characters of raw model
        # arguments per tool call — under the per-leaf bound, under the
        # rendered-size margin, and inside a key the allowlist already
        # permitted.
        from backend.evaluation.dimensions import DIMENSIONS

        row = build_report(result)["cases"][0]
        assert set(row["dimensions"]) == set(DIMENSIONS), (
            "the per-case dimensions block has keys that are not dimensions: "
            f"{set(row['dimensions']) - set(DIMENSIONS)}"
        )
        for key, dimension in row["dimensions"].items():
            assert set(dimension) == {
                "scored",
                "passed",
                "mechanically_scored",
            }, f"cases[].dimensions.{key} carries unexpected fields: {set(dimension)}"

        # And `conditions[]`, which round 8's extension stopped one level short
        # of. These rows are per-run territory carrying model-derived content
        # by design, and a 450-character tail of the system prompt rode in one
        # with every other check green: it is under the leaf bound, it contains
        # none of the head-of-prompt phrases asserted below, and a constant
        # addition cancels in the rendered-size comparison.
        for index, condition in enumerate(row["conditions"]):
            assert set(condition) == {"name", "passed", "detail"}, (
                f"cases[].conditions[{index}] carries unexpected fields: "
                f"{set(condition)}"
            )

        # Substring-by-window, not exact match. `case.prompt not in payload`
        # is an exact match on the whole prompt, so any truncation escaped it;
        # the same was true of the rendered skill text.
        def windows(text, size=40):
            return {text[i : i + size] for i in range(0, max(len(text) - size, 1))}

        skills = build_skills_context(case.skill_paths()) or ""
        for label, text in (("prompt", case.prompt), ("skill text", skills)):
            leaked = sorted(window for window in windows(text) if window in payload)
            assert not leaked, f"{label} fragment(s) in the report: {leaked[:3]}"

        assert set(row) == {
            "case_id",
            "dimension",
            "passed",
            "run_error",
            "conditions",
            "dimensions",
            "latency_ms",
            "tokens",
            "provider_calls",
            "tool_calls",
        }

    def test_every_string_in_a_rendered_report_is_bounded_wherever_it_sits(
        self, profile
    ):
        """
        G8 was pinned per quoting SITE, so a new site was unpinned.

        Adding a `tool_arguments` field to the case row — raw model arguments,
        verbatim — left the suite green, because the payload-wide assertions
        hunt two named sentinels and the bound probes each read one
        `ConditionResult.detail`. Walking the rendered report and bounding every
        string leaf closes the shape instead of the instance: a future field
        carrying model-written content fails here without anyone remembering to
        add a probe for it.
        """
        from backend.evaluation.cases import AcceptanceCase, ExpectedBehaviour
        from backend.evaluation.tests.conftest import ScriptedProvider

        huge = "ZZQQ" * 800
        case = AcceptanceCase(
            id="report-leaf-bound-probe",
            dimension="unsupported_entity_reference",
            prompt="which initiative produces the Metadata Handbook?",
            graph="metadata-pilot-small.json",
            expect=ExpectedBehaviour(
                tool_calls_valid=True, answer_entities_supported=True
            ),
            notes=(
                "probe case whose run puts oversized model-written content into "
                "every field a report can quote it in"
            ),
        )
        provider = ScriptedProvider(
            [
                # An oversized tool NAME, not just an oversized argument. The
                # first version of this probe asked for `search_graph` and
                # `update_node`, so the one report field that is a raw
                # model-chosen string — the tool-call row — was never
                # oversized under it, and a 3200-character name reached the
                # report (and a file, via `--out`) with this test green. A
                # probe for "every string leaf" has to make every leaf big.
                [(huge, {"qeury": huge, "limit": "not-an-int"})],
                [("update_node", {"node_id": huge, "updates": {"summary": huge}})],
                f"{PROSE_SENTINEL} it is eval-{huge}-other.",
            ]
        )
        result = run_suite(profile, cases=[case], provider_factory=lambda _p: provider)
        report = build_report(result)

        def leaves(value, path="report"):
            if isinstance(value, str):
                yield path, value
            elif isinstance(value, dict):
                for key, item in value.items():
                    # Keys too: a map whose KEYS carry the content was
                    # invisible to a walk that only descended into values. No
                    # report field has model-controlled keys today, so this arm
                    # catches nothing now — it is here so that adding such a
                    # field does not also need someone to remember this walk.
                    yield f"{path}.<key>", key
                    yield from leaves(item, f"{path}.{key}")
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    yield from leaves(item, f"{path}[{index}]")

        # Harness-authored prose is exempt — the caveats from `dimensions.py`,
        # repeated verbatim in every report. The longest is 1167 characters, so
        # a 1200-char bound over all leaves sat 33 characters from reporting a
        # leak on an ordinary edit to that prose, and the predictable response
        # would have been to raise the bound.
        #
        # Exempt BY VALUE, not by path. A `".dimensions." not in path` test
        # also matched `report.cases[N].dimensions.*`, which is per-run
        # territory, so the very field this test's docstring says it closed —
        # raw model arguments in the case row — was re-opened by moving it one
        # level down, 450 characters per tool call per case. The caveats are a
        # known finite set, so membership in that set is the exemption.
        from backend.evaluation.dimensions import DIMENSIONS

        caveats = {dimension.caveat for dimension in DIMENSIONS.values()}
        oversized = [
            (path, len(text))
            for path, text in leaves(report)
            if len(text) >= 500 and text not in caveats
        ]
        assert not oversized, f"unbounded string(s) in the report: {oversized}"
        assert huge not in json.dumps(report), "the whole oversized value survived"

        exempted = {text for _, text in leaves(report) if text in caveats}
        assert exempted, (
            "no caveat appears in the report, so the exemption above is hiding "
            "nothing and should be removed"
        )

        for path, text in leaves(report):
            if path.endswith(".run_error"):
                assert len(text) <= 400, f"{path} is {len(text)} chars"

        # And the RENDERED SIZE, because bounding each leaf is not the same as
        # bounding the report. Returning a list of 80-character chunks from
        # `_truncate` kept every leaf under the bound and put the whole
        # 3200-character model-chosen value back in the file, reconstructable
        # — and `huge not in json.dumps(report)` missed it because JSON writes
        # `", "` between the chunks. Comparing against the same suite run with
        # small values makes any such smuggling show up as size.
        small = ScriptedProvider(
            [
                [("search_graph", {"qeury": "x", "limit": "not-an-int"})],
                [("update_node", {"node_id": "x", "updates": {"summary": "y"}})],
                "it is eval-nope-other.",
            ]
        )
        baseline = json.dumps(
            build_report(
                run_suite(profile, cases=[case], provider_factory=lambda _p: small)
            )
        )
        rendered = json.dumps(report)
        # 300, not 2000. The measured delta between these two runs is about
        # -18 characters, so a 2000 threshold left ~2018 of headroom — enough
        # for a chunked value of ~1900 characters, which is the smuggling
        # route this check exists to close. 300 is still an order of magnitude
        # above the observed noise.
        assert len(rendered) - len(baseline) < 300, (
            f"the oversized run rendered {len(rendered) - len(baseline)} more "
            "characters than the same run with small values, so model-written "
            "content is reaching the report in bulk"
        )

    def test_running_a_case_writes_no_file_but_its_own_temporary_graph(
        self, monkeypatch, profile, case_by_id
    ):
        """
        G2's second clause — "no file the harness writes" — pinned by path.

        Two earlier versions were each one step short. Watching a single
        directory missed an absolute path, so a dump to
        `tempfile.gettempdir()` was invisible; hooking four write functions
        missed `os.open` + `os.write`, which is the same hole the read guard
        had already been widened for. Both are now the same audit event, so
        the two guards are one net instead of two enumerations of different
        length.

        "Its own" is a set of real directories, recorded as the run creates
        them, rather than a prefix: the first attempt excluded everything under
        the system temp dir and so swallowed the very mutation it was written
        for, a dump straight into the temp root.
        """
        import tempfile as tempfile_module

        from backend.evaluation.tests.conftest import (
            ScriptedProvider,
            audit_open_target,
            watch_audit,
        )

        case = case_by_id["skill-adherence-ambiguous-name-halts"]
        # Set, because otherwise the credential arm of the content check below
        # compares against a value no code could have written. Round 9's
        # commit message said this guard reads "the credential, the prompt, the
        # skill text and the answer"; the credential arm did not exist, and
        # adding it without this line would have been worse than leaving it
        # out — a guard that reads as four and checks three.
        monkeypatch.setenv(profile.credential_ref, SENTINEL)

        owned = []
        real_tempdir = tempfile_module.TemporaryDirectory

        class RecordingTemporaryDirectory(real_tempdir):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                owned.append(Path(self.name).resolve())

        monkeypatch.setattr(
            tempfile_module, "TemporaryDirectory", RecordingTemporaryDirectory
        )

        writes = []
        bodies = []
        pending = []

        def watcher(event, args):
            if event != "open":
                return
            decoded = audit_open_target(args)
            if decoded is None:
                return
            resolved, writing = decoded
            if writing:
                writes.append(resolved)
                pending.append(resolved)

        def drain():
            """Read what has been written, while the files still exist."""
            for path in list(pending):
                try:
                    bodies.append(
                        (str(path), path.read_text(encoding="utf-8", errors="replace"))
                    )
                except OSError:
                    continue
                finally:
                    pending.remove(path)

        class DrainingProvider(ScriptedProvider):
            """Reads what has been written so far, from inside the run.

            The provider CALL is the drain point: the factory runs before the
            fixture graph is written, and after `run_suite` returns every
            owned directory is gone — which is why reading afterwards captured
            nothing at all.
            """

            def create_completion(self, *args, **kwargs):
                drain()
                return super().create_completion(*args, **kwargs)

        with watch_audit(watcher):
            run_suite(
                profile,
                cases=[case],
                provider_factory=lambda _p: DrainingProvider(
                    [f"{PROSE_SENTINEL} two nodes share that name, so I stopped."]
                ),
            )
        monkeypatch.undo()

        assert owned, "the run created no temporary directory, so this proves nothing"
        assert writes, "the write hook never fired, so this proves nothing"

        stray = [
            str(path)
            for path in writes
            if not any(path.is_relative_to(directory) for directory in owned)
        ]
        assert not stray, f"the run wrote outside the directory it created: {stray}"

        # Owned is not the same as ephemeral. `TemporaryDirectory(delete=False)`
        # is recorded here as owned and never cleaned up, so a dump of the
        # prompt, the skill text and the answer was admitted by the path check
        # and survived the process.
        surviving = [str(d) for d in owned if d.exists()]
        assert not surviving, f"temporary director(ies) outlived the run: {surviving}"

        # And content, not just location. A path guard says where a file may
        # be; G2 is about what may be in one.
        #
        # Read DURING the run, from `bodies` captured by the watcher. Reading
        # here could not work and did not: the two assertions above require
        # every owned directory to be gone and every write to be inside one,
        # so every `read_text` raised `FileNotFoundError` into an
        # `except OSError: continue` and the value assertion never executed
        # once. Capturing the path list during the run is not the same as
        # reading the bytes during it.
        skills = build_skills_context(case.skill_paths()) or ""
        assert bodies, "no file content was captured, so this proves nothing"
        for path, body in bodies:
            for label, value in (
                ("the credential", SENTINEL),
                ("the prompt", case.prompt),
                ("the skill text", skills),
                ("the answer", PROSE_SENTINEL),
            ):
                assert value not in body, f"{path} carries {label}"

    def test_no_condition_detail_carries_the_answer_or_an_unbounded_argument(
        self, profile
    ):
        """
        Finding 3: the existing assertion ran on a case that produces none of
        these details.

        It drove `tool-call-validity-read-path`, which declares only
        `tool_calls_valid` and makes valid calls — so the details that can
        quote model-written content were never generated, and
        `PROSE_SENTINEL not in payload` could not fire for them. This case
        declares `answer_entities_supported` AND makes a schema-invalid call
        with an oversized argument, so two of them are produced; the
        `final_node_state` quote is covered by its own test below.
        """
        from backend.evaluation.cases import AcceptanceCase, ExpectedBehaviour
        from backend.evaluation.tests.conftest import ScriptedProvider

        huge = "ZZQQ" * 800
        case = AcceptanceCase(
            id="detail-bound-probe",
            dimension="unsupported_entity_reference",
            prompt="which initiative produces the Metadata Handbook?",
            graph="metadata-pilot-small.json",
            expect=ExpectedBehaviour(
                tool_calls_valid=True, answer_entities_supported=True
            ),
            notes=(
                "probe case producing every condition detail that can quote "
                "model-written content, so a report can be checked for it"
            ),
        )
        provider = ScriptedProvider(
            [
                # Schema-invalid, with an oversized value in the arguments.
                [("search_graph", {"qeury": huge, "limit": "not-an-int"})],
                f"{PROSE_SENTINEL} it is eval-initiative-metadata-registry-programme.",
            ]
        )
        result = run_suite(profile, cases=[case], provider_factory=lambda _p: provider)
        payload = json.dumps(build_report(result))
        score = result.scores[0]

        assert not score.passed, "the probe must actually fail to make details"
        assert PROSE_SENTINEL not in payload, "the model's answer reached a report"
        for condition in score.conditions:
            assert len(condition.detail) < 1200, (
                f"{condition.name} detail is {len(condition.detail)} chars"
            )
            assert huge not in condition.detail

    def test_a_model_written_field_value_reaches_a_report_only_bounded(
        self, monkeypatch, profile
    ):
        """
        G2's limit, stated honestly and bounded.

        A condition's detail explains why it failed, so it quotes what the model
        actually wrote — which is the diagnosis, and is model prose. A
        `description` can hold 2000 characters, so an unbounded repr would put a
        paragraph of the model's own writing into a report the docs describe as
        carrying none. The value written here is longer than the bound.

        (`summary` would not do: it caps at 300 characters, so an oversized one
        is rejected and never reaches the graph at all.)
        """
        from backend.evaluation.cases import AcceptanceCase, ExpectedBehaviour
        from backend.evaluation.tests.conftest import ScriptedProvider

        long_prose = "ZZQQ-written-field-marker " * 40
        assert 300 < len(long_prose) < 2000

        case = AcceptanceCase(
            id="bounded-detail-probe",
            dimension="completeness",
            prompt="rewrite the description",
            graph="metadata-pilot-small.json",
            expect=ExpectedBehaviour(
                final_node_state={
                    "eval-resource-metadata-handbook": {
                        "description": "a value the model will not write"
                    }
                }
            ),
            notes=(
                "probe case asserting that a long model-written field value is "
                "abbreviated before it reaches a report, since a condition detail "
                "legitimately quotes what the model wrote"
            ),
        )
        provider = ScriptedProvider(
            [
                [("search_graph", {"query": "Metadata Handbook"})],
                [
                    (
                        "update_node",
                        {
                            "node_id": "eval-resource-metadata-handbook",
                            "updates": {"description": long_prose},
                        },
                    )
                ],
                "done",
            ]
        )
        score = run_case(case, profile, provider_factory=lambda _p: provider)

        detail = next(
            c.detail for c in score.conditions if c.name == "final_node_state"
        )
        assert not score.passed
        # The write landed, so the detail quotes it — abbreviated.
        assert "ZZQQ-written-field-marker" in detail, detail
        assert "chars)" in detail, f"the oversized value was not abbreviated: {detail}"
        assert len(detail) < 400, len(detail)
        assert detail.count("ZZQQ-written-field-marker") < 10

    @pytest.mark.parametrize(
        "site", ["provider_build", "chat_path", "chat_raises", "fixture_setup"]
    )
    def test_the_harnesss_own_log_lines_are_scrubbed_of_all_three_values(
        self, monkeypatch, tmp_path, caplog, profile, case_by_id, site
    ):
        """
        The report half of round 4 was pinned; the log half was pinned by
        nothing.

        Deleting `_scrub`'s body — or just the credential entry from the list
        it is given — left the whole suite green while a WARNING carried the
        resolved key. `_scrub`, `caplog` and `<redacted>` appeared in no test.

        Scoped to the harness's own logger on purpose. `ChatProcessor` logs a
        swallowed exception at ERROR before the harness sees it, which this
        function cannot reach and the docs say so: the claim being pinned is
        that the lines the harness writes are clean, not that every line in the
        process is.
        """
        import logging

        from backend.ui import ChatService
        from backend.evaluation.tests.conftest import ScriptedProvider

        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        case = case_by_id["skill-adherence-ambiguous-name-halts"]
        graphs_dir = None
        # The raised message carries all three values. It used to carry only
        # the credential and the prompt, so `assert "ACTIVE SKILL
        # INSTRUCTIONS" not in ours` passed at all four sites without the skill
        # text ever being in the message — one of the three values `_scrub`'s
        # docstring promises, pinned by nothing.
        skills = build_skills_context(case.skill_paths()) or ""
        # The WHOLE skill text, not a slice. `_scrub` matches by exact value,
        # and its docstring says so — a truncated echo is explicitly outside
        # what it can remove. Asserting against a slice would pin an
        # impossible property; asserting against the whole value pins the one
        # the function actually promises.
        boom = (
            f"401 with Authorization: Bearer {SENTINEL} on {case.prompt} "
            f"while sending {skills}"
        )

        if site == "provider_build":

            def factory(_profile):
                raise RuntimeError(boom)

        elif site == "chat_path":

            def factory(_profile):
                provider = ScriptedProvider(["x"])

                def explode(*_args, **_kwargs):
                    raise RuntimeError(boom)

                provider.create_completion = explode
                return provider

        elif site == "chat_raises":

            def explode(*_args, **_kwargs):
                raise RuntimeError(boom)

            monkeypatch.setattr(ChatService, "process_message", explode)

            def factory(_profile):
                return ScriptedProvider(["x"])

        else:  # fixture_setup
            # Not a malformed graph: `json.loads` runs before the try this site
            # is inside, so a bad file propagates out of run_case instead of
            # reaching it. The service build is what that except wraps.
            import backend.evaluation.runner as runner_module

            def explode(*_args, **_kwargs):
                raise RuntimeError(boom)

            monkeypatch.setattr(runner_module, "_build_chat_service", explode)

            def factory(_profile):
                return ScriptedProvider(["x"])

        with caplog.at_level(logging.DEBUG, logger="backend.evaluation.runner"):
            run_suite(
                profile, cases=[case], provider_factory=factory, graphs_dir=graphs_dir
            )

        ours = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name == "backend.evaluation.runner"
        )
        assert ours, f"{site} wrote no harness log line at all"
        assert SENTINEL not in ours, f"{site} logged the credential"
        assert case.prompt not in ours, f"{site} logged the prompt"
        assert "ACTIVE SKILL INSTRUCTIONS" not in ours, f"{site} logged the skill text"
        assert "<redacted>" in ours, f"{site} redacted nothing"

    def test_the_provider_seam_is_not_reachable_from_an_http_request(self):
        """
        The seam this PR adds to `process_message` must stay harness-only.

        `llm_provider` lets a caller hand in a provider object, which is the
        whole point for the harness and would be a remote code path if a
        request body could set it. It is unreachable today because the REST
        handler forwards explicit keyword arguments and `ChatRequest` has no
        such field — and nothing pinned either half, so a refactor to
        `**request.model_dump()` would open it silently.
        """
        import ast
        import inspect

        from backend.ui import chat_service as chat_service_module
        from backend.ui import rest_api

        # Both routes to the chat service: the chat endpoint calls
        # `process_message` directly, and `/chat/simple` reaches it through
        # `ChatService.process_chat_request`. The docstring said "the REST
        # handler", singular, for two handlers.
        #
        # Scoped to the REQUEST-FACING functions, not to whole modules:
        # `ChatService.process_message` forwarding `llm_provider` into
        # `chat_logic` IS the harness seam and must keep working. What must
        # never happen is a request-shaped value reaching it.
        # `ast.AsyncFunctionDef` as well as `ast.FunctionDef`: the REST
        # handlers are `async def`, so matching only the sync node type made
        # this half of the scan visit nothing at all. Hence the positive
        # control below — a scan that finds no function to scan is not a pass.
        visited = set()

        def calls_in(module, function_names):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or (
                    node.name not in function_names
                ):
                    continue
                visited.add(f"{module.__name__}.{node.name}")
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        yield module.__name__, node.name, inner

        forwarding = []
        request_facing = [
            (rest_api, {"chat", "chat_simple"}),
            (chat_service_module, {"process_chat_request"}),
        ]
        for module, names in request_facing:
            for module_name, function, call in calls_in(module, names):
                target = ast.unparse(call.func)
                if not target.endswith(("process_message", "process_chat_request")):
                    continue
                for keyword in call.keywords:
                    if keyword.arg is None:
                        forwarding.append(
                            f"{module_name}.{function}: **{ast.unparse(keyword.value)}"
                        )
                    elif keyword.arg == "llm_provider":
                        forwarding.append(f"{module_name}.{function}: llm_provider=")
        assert visited == {
            "backend.ui.rest_api.chat",
            "backend.ui.rest_api.chat_simple",
            "backend.ui.chat_service.process_chat_request",
        }, f"the scan did not reach every request-facing function: {visited}"
        assert forwarding == [], (
            "a request-facing function forwards unpacked or explicit provider "
            f"arguments into the chat service: {forwarding}"
        )

        # Unconditional: behind an `if`, a rename deleted the assertion rather
        # than failing it. Both request models, since both reach the service.
        for name in ("ChatRequest", "SimpleChatRequest"):
            model = getattr(rest_api, name)
            assert "llm_provider" not in model.model_fields, name

    def test_no_harness_module_state_holds_the_credential_after_a_run(
        self, monkeypatch, profile, case_by_id
    ):
        """
        G1 says "never stored", and nothing inspected harness module state.

        Caching the resolved value in a module-level dict survived the whole
        suite. The credential then lives in process memory for as long as the
        interpreter does — and a rotation mid-suite is silently ignored, so the
        cache is a correctness bug as well as an exposure. Asserted by looking
        at the module rather than inferring from behaviour: the value is either
        reachable from module state or it is not.

        Within limits worth naming: the walk covers str, dict, list, tuple and
        set to depth three, so a credential held as an attribute of a
        module-level object, in a function default or in a closure cell would
        not be seen. It catches the shape a cache actually takes, not every
        shape one could take.
        """
        import sys

        from backend.evaluation.tests.conftest import ScriptedProvider

        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        run_suite(
            profile,
            cases=[case_by_id["skill-adherence-ambiguous-name-halts"]],
            provider_factory=lambda _p: ScriptedProvider(
                ["two nodes share that name, so I stopped."]
            ),
        )

        def reachable(value, depth=0):
            if depth > 3:
                return False
            if isinstance(value, str):
                return SENTINEL in value
            if isinstance(value, dict):
                return any(
                    reachable(item, depth + 1)
                    for item in list(value.keys()) + list(value.values())
                )
            if isinstance(value, (list, tuple, set, frozenset)):
                return any(reachable(item, depth + 1) for item in value)
            return False

        # Every module in the package plus the CLI, derived rather than
        # listed: the three names this loop used to spell out missed a cache in
        # `cases.py`, which is the same module-level dict of strings the walk
        # was written for, simply in a fourth module. A new module is now
        # covered by existing code rather than by someone remembering.
        watched = [
            module
            for name, module in list(sys.modules.items())
            if module is not None
            and (
                name == "backend.evaluation"
                or name.startswith("backend.evaluation.")
                or name == "scripts.run_skill_eval"
            )
            and ".tests" not in name
        ]
        assert len(watched) >= 5, (
            f"too few modules watched: {sorted(m.__name__ for m in watched)}"
        )
        for module in watched:
            holders = [
                name
                for name, value in vars(module).items()
                if not name.startswith("__") and reachable(value)
            ]
            assert not holders, (
                f"{module.__name__} module state holds the credential: {holders}"
            )

    def test_no_log_record_from_a_SUCCESSFUL_run_carries_any_of_the_three(
        self, monkeypatch, profile, case_by_id
    ):
        """
        The four scrubbed sites are all failure sites, so the common path was
        unwatched.

        Adding `logger.info("... (credential %s)", secrets[0])` on the success
        path left the whole suite green, because the only `caplog` test forces
        an error at each of the four handling sites and a line guarded by
        `if not transcript.run_error` never appears in its records. So the
        property is asserted over the LOGGER for a run that succeeds, rather
        than over a list of known call sites — which is the same enumeration
        mistake this harness has now made with read entry points, socket
        methods, write entry points, path types and module names.
        """
        import logging

        from backend.evaluation.tests.conftest import ScriptedProvider

        case = case_by_id["skill-adherence-ambiguous-name-halts"]
        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        skills = build_skills_context(case.skill_paths()) or ""
        forbidden = {
            "credential": SENTINEL,
            "prompt": case.prompt,
            "skill text": "ACTIVE SKILL INSTRUCTIONS",
        }

        offenders = []

        class Tripwire(logging.Handler):
            def emit(self, record):
                try:
                    text = record.getMessage()
                except Exception:  # pragma: no cover - defensive
                    return
                for label, value in forbidden.items():
                    if value and value in text:
                        offenders.append((label, record.name, text[:160]))

        tripwire = Tripwire()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(tripwire)
        root.setLevel(logging.DEBUG)
        try:
            result = run_suite(
                profile,
                cases=[case],
                provider_factory=lambda _p: ScriptedProvider(
                    ["two nodes share that name, so I stopped."]
                ),
            )
        finally:
            root.removeHandler(tripwire)
            root.setLevel(previous)

        assert not result.scores[0].run_error, "this probe must drive a SUCCESSFUL run"
        assert skills, "the case injects no skill text, so one value is unchecked"
        assert not offenders, f"log records carried protected values: {offenders}"

    @pytest.mark.parametrize(
        ("length", "redacted"),
        [(8, False), (9, True)],
    )
    def test_the_scrub_floor_is_eight_characters_in_both_directions(
        self, length, redacted
    ):
        """
        The floor is a judgement, so it is pinned rather than left to drift.

        Pinned in both directions because it buys an exposure rather than
        closing one: a credential of eight characters or fewer is NOT
        redacted, and `resolve_credential` accepts any non-empty value, so
        nothing stops one being that short. (An earlier version of this
        docstring gave the likelihood argument — "below the floor a value is
        likelier to occur in unrelated text" — which `_scrub` retracted as
        false for a short credential. Repeating it here handed a reader the
        retracted justification.)
        """
        from backend.evaluation.runner import _scrub

        secret = "S" * length
        out = _scrub(f"failed on {secret} here", [secret])
        assert ("<redacted>" in out) is redacted
        assert (secret in out) is not redacted

    @pytest.mark.parametrize(
        ("site", "expected"),
        [
            ("provider_build", "provider unavailable: ValueError"),
            ("chat_path", "provider call failed: ValueError"),
            ("chat_raises", "chat path failed: ValueError"),
        ],
    )
    def test_a_run_error_names_its_stage_and_the_exception_class(
        self, monkeypatch, tmp_path, profile, case_by_id, site, expected
    ):
        """
        The docs promise `provider call failed: APIConnectionError`.

        Nothing pinned either half. Deleting `ProviderCall.error_type` left the
        whole suite green while every provider failure reported `provider call
        failed: error`, because the aggregation falls back to a literal when
        the class is missing — so the field the report quotes instead of the
        raiser's message was itself unprotected. And one site emitted a bare
        class with no stage at all, against the sentence above.

        `ValueError` rather than `RuntimeError` so the assertion fails if the
        class stops being read from the exception and starts being a constant.
        """
        from backend.ui import ChatService
        from backend.evaluation.tests.conftest import ScriptedProvider

        monkeypatch.setenv(profile.credential_ref, "unused-by-a-mock")
        case = case_by_id["skill-adherence-ambiguous-name-halts"]

        if site == "provider_build":

            def factory(_profile):
                raise ValueError("detail")

        elif site == "chat_path":

            def factory(_profile):
                provider = ScriptedProvider(["x"])

                def explode(*_args, **_kwargs):
                    raise ValueError("detail")

                provider.create_completion = explode
                return provider

        else:

            def explode(*_args, **_kwargs):
                raise ValueError("detail")

            monkeypatch.setattr(ChatService, "process_message", explode)

            def factory(_profile):
                return ScriptedProvider(["x"])

        result = run_suite(profile, cases=[case], provider_factory=factory)
        assert result.scores[0].run_error == expected

    @pytest.mark.parametrize(
        "site",
        ["provider_build", "chat_path", "chat_raises", "fixture_setup"],
    )
    def test_no_run_error_carries_the_credential_the_prompt_or_the_skill_text(
        self, monkeypatch, tmp_path, profile, case_by_id, site
    ):
        """
        `run_error` reaches a report verbatim, and nothing asserted its CONTENT.

        Only that one was set, and what it was prefixed with. Each construction
        site wraps an exception message, and an exception can carry whatever the
        raiser put in it — a resolved credential, the case prompt, or the whole
        injected skill text, which G2 names explicitly.

        Which site each name reaches is worth stating, because an earlier
        version of this docstring claimed three and drove one of them twice.
        `ChatProcessor.process_message` swallows every exception and returns it
        as the assistant's reply, so `chat_path` — a provider that raises — does
        NOT reach the except around `process_message`; it reaches the
        provider-call aggregation that reads the recorded call. `chat_raises`
        makes `process_message` itself raise, which is the only way into that
        except, and the site that had no stage prefix for exactly as long as no
        test drove it.
        """
        from backend.evaluation.cases import load_cases
        from backend.evaluation.tests.conftest import ScriptedProvider

        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        case = case_by_id["skill-adherence-ambiguous-name-halts"]
        graphs_dir = None

        if site == "provider_build":

            def factory(_profile):
                raise RuntimeError(f"boom {SENTINEL} {PROSE_SENTINEL}")

        elif site == "chat_path":

            def factory(_profile):
                provider = ScriptedProvider(["x"])

                def explode(*_args, **_kwargs):
                    raise RuntimeError(f"boom {SENTINEL} {PROSE_SENTINEL}")

                provider.create_completion = explode
                return provider

        elif site == "chat_raises":
            from backend.ui import ChatService

            def explode(*_args, **_kwargs):
                raise RuntimeError(f"boom {SENTINEL} {PROSE_SENTINEL}")

            monkeypatch.setattr(ChatService, "process_message", explode)

            def factory(_profile):
                return ScriptedProvider(["x"])

        else:  # fixture_setup
            (tmp_path / case.graph).write_text(
                json.dumps({"nodes": [{"id": "x"}], "edges": []}), encoding="utf-8"
            )
            graphs_dir = tmp_path

            def factory(_profile):
                return ScriptedProvider(["x"])

        result = run_suite(
            profile,
            cases=[case],
            provider_factory=factory,
            graphs_dir=graphs_dir,
        )
        payload = json.dumps(build_report(result))
        score = result.scores[0]

        assert score.run_error is not None, f"{site} produced no run_error"
        assert SENTINEL not in payload, f"{site} leaked the credential"
        assert PROSE_SENTINEL not in payload, f"{site} leaked model-side text"
        assert "ACTIVE SKILL INSTRUCTIONS" not in payload, (
            f"{site} leaked the injected skill text"
        )
        assert case.prompt not in payload, f"{site} leaked the case prompt"
        rendered = json.loads(payload)["cases"][0]["run_error"]
        assert rendered is None or len(rendered) <= 400, len(rendered)
        assert len(load_cases()) == 9  # the shipped set is untouched by this probe

    def test_the_transcript_never_holds_the_credential(self, monkeypatch, profile):
        from backend.evaluation.tests.conftest import ScriptedProvider
        from backend.evaluation.transcript import RecordingProvider

        monkeypatch.setenv(profile.credential_ref, SENTINEL)
        recorder = RecordingProvider(ScriptedProvider(["done"]))
        recorder.create_completion([], "system", [{"name": "search_graph"}])
        blob = repr(recorder.transcript)
        assert SENTINEL not in blob


class TestTheSuiteRunsWithNoCredentialAtAll:
    def test_the_whole_shipped_suite_scores_with_no_provider_key_in_the_environment(
        self, monkeypatch, profile, case_by_id
    ):
        """
        The property that lets this suite run in CI.

        Every provider variable is cleared, including the ones the legacy
        single-provider path would pick up, and the suite still runs end to end
        against a scripted model.
        """
        from backend.evaluation.tests.conftest import ScriptedProvider

        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_BASE_URL",
            "LLM_PROVIDER",
            profile.credential_ref,
        ):
            monkeypatch.delenv(name, raising=False)

        result = run_suite(
            profile,
            cases=list(case_by_id.values()),
            provider_factory=lambda _p: ScriptedProvider(
                [[("search_graph", {"query": "x"})], "done"]
            ),
        )
        assert result.total == len(case_by_id)
        assert all(score.run_error is None for score in result.scores)
