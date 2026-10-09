"""
Fixtures for the evaluation-harness tests.

Every test here runs with a scripted provider: no test needs an API key and no
test makes a network call. That is a guarantee of the harness, not a
convenience — a test suite that only passes with a real credential cannot run in
CI, and the one thing this harness must never do is make a credential necessary
to check it.

The ``no_network`` fixture below ENFORCES the second half rather than asserting
it. It had to be added after the claim turned out to be false: a test that meant
to substitute the provider factory patched a module attribute that
``run_suite`` had already captured as a default argument, so the real factory
ran and the suite made 27 outbound connections while documenting that it made
none. A guarantee about what the suite does not do is worth only as much as the
thing that stops it.
"""

import json
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


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """
    Fail any test in this suite that attempts outbound traffic.

    Autouse and unconditional: a test that needs the network does not belong
    here, so there is deliberately no opt-out marker to reach for. The failure
    names the destination, because the useful question when this fires is which
    provider got built for real.

    Connection-oriented egress was all this covered, while the module docstring
    claimed the suite makes none at all. A mutation sending statsd-shaped
    telemetry with `sendto` — connectionless, so it never calls `connect` —
    left the suite green and the datagram really did leave the machine. The
    send methods are hooked too, and `getaddrinfo`, which catches a resolve
    before any socket exists and gives a clearer failure than a refused send.

    What this cannot see is a child process: `subprocess.run(["curl", …])`
    opens its own sockets in its own interpreter. Three tests here do shell
    out deliberately (the `.env` probes), so banning that outright is not an
    option, and the claim is therefore about this process.
    """
    import socket

    saved = {
        name: getattr(socket.socket, name)
        for name in (
            "connect",
            "connect_ex",
            "sendto",
            "sendmsg",
            "sendall",
            "send",
        )
        if hasattr(socket.socket, name)
    }
    real_getaddrinfo = socket.getaddrinfo

    def refuse_socket(name):
        def refuse(self, *args, **kwargs):
            destination = args[1] if name in ("sendto", "sendmsg") else args[0:1]
            raise AssertionError(
                f"{request.node.nodeid} attempted outbound traffic via "
                f"{name}() to {destination!r}. No test in this suite may touch "
                "the network — if a provider was built for real, the "
                "substitution did not take effect (see this module's "
                "docstring)."
            )

        return refuse

    def refuse_resolve(host, *args, **kwargs):
        if host in (None, "", "localhost", "127.0.0.1", "::1"):
            return real_getaddrinfo(host, *args, **kwargs)
        raise AssertionError(
            f"{request.node.nodeid} attempted to resolve {host!r}. No test in "
            "this suite may touch the network."
        )

    for name in saved:
        monkeypatch.setattr(socket.socket, name, refuse_socket(name))
    monkeypatch.setattr(socket, "getaddrinfo", refuse_resolve)
    try:
        yield
    finally:
        for name, original in saved.items():
            setattr(socket.socket, name, original)
        socket.getaddrinfo = real_getaddrinfo


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
