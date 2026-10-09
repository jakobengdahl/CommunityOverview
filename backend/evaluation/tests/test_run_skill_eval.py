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

    @pytest.mark.parametrize(
        "route",
        [
            "connect",
            "connect_ex",
            "sendto",
            "sendmsg",
            "bind",
            "raw_socket_module",
            "create_connection",
        ],
    )
    def test_outbound_traffic_inside_this_suite_fails_the_test(self, route):
        """
        A probe per route the guard has to cover, including the C base class.

        Patching methods on `socket.socket` left two holes that a mutation
        walked through: `_socket.socket` is the C base class, so the refusals
        installed on the Python subclass were simply not there (a statsd
        datagram sent that way was delivered), and the destination the failure
        named was computed per method and wrong for four of the six — for
        `send` it printed the outbound payload and called it a destination.

        The guard is now audit-event driven, which changes what "a connected
        send" means here: `send`/`sendall` have no audit event of their own,
        but reaching a connected socket requires `connect`, which does. So
        those two are covered by the `connect` row rather than probed directly;
        probing them against an unconnected socket only measured the OS.
        """
        import socket

        payload = b"x"
        address = ("192.0.2.1", 8125)

        # Either branch of the guard is a pass: `create_connection` resolves
        # before it connects, so it is stopped by the resolve check, and which
        # of the two fires is not what this pins.
        with pytest.raises(
            AssertionError, match="attempted (outbound traffic|to resolve)"
        ):
            if route == "raw_socket_module":
                import _socket

                _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM).sendto(
                    payload, address
                )
            elif route == "create_connection":
                socket.create_connection(address, timeout=1)
            elif route == "bind":
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM).bind(("192.0.2.1", 0))
            elif route == "sendmsg":
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg(
                    [payload], [], 0, address
                )
            elif route == "sendto":
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
                    payload, address
                )
            else:
                getattr(socket.socket(socket.AF_INET, socket.SOCK_STREAM), route)(
                    address
                )

    def test_a_connected_send_is_unreachable_because_connect_is_refused(self):
        """
        Why `send`/`sendall` need no probe of their own under the audit guard.

        Stated as a test rather than only in a comment, so that if `connect`
        ever stops being blocked this reasoning fails loudly instead of leaving
        the two send methods silently uncovered.
        """
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(AssertionError, match="attempted outbound traffic"):
            sock.connect(("192.0.2.1", 8125))
        with pytest.raises(OSError):
            sock.sendall(b"x")

    def test_resolving_a_hostname_inside_this_suite_fails_the_test(self):
        """
        Caught before a socket exists, which gives a clearer failure.

        Localhost stays resolvable: the fixture graph's storage and the test
        HTTP stubs are local, and breaking those would say nothing about
        egress.
        """
        import socket

        with pytest.raises(AssertionError, match="attempted to resolve"):
            socket.getaddrinfo("example.invalid", 443)
        assert socket.getaddrinfo("127.0.0.1", 0)

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

        # Off the BUILT REQUEST, not off an attribute. `client.api_key` is one
        # of several inputs the SDK merges into a request, and it is not the
        # last word: `default_headers` lands after the auth header it derives,
        # so `client._custom_headers["Authorization"] = <file contents>` puts a
        # file-sourced key on the wire while `api_key` still reads the
        # environment value. This test plants `.eval_key` and chdirs to it, so
        # it EXECUTED that leak and passed. The header is what actually goes
        # out, and asserting it closes the class instead of one attribute.
        self.assert_request_carries_only(provider, "ENV-SOURCED-KEY")
        assert provider.client.api_key == "ENV-SOURCED-KEY"
        assert getattr(provider, "api_key", None) in (None, "ENV-SOURCED-KEY")

    def test_building_a_provider_reads_no_file_inside_the_repository(
        self, monkeypatch, tmp_path
    ):
        """
        The whole outgoing request, and every read entry point.

        Three mutations put a file-sourced key on the wire past the guards
        here. The first two wrote `Authorization` into the SDK client's custom
        headers, which the SDK merges AFTER the auth header it derives from
        `api_key`, one of them through a local variable so the static scan saw
        an `ast.Subscript` on an `ast.Name` and had nothing to match. The third
        went around headers entirely: `client._custom_query["api_key"]`, which
        the SDK merges into the request URL, read via `os.open`/`os.read`,
        which this guard did not hook. Every assertion was on headers, so the
        key travelled in the query string with the suite green.

        Hence two independent checks, both over outcomes rather than
        spellings: nothing inside the repository is read while a provider is
        built, and the built request — url, headers and body — carries the
        environment value and no other.

        The second is the load-bearing one. The read guard is an audit hook on
        the `open` event rather than a list of patched functions: three rounds
        of this review closed a guard by widening an enumeration and each time
        a mutation walked through the item that was missing, so the net is now
        at the layer where `builtins.open`, `io.open`, `os.open`, `Path.open`
        and `mmap` are all the same event. A child process still has its own
        interpreter and its own hooks, so this is a net and not a proof.
        Anything that reaches the wire, however it was read, has to pass
        through the request.

        Site-packages is excluded by path segment, not by assuming it sits
        outside the tree: `.gitignore` names `venv/` and `.venv/`, so a
        developer following that layout has the SDK's own package data
        resolving inside the repo, and importlib.metadata reads two
        `dist-info/METADATA` files while building a client.
        """
        import pathlib

        from backend.config.model_profiles import ModelProfile
        from backend.evaluation.runner import default_provider_factory
        from backend.evaluation.tests.conftest import record_file_access

        repo = pathlib.Path(__file__).resolve().parents[3]

        planted = []
        try:
            # Plant one everywhere a leak has been tried, so a read that does
            # happen has something to find and the failure names the file.
            # Inside the try: a failure on the second write must still clean up
            # the first, or a file called `.eval_key` holding something that
            # looks like a key is left in the working tree.
            for candidate in (
                repo / "backend" / "evaluation" / "fixtures" / ".eval_endpoint_token",
                repo / ".eval_key",
                repo / "backend" / "evaluation" / ".eval_key",
            ):
                if not candidate.exists():
                    candidate.write_text("FILE-SOURCED-KEY", encoding="utf-8")
                    planted.append(candidate)

            monkeypatch.setenv("SKILL_EVAL_FS_PROBE", "ENV-SOURCED-KEY")
            with record_file_access(repo) as accesses:
                provider = default_provider_factory(
                    ModelProfile(
                        id="probe",
                        name="Probe",
                        provider="openai",
                        model="m",
                        default=True,
                        credential_ref="SKILL_EVAL_FS_PROBE",
                    )
                )
        finally:
            monkeypatch.undo()
            for candidate in planted:
                candidate.unlink(missing_ok=True)

        reads = [path for path, mode in accesses if mode == "r"]
        assert not reads, f"building a provider read repository file(s): {reads}"
        self.assert_request_carries_only(provider, "ENV-SOURCED-KEY")

    @staticmethod
    def assert_request_carries_only(provider, expected_key):
        """
        Assert the built request carries `expected_key` and no other credential.

        Over the whole request, because a leak does not have to use a header:
        `_custom_query` rides in the URL, and a body parameter would ride in
        the content. Asserting on `request.headers` alone let a file-sourced
        key through in the query string.
        """
        import sys as sys_module

        from openai._models import FinalRequestOptions

        client = provider.client
        built = client._build_request(
            FinalRequestOptions(method="post", url="/chat/completions", json_data={})
        )

        # Then SEND it, through a mock transport. Building a request is not the
        # last word on what goes out: an httpx event hook is installed at
        # construction and runs at send time, so a hook reading a repo file and
        # stamping `Authorization` left every construction-time assertion true
        # (`client.api_key` was still the environment value, `_custom_query` was
        # empty, nothing was read while the read guard watched) while the wire
        # carried a file-sourced key. `Client.send` runs the hooks and a mock
        # transport keeps it local, so what the transport receives is what the
        # endpoint would have received.
        captured = []

        # The httpx the CLIENT uses, taken from the client rather than
        # imported: two httpx distributions are installed here, the SDK is
        # built against one of them, and a transport or response from the other
        # fails the sync client's own isinstance check deep inside httpx — the
        # probe then dies in the library instead of reaching its assertions.
        inner = client._client
        # From the MRO, not the instance's own class: the SDK's client is a
        # subclass of the httpx one, so its own `__module__` is `openai`.
        httpx = sys_module.modules[
            next(
                base.__module__.split(".")[0]
                for base in type(inner).__mro__
                if base.__module__.split(".")[0] not in ("openai", "builtins")
            )
        ]

        class CapturingTransport(httpx.BaseTransport):
            """A minimal sync transport; `MockTransport` differs across these."""

            def handle_request(self, request):
                captured.append(request)
                return httpx.Response(200, content=b"{}", request=request)

        # Both the default transport and any proxy mounts: with HTTPS_PROXY set
        # — as it is in the cloud session this harness is developed in — httpx
        # routes through `_mounts` and never consults `_transport`, so swapping
        # only the latter sent the probe at the real proxy.
        transport, mounts = inner._transport, dict(inner._mounts)
        inner._transport = CapturingTransport()
        inner._mounts = {}
        try:
            inner.send(built)
        finally:
            inner._transport = transport
            inner._mounts = mounts

        assert captured, "the request was never sent, so hooks never ran"
        request = captured[0]
        whole = "\n".join(
            (
                str(request.url),
                str(request.headers),
                (request.content or b"").decode("utf-8", "replace"),
            )
        )
        assert not client._client.event_hooks.get("request"), (
            "the client carries request event hooks, which run after the "
            f"request is built: {client._client.event_hooks.get('request')}"
        )
        assert request.headers.get("authorization") == f"Bearer {expected_key}"
        assert "FILE-SOURCED-KEY" not in whole, (
            "a file-sourced credential reached the outgoing request"
        )
        for marker in ("api_key", "api-key", "apikey", "token", "secret"):
            for segment in str(request.url.query, "utf-8").split("&"):
                assert not segment.lower().startswith(marker), (
                    f"the request URL carries a credential-shaped query "
                    f"parameter: {segment.split('=')[0]}"
                )
        assert not client._custom_query, (
            f"the client carries custom query parameters: "
            f"{sorted(client._custom_query)}"
        )

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
                    # A subscript target wears the attribute one level down:
                    # `client._custom_headers["Authorization"] = …` is an
                    # ast.Subscript, so matching ast.Attribute alone let the
                    # proven bypass through with the whole suite green.
                    if isinstance(target, ast.Subscript):
                        target = target.value
                    if isinstance(target, ast.Attribute) and target.attr in (
                        "api_key",
                        "api_key_override",
                        "auth_token",
                        # Not named for a credential, but each carries one: the
                        # SDK merges custom headers AFTER its own auth headers,
                        # so assigning here overrides Authorization outright
                        # while `api_key` still reads the environment value.
                        "_custom_headers",
                        "default_headers",
                        "headers",
                        "auth_headers",
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
