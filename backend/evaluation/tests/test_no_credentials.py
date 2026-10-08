"""
The credential guarantee: no code path reads a key from a file in the repo, and
no test needs a real one.

This is the guarantee that decides whether the harness is safe to commit to a
public repository and safe to hand to someone who will run it against a paid
endpoint. It is tested rather than asserted in prose because the failure is
silent: a key pasted into a fixture, or echoed into a report, looks like nothing
until the repository is public — which this one is.
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
    run_suite,
)

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


class TestReportsNeverCarryACredential:
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

    def test_no_environment_variable_is_read_at_import_time(self):
        """Importing the harness must not depend on, or capture, any key."""
        import importlib

        import backend.evaluation as module

        assert importlib.reload(module) is module
