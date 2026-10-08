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


class TestADotEnvFileDoesNotSupplyTheCredential:
    """
    Pinned because the documentation got this wrong in both directions.

    First it claimed nothing but the shell is read; then, correcting that, it
    claimed a repo-root `.env` would satisfy a `credential_ref` and `.env.example`
    told the operator so. Neither is true: the harness resolves the variable
    before anything imports the assistant, so `load_dotenv()` has not run. An
    operator following the wrong version puts a key on disk and still gets the
    run refused.
    """

    def test_run_case_refuses_when_only_a_dot_env_carries_the_key(
        self, monkeypatch, tmp_path, profile, case_by_id
    ):
        import os

        monkeypatch.delenv(profile.credential_ref, raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            f"{profile.credential_ref}={SENTINEL}\n", encoding="utf-8"
        )

        score = run_case(case_by_id["tool-call-validity-read-path"], profile)

        assert score.run_error is not None
        assert "MissingCredentialError" in score.run_error
        assert profile.credential_ref in score.run_error
        # And the value never made it into the environment on this path.
        assert os.environ.get(profile.credential_ref) is None

    def test_the_cli_refuses_when_only_a_dot_env_carries_the_key(
        self, monkeypatch, tmp_path, capsys
    ):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from scripts.run_skill_eval import main

        monkeypatch.delenv("SKILL_EVAL_DOTENV_PROBE", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            f"SKILL_EVAL_DOTENV_PROBE={SENTINEL}\n", encoding="utf-8"
        )
        profiles = tmp_path / "providers.json"
        profiles.write_text(
            json.dumps(
                [
                    {
                        "id": "probe",
                        "name": "Probe",
                        "provider": "openai",
                        "model": "m",
                        "default": True,
                        "credential_ref": "SKILL_EVAL_DOTENV_PROBE",
                    }
                ]
            )
        )

        assert main(["--profiles", str(profiles)]) == 2
        err = capsys.readouterr().err
        assert "SKILL_EVAL_DOTENV_PROBE is not set" in err
        assert SENTINEL not in err


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
        from backend.evaluation.runner import run_case
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
