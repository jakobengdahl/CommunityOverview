"""
Tests for the operator-facing entry point, scripts/run_skill_eval.py.

It had none, and three of this harness's credential guarantees live in it: that
there is no way to pass a key as an argument, that an unset variable is a hard
stop rather than a fallback, and that the report it writes names the dimension
the harness does not score. A guarantee stated only in a docstring is a
guarantee until someone edits the file.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_skill_eval import (  # noqa: E402
    check_credentials,
    main,
    print_dimensions,
)

SENTINEL = "sk-test-SENTINEL-must-never-be-written-anywhere-0123456789"


@pytest.fixture
def profile_file(tmp_path) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "probe",
                    "name": "Probe",
                    "provider": "openai",
                    "model": "test-model",
                    "default": True,
                    "credential_ref": "SKILL_EVAL_PROBE_KEY",
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


class TestTheSuiteCannotReachTheNetwork:
    """
    G1's egress clause, enforced rather than asserted.

    The conftest `no_network` fixture is the only thing stopping a provider
    built for real from calling out. It exists because the claim "no test makes
    a network call" was false for several rounds: a provider substitution that
    patched a module attribute could not take effect, because `run_suite` had
    captured the factory as a default argument, and the suite quietly made 27
    outbound connections while documenting that it made none.
    """

    def test_the_egress_guard_is_autouse_and_has_no_opt_out(self):
        from backend.evaluation.tests import conftest

        fixture = conftest.no_network
        # pytest 9 exposes the decorator's arguments on the fixture definition;
        # older versions used a `_pytestfixturefunction` attribute. Accept either
        # rather than pinning this suite to one pytest line.
        marker = getattr(
            fixture,
            "_fixture_function_marker",
            getattr(fixture, "_pytestfixturefunction", None),
        )
        assert marker is not None, "no_network is no longer a fixture"
        assert marker.autouse is True, (
            "the egress guard must be autouse; a guard a test can decline is not "
            "a guarantee"
        )

    def test_an_outbound_connection_inside_this_suite_fails_the_test(self):
        """The guard bites, with a message naming the address."""
        import socket

        with pytest.raises(AssertionError, match="attempted an outbound connection"):
            socket.socket().connect(("example.invalid", 443))

    def test_the_run_helpers_resolve_their_provider_factory_at_call_time(self):
        """
        The late-binding bug that made the substitution inert.

        A function object captured as a default argument cannot be replaced by
        patching the module attribute, so this must stay None-defaulted.
        """
        import inspect

        from backend.evaluation.runner import run_case, run_suite

        for function in (run_case, run_suite):
            default = inspect.signature(function).parameters["provider_factory"].default
            assert default is None, (
                f"{function.__name__} binds its provider factory as a default "
                "argument, which makes substituting the module attribute inert"
            )


class TestNoWayToPassAKeyAsAnArgument:
    def test_the_parser_exposes_no_credential_bearing_option(self, capsys):
        """
        The script's docstring says there is deliberately no --api-key flag,
        because a key on a command line lands in shell history and in the
        process table. Nothing checked that, so adding one was invisible.
        """
        with pytest.raises(SystemExit):
            main(["--help"])
        help_text = capsys.readouterr().out.lower()
        for forbidden in ("--api-key", "--key", "--token", "--secret", "--password"):
            assert forbidden not in help_text

    def test_an_unknown_key_flag_is_rejected(self, capsys, profile_file):
        with pytest.raises(SystemExit):
            main(["--profiles", str(profile_file), "--api-key", SENTINEL])


class TestAnUnsetCredentialIsAHardStop:
    def test_it_does_not_fall_back_to_the_ambient_provider_key(
        self, monkeypatch, profile_file, capsys
    ):
        """
        The fallback G1 forbids: a run billed to a key the operator did not
        choose, and attributed to the endpoint they did.
        """
        monkeypatch.delenv("SKILL_EVAL_PROBE_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
        monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)

        assert main(["--profiles", str(profile_file)]) == 2

        err = capsys.readouterr().err
        assert "SKILL_EVAL_PROBE_KEY" in err
        assert SENTINEL not in err

    def test_check_credentials_reports_the_variable_by_name(
        self, monkeypatch, profile_file
    ):
        from backend.evaluation.runner import load_profiles

        monkeypatch.delenv("SKILL_EVAL_PROBE_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
        missing = check_credentials(load_profiles(profile_file))
        assert missing == [("probe", "SKILL_EVAL_PROBE_KEY")]

    def test_a_profile_with_no_credential_ref_at_all_is_reported(self, tmp_path):
        from backend.evaluation.runner import load_profiles

        path = tmp_path / "p.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "id": "no-ref",
                        "name": "No ref",
                        "provider": "openai",
                        "model": "m",
                        "default": True,
                    }
                ]
            )
        )
        missing = check_credentials(load_profiles(path))
        assert missing[0][0] == "no-ref"


class TestTheReportDeclaresWhatIsNotScored:
    def test_the_dimensions_listing_marks_hallucination_unscored(self, capsys):
        print_dimensions()
        out = capsys.readouterr().out
        assert "hallucination  [none]" in out
        assert "deliberately not scored" in out
        # And every dimension is listed, so none can quietly drop out.
        from backend.evaluation import DIMENSIONS

        for key in DIMENSIONS:
            assert key in out

    def test_a_written_report_names_the_unscored_dimension(
        self, monkeypatch, profile_file, tmp_path, capsys
    ):
        """
        What an operator keeps. A report that stopped declaring hallucination
        unscored would read as a clean sweep of every dimension.
        """
        from backend.evaluation.tests.conftest import ScriptedProvider

        monkeypatch.setenv("SKILL_EVAL_PROBE_KEY", SENTINEL)
        monkeypatch.setattr(
            "backend.evaluation.runner.default_provider_factory",
            lambda _profile: ScriptedProvider(
                [[("search_graph", {"query": "x"})], "done"]
            ),
        )
        out_file = tmp_path / "report.json"
        assert main(["--profiles", str(profile_file), "--out", str(out_file)]) == 0

        document = json.loads(out_file.read_text(encoding="utf-8"))

        # This substitution used to be inert, because run_suite had captured
        # default_provider_factory as a default argument — so every case ran
        # against the real provider, failed, and this test still passed on
        # assertions that are true of a suite which never reached a model.
        # Asserting the run actually happened is what keeps that from recurring.
        summary = document["reports"][0]["summary"]
        assert summary["run_errors"] == 0, (
            "cases never reached the scripted model — the provider substitution "
            "is not taking effect"
        )
        assert summary["passed"] > 0, summary
        assert document["unscored_dimensions"] == ["hallucination"]
        assert document["reports"][0]["summary"]["unscored_dimensions"] == [
            "hallucination"
        ]
        assert SENTINEL not in out_file.read_text(encoding="utf-8")
        # The SUCCESS path's stderr — what lands in a CI log. Only the refusal
        # path was asserted, and it returns before the progress line is printed.
        streams = capsys.readouterr()
        assert SENTINEL not in streams.err
        assert SENTINEL not in streams.out

    def test_an_unknown_profile_id_is_rejected_before_any_run(
        self, monkeypatch, profile_file
    ):
        monkeypatch.setenv("SKILL_EVAL_PROBE_KEY", SENTINEL)
        with pytest.raises(SystemExit):
            main(["--profiles", str(profile_file), "--profile-id", "no-such-profile"])


class TestNoCodePathReadsACredentialFromAFile:
    """
    G1's first clause, which no test asserted.

    The repository scan in test_no_credentials.py looks for key-shaped
    *literals*; a line that READS a file into os.environ, or hands a file's
    contents to a provider as an api_key, carries no literal and passed it.
    """

    SCANNED = sorted(
        [*(REPO_ROOT / "backend" / "evaluation").rglob("*.py")]
        + [REPO_ROOT / "scripts" / "run_skill_eval.py"]
    )

    def test_no_module_assigns_a_file_read_into_the_environment(self):
        import ast

        offenders = []
        for path in self.SCANNED:
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # os.environ[...] = <anything>  — the harness only ever READS it.
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript) and ast.unparse(
                            target.value
                        ).endswith("environ"):
                            offenders.append(f"{path.name}:{node.lineno}")
                # os.environ.setdefault(...) / .update(...)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = ast.unparse(node.func.value)
                    if owner.endswith("environ") and node.func.attr in (
                        "setdefault",
                        "update",
                        "pop",
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"harness writes to os.environ at {offenders}"

    def test_no_module_passes_a_file_read_as_a_credential(self):
        """
        Keyword AND positional.

        This inspected `node.keywords` only, so
        `create_provider_from_profile(profile, keyfile.read_text())` — whose
        second positional parameter IS `api_key_override`, documented to take
        precedence over `credential_ref` — was invisible to it.
        """
        import ast

        # `json.load(open(...))` matched none of the `.read*(` forms, which is
        # how the proven leak got its secret in the first place.
        readers = ("read_text", "read_bytes", "readline", "read")
        openers = ("open(", "json.load(", "json.loads(", "loadtxt(", "load(")

        def reads_a_file(source: str) -> bool:
            return any(f".{reader}(" in source for reader in readers) or any(
                opener in source for opener in openers
            )

        offenders = []
        for path in self.SCANNED:
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg not in ("api_key", "api_key_override", "credential"):
                        continue
                    source = ast.unparse(keyword.value)
                    if reads_a_file(source):
                        offenders.append(f"{path.name}:{node.lineno}: {source}")
                # Any positional argument of a provider constructor.
                callee = ast.unparse(node.func)
                if callee.endswith(
                    (
                        "create_provider_from_profile",
                        "create_provider",
                        "OpenAI",
                        "Anthropic",
                    )
                ):
                    for argument in node.args:
                        source = ast.unparse(argument)
                        if reads_a_file(source):
                            offenders.append(f"{path.name}:{node.lineno}: {source}")
        assert not offenders, f"a file's contents reach a credential at {offenders}"

    def test_no_module_supplies_an_api_key_override_at_all(self):
        """
        Positional or keyword, from a file or anywhere else.

        The mechanism, not one filename: `api_key_override` is the documented
        way to beat `credential_ref`, so the harness must never pass a second
        argument to `create_provider_from_profile` by any route.
        """
        import ast

        offenders = []
        for path in self.SCANNED:
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not ast.unparse(node.func).endswith("create_provider_from_profile"):
                    continue
                if len(node.args) > 1 or any(
                    kw.arg == "api_key_override" for kw in node.keywords
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"api_key_override supplied at {offenders}"

    def test_the_built_providers_outgoing_credential_is_the_environment_value(
        self, monkeypatch, tmp_path
    ):
        """
        The outcome, not the inputs — which is what G1 actually claims.

        The guards below all check an *input* to the construction: no override
        argument, no file read flowing into one, no key-shaped literal. None of
        them sees `provider.client.api_key = <file contents>` AFTER the
        provider is built, and the SDK client's attribute is writable. That
        route was proven to put a file-sourced key on the wire with the whole
        suite green. Asserting what the provider ends up holding closes the
        class rather than the instance.
        """
        from backend.config.model_profiles import ModelProfile
        from backend.evaluation.runner import default_provider_factory

        monkeypatch.chdir(tmp_path)
        for name in ("eval_credentials.json", ".eval_key", ".env"):
            (tmp_path / name).write_text(
                '{"key": "FILE-SOURCED-KEY"}', encoding="utf-8"
            )
        monkeypatch.setenv("SKILL_EVAL_OUTCOME_PROBE", "ENV-SOURCED-KEY")

        profile = ModelProfile(
            id="probe",
            name="Probe",
            provider="openai",
            model="m",
            default=True,
            credential_ref="SKILL_EVAL_OUTCOME_PROBE",
        )
        provider = default_provider_factory(profile)

        # Whatever the provider will send must be exactly the environment value.
        assert provider.client.api_key == "ENV-SOURCED-KEY"
        assert getattr(provider, "api_key", None) in (None, "ENV-SOURCED-KEY")

    def test_no_module_assigns_a_credential_onto_a_built_provider(self):
        """
        The static half of the same hole: assignment, not argument passing.

        The scans below look at call arguments. `provider.client.api_key = …`
        is an assignment to an attribute, so none of them sees it.
        """
        import ast

        offenders = []
        for path in self.SCANNED:
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in (
                        "api_key",
                        "api_key_override",
                        "auth_token",
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno}: {ast.unparse(target)}"
                        )
        assert not offenders, f"credential assigned onto an object at {offenders}"

    def test_the_harness_never_overrides_the_profiles_credential_at_runtime(
        self, monkeypatch, tmp_path
    ):
        """
        The behavioural half, asserting the mechanism rather than a filename.

        The previous version planted exactly `backend/evaluation/.eval_key`, so
        a code path reading any other name slipped through. This records every
        provider construction the harness performs and asserts none of them
        carried an override.
        """
        import backend.evaluation.runner as runner_module
        from backend.evaluation import load_cases
        from backend.evaluation.tests.conftest import ScriptedProvider

        monkeypatch.chdir(tmp_path)
        for name in (".eval_key", ".eval_credential", ".env", "api_key.txt", "key"):
            (tmp_path / name).write_text(SENTINEL, encoding="utf-8")

        calls = []

        def record(profile, api_key_override=None, *args, **kwargs):
            calls.append(api_key_override)
            return ScriptedProvider([[("search_graph", {"query": "x"})], "done"])

        monkeypatch.setattr(
            runner_module, "create_provider_from_profile", record, raising=True
        )
        monkeypatch.setenv("EVAL_HARNESS_TEST_KEY", SENTINEL)

        from backend.config.model_profiles import ModelProfile

        profile = ModelProfile(
            id="probe",
            name="Probe",
            provider="openai",
            model="m",
            default=True,
            credential_ref="EVAL_HARNESS_TEST_KEY",
        )
        runner_module.run_case(load_cases()[0], profile)

        assert calls, "no provider was constructed, so nothing was asserted"
        assert all(override is None for override in calls), calls

    def test_a_key_file_planted_beside_the_harness_is_not_picked_up(
        self, monkeypatch, tmp_path, profile
    ):
        """
        The behavioural half: a credential sitting in the most obvious place
        anyone would put one must not satisfy a profile.

        Written under tmp_path with the cwd moved there, not into the
        repository: an interrupted run would otherwise leave a key-shaped file
        in a public checkout, which is the very thing this file exists to
        prevent.
        """
        from backend.config.model_profiles import MissingCredentialError
        from backend.evaluation.runner import default_provider_factory

        monkeypatch.chdir(tmp_path)
        for name in (".eval_key", ".env", "api_key.txt"):
            (tmp_path / name).write_text(SENTINEL, encoding="utf-8")

        monkeypatch.delenv(profile.credential_ref, raising=False)
        with pytest.raises(MissingCredentialError, match=profile.credential_ref):
            default_provider_factory(profile)
