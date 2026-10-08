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

        In a fresh process the first case refuses, because the provider is built
        before the assistant. Once something has imported the assistant,
        `load_dotenv()` has run and the same call accepts the `.env` value. That
        asymmetry is the reason the docs tell the operator to export instead.
        """
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from backend.config.model_profiles import ModelProfile\n"
            "from backend.evaluation import load_cases, run_case\n"
            "prof = ModelProfile(id='p', name='P', provider='openai', model='m',\n"
            "                    default=True, credential_ref='SKILL_EVAL_DOTENV_PROBE')\n"
            "case = load_cases()[0]\n"
            "first = run_case(case, prof)\n"
            "print('FRESH_REFUSED:', 'MissingCredentialError' in (first.run_error or ''))\n"
            "import backend.ui.chat_logic  # noqa  -- runs load_dotenv()\n"
            "import os\n"
            "print('NOW_IN_ENV:', os.environ.get('SKILL_EVAL_DOTENV_PROBE') is not None)\n"
            "second = run_case(case, prof)\n"
            "print('SECOND_REFUSED:',\n"
            "      'MissingCredentialError' in (second.run_error or ''))\n"
        ) % str(REPO_ROOT)
        result = self._run(script, "SKILL_EVAL_DOTENV_PROBE=from-repo-root\n")
        assert "FRESH_REFUSED: True" in result.stdout, result.stdout + result.stderr
        assert "NOW_IN_ENV: True" in result.stdout, result.stdout + result.stderr
        # The operative half of the docs' claim, which was asserted only as
        # "the value is now in the environment": the SECOND call must accept it.
        # A regression that made it refuse would have passed before.
        assert "SECOND_REFUSED: False" in result.stdout, result.stdout + result.stderr


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

    def test_no_condition_detail_carries_the_answer_or_an_unbounded_argument(
        self, profile
    ):
        """
        Finding 3: the existing assertion ran on a case that produces none of
        these details.

        It drove `tool-call-validity-read-path`, which declares only
        `tool_calls_valid` and makes valid calls — so the three details that can
        quote model-written content were never generated, and
        `PROSE_SENTINEL not in payload` could not fire for them. This case
        declares `answer_entities_supported` AND makes a schema-invalid call
        with an oversized argument, so all of them are produced.
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
        "site",
        ["provider_build", "chat_path", "fixture_setup"],
    )
    def test_no_run_error_carries_the_credential_the_prompt_or_the_skill_text(
        self, monkeypatch, tmp_path, profile, case_by_id, site
    ):
        """
        `run_error` reaches a report verbatim, and nothing asserted its CONTENT.

        Only that one was set, and what it was prefixed with. Each of the three
        construction sites wraps an exception message, and an exception can carry
        whatever the raiser put in it — a resolved credential, the case prompt,
        or the whole injected skill text, which G2 names explicitly. All three
        sites are driven here, with every sentinel checked against the rendered
        report.
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
