"""
Fixtures for the evaluation-harness tests.

Every test here runs with a scripted provider: no test needs an API key and no
test makes a network call. That is a guarantee of the harness, not a
convenience — a test suite that only passes with a real credential cannot run in
CI, and the one thing this harness must never do is make a credential necessary
to check it.

The ``no_network`` fixture below ENFORCES that rather than asserting it, and it
exists because the claim has twice turned out to be false. First a test that
meant to substitute the provider factory patched a module attribute that
``run_suite`` had already captured as a default argument, so the real factory
ran and the suite made 27 outbound connections. Then — with the fixture in
place and three places in this repo stating the guarantee absolutely — one of
the ``.env`` probes below ran ``run_case`` with no factory *in a child
process*, where no fixture of this one's reaches, and issued real HTTP. That
probe now builds a provider and stops, because resolving a credential is what
it was ever about.

So the honest form of the guarantee: no test needs an API key, and no test
makes a network call — enforced by audit hook within this process, and true of
the child processes because none of them sends a request, which is a property
of those three tests rather than of a guard. A guarantee about what the suite
does not do is worth only as much as the thing that stops it.
"""

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import pytest

from backend.config.model_profiles import ModelProfile
from backend.llm.llm_providers import LLMProvider, LLMResponse

# A turn is either the text the model ends with, or the tool calls it requests.
Turn = Union[str, Sequence[Tuple[str, Dict[str, Any]]]]


class ScriptedProvider(LLMProvider):
    """
    An LLMProvider that plays a fixed script, standing in for a real model.

    Each element of ``turns`` is one create_completion: a string ends the turn
    with that text, a sequence of ``(tool_name, arguments)`` requests those
    tools. Running past the end of the script ends the conversation, so a script
    never has to count the assistant's recursion exactly.
    """

    def __init__(
        self,
        turns: Sequence[Turn],
        usage: Optional[Dict[str, int]] = None,
        raise_on_turn: Optional[int] = None,
    ):
        self.turns = list(turns)
        self.usage = usage
        self.raise_on_turn = raise_on_turn
        self.call_count = 0
        self.received_system_prompts: List[str] = []
        self.received_tools: List[List[Dict]] = []

    def create_completion(
        self,
        messages: List[Dict],
        system_prompt: str,
        tools: List[Dict],
        max_tokens: int = 4096,
    ) -> LLMResponse:
        index = self.call_count
        self.call_count += 1
        self.received_system_prompts.append(system_prompt)
        self.received_tools.append(tools)

        if self.raise_on_turn is not None and index == self.raise_on_turn:
            raise RuntimeError("scripted provider failure")

        turn: Turn = self.turns[index] if index < len(self.turns) else "End of script."
        raw = _RawResponse(self.usage) if self.usage is not None else None

        if isinstance(turn, str):
            return LLMResponse(
                content=[{"type": "text", "text": turn}],
                stop_reason="end_turn",
                raw_response=raw,
            )
        return LLMResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": f"call_{index}_{position}",
                    "name": name,
                    "input": arguments,
                }
                for position, (name, arguments) in enumerate(turn)
            ],
            stop_reason="tool_use",
            raw_response=raw,
        )

    def format_tool_definitions(self, tools: List[Dict]) -> List[Dict]:
        return tools


class _RawResponse:
    """Minimal stand-in for a provider SDK response that reports usage."""

    def __init__(self, usage: Dict[str, int]):
        self.usage = _Usage(usage)


class _Usage:
    def __init__(self, values: Dict[str, int]):
        for key, value in values.items():
            setattr(self, key, value)


# --------------------------------------------------------------------------
# audit-hook infrastructure
#
# Three successive rounds of this review closed a guard by widening an
# enumeration — six file-read entry points, six socket methods, four file-write
# entry points — and each time a mutation walked through the item that was
# missing: `os.write` in a guard whose sibling already hooked `os.open`, and
# `_socket.socket`, the C base class of the Python class being patched (a
# `monkeypatch.setattr` on the subclass leaves the base untouched, so
# `_socket.socket(...).sendto(...)` had real, unpatched methods).
#
# An audit hook is not an enumeration. The events fire from the C layer, so
# `open` covers `builtins.open`, `io.open`, `os.open`, `Path.open` and `mmap`
# alike, and the socket events cannot be sidestepped by importing a different
# module. A hook cannot be removed once installed, so one permanent hook
# dispatches to whatever watchers are currently registered.
#
# What this still cannot see is a child process: it has its own interpreter and
# its own hooks. Three tests here shell out deliberately (the `.env` probes),
# and none of them makes a request — one used to, through a `run_case` with no
# provider factory, and the suite really did issue outbound HTTP while three
# places claimed it never does.
# --------------------------------------------------------------------------

_AUDIT_WATCHERS: List[Any] = []


def _dispatch_audit(event: str, args: tuple) -> None:
    for watcher in list(_AUDIT_WATCHERS):
        watcher(event, args)


sys.addaudithook(_dispatch_audit)


@contextlib.contextmanager
def watch_audit(watcher):
    """Register an audit watcher for the duration of the block."""
    _AUDIT_WATCHERS.append(watcher)
    try:
        yield
    finally:
        _AUDIT_WATCHERS.remove(watcher)


def audit_open_target(args: tuple):
    """
    Decode an ``open`` audit event into ``(path, is_write)``, or None to ignore.

    The event's shape differs by caller and getting this wrong silently
    disables a guard: ``builtins.open`` passes a mode string, while ``os.open``
    passes ``None`` for the mode and the real flags as an integer third
    argument. A watcher that read only the second argument therefore saw
    ``None`` for every ``os.open`` and classified a write as "not a write" —
    which is exactly how an ``os.open`` + ``os.write`` debug dump of the
    prompt, the injected skill text and the answer passed a guard written to
    stop it.

    Both forms are consulted, so neither spelling can be the one that slips.
    """
    if not args:
        return None
    path, mode = args[0], args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if path is None or isinstance(path, int):
        return None
    writing = False
    if isinstance(mode, str):
        writing = any(flag in mode for flag in ("w", "a", "x", "+"))
    if isinstance(flags, int):
        writing = writing or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)
        )
    try:
        return Path(path).resolve(), writing
    except (OSError, ValueError, TypeError):
        return None


@contextlib.contextmanager
def record_file_access(root: Path, exclude=("site-packages", "dist-packages", ".git")):
    """
    Record files under ``root`` that are opened, with the mode each was opened in.

    Yields the list it fills: ``(relative_path, mode)`` tuples, where mode is
    ``"r"`` or ``"w"``. Reads and writes come through one hook because the
    ``open`` audit event carries both, which is also why neither side can be a
    shorter list of entry points than the other.
    """
    seen: List[tuple] = []

    def watcher(event, args):
        if event != "open":
            return
        decoded = audit_open_target(args)
        if decoded is None:
            return
        resolved, writing = decoded
        if not resolved.is_relative_to(root):
            return
        if any(part in exclude for part in resolved.parts):
            return
        seen.append((str(resolved.relative_to(root)), "w" if writing else "r"))

    with watch_audit(watcher):
        yield seen


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """
    Fail any test in this suite that attempts outbound traffic.

    Autouse and unconditional: a test that needs the network does not belong
    here, so there is deliberately no opt-out marker to reach for.

    Driven by audit events rather than by patched methods. Patching
    `socket.socket.{connect,sendto,…}` covered neither `_socket.socket`, the C
    base class, nor a connected `send` whose destination came from an earlier
    `connect` — and computing a "destination" per method to name in the failure
    was wrong for four of the six it hooked, naming a flags integer or the
    outbound payload itself. The audit events carry the real arguments, and
    `socket.connect` is unavoidable before any connected send, so blocking it
    plus the connectionless sends plus resolution covers the process.

    Localhost stays reachable: the fixture graph's storage is local, and
    breaking it would say nothing about egress.
    """
    blocked = {
        "socket.connect",
        "socket.connect_ex",
        "socket.sendto",
        "socket.sendmsg",
        "socket.bind",
    }
    local = {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0"}

    def is_local(address):
        if isinstance(address, (tuple, list)) and address:
            return address[0] in local
        return address in local

    def watcher(event, args):
        if event == "socket.getaddrinfo":
            if args and not is_local(args[0]):
                raise AssertionError(
                    f"{request.node.nodeid} attempted to resolve {args[0]!r}. "
                    "No test in this suite may touch the network."
                )
            return
        if event not in blocked:
            return
        address = args[1] if len(args) > 1 else None
        if is_local(address):
            return
        raise AssertionError(
            f"{request.node.nodeid} attempted outbound traffic ({event}) to "
            f"{address!r}. No test in this suite may touch the network — if a "
            "provider was built for real, the substitution did not take effect "
            "(see the audit-hook note in this module)."
        )

    with watch_audit(watcher):
        yield


@pytest.fixture(autouse=True)
def mock_embedding_model():
    """
    Keep embedding generation off the network.

    Mutating a fixture graph triggers an embedding update, which on a machine
    with sentence-transformers installed would download a model. Mocked so the
    suite behaves the same there as in CI's ML-free install.
    """
    import backend.core.vector_store as vs

    original = vs._ensure_sentence_transformers

    class _MockSentenceTransformer:
        def __init__(self, model_name=None):
            import numpy as np

            self._np = np

        def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
            single = isinstance(texts, str)
            items = [texts] if single else texts
            vectors = []
            for text in items:
                self._np.random.seed(abs(hash(text)) % (2**32))
                vectors.append(self._np.random.rand(384).astype(self._np.float32))
            result = self._np.array(vectors)
            return result[0] if single else result

    vs._ensure_sentence_transformers = lambda: _MockSentenceTransformer
    vs._SentenceTransformer = None
    yield
    vs._ensure_sentence_transformers = original
    vs._SentenceTransformer = None


@pytest.fixture
def profile() -> ModelProfile:
    """A provider configuration whose credential_ref names a variable, not a key."""
    return ModelProfile(
        id="eval-test-profile",
        name="Evaluation test profile",
        provider="openai",
        model="test-model",
        default=True,
        endpoint="https://example.invalid/v1",
        credential_ref="EVAL_HARNESS_TEST_KEY",
    )


@pytest.fixture
def scripted():
    """Build a provider_factory that ignores the profile and plays a script."""

    def _factory(turns: Sequence[Turn], **kwargs):
        provider = ScriptedProvider(turns, **kwargs)
        return lambda _profile: provider, provider

    return _factory


@pytest.fixture
def fixture_graph() -> Dict[str, Any]:
    """The small fixture graph, as the shipped cases use it."""
    from backend.evaluation.cases import GRAPHS_DIR

    return json.loads(
        (GRAPHS_DIR / "metadata-pilot-small.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def case_by_id():
    """Look a shipped acceptance case up by its id."""
    from backend.evaluation.cases import load_cases

    cases = {case.id: case for case in load_cases()}
    return cases
