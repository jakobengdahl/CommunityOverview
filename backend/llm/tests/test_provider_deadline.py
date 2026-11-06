# covers: backend/llm/llm_providers.py
"""Provider deadlines preserve SDK defaults, context, results, and errors."""

from unittest.mock import patch

import pytest

from backend.llm.llm_providers import ClaudeProvider, OpenAIProvider


@pytest.mark.parametrize(
    "provider_class, target",
    [(OpenAIProvider, "openai.OpenAI"), (ClaudeProvider, "anthropic.Anthropic")],
)
def test_total_provider_timeout_recovers_while_sdk_keeps_making_progress(
    provider_class, target
):
    from threading import Event
    from time import monotonic

    entered, release, finished = Event(), Event(), Event()

    def dribbling_response(**kwargs):
        entered.set()
        try:
            # Simulate an SDK that receives bytes often enough to avoid its
            # per-read timeout, but never completes the provider call.
            while not release.wait(0.005):
                pass
        finally:
            finished.set()

    with patch(target) as client:
        provider = provider_class("test-key")
        client.return_value.with_options.return_value = client.return_value
        create = (
            client.return_value.messages.create
            if provider_class is ClaudeProvider
            else client.return_value.chat.completions.create
        )
        create.side_effect = dribbling_response
        start = monotonic()
        try:
            with pytest.raises(TimeoutError, match="LLM call exceeded 0.05 seconds"):
                provider.create_completion([], "", [], timeout_seconds=0.05)
            assert entered.is_set()
            assert not finished.is_set()
            assert monotonic() - start < 1
        finally:
            release.set()
            assert finished.wait(1)


def test_provider_deadline_preserves_context_results_and_errors():
    from contextvars import ContextVar
    from backend.llm.llm_providers import _provider_call

    context = ContextVar("provider_test_context")
    token = context.set(object())
    try:
        assert _provider_call(context.get, deadline_seconds=1) is context.get()
        error = ValueError("provider failure")

        def fail():
            raise error

        with pytest.raises(ValueError) as caught:
            _provider_call(fail, deadline_seconds=1)
        assert caught.value is error
    finally:
        context.reset(token)


@pytest.mark.parametrize("provider_class", [OpenAIProvider, ClaudeProvider])
@pytest.mark.parametrize("bounded", [False, True])
def test_built_request_timeout_and_retries(provider_class, bounded, monkeypatch):
    """Inspect actual SDK requests, preserving non-chat defaults and retries."""
    import importlib
    import httpx as legacy_httpx

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "1")
    provider = provider_class("test-key")
    httpx = importlib.import_module(
        "httpx"
        if isinstance(provider.client.timeout, legacy_httpx.Timeout)
        else "httpx2"
    )
    requests = []
    from threading import current_thread

    caller = current_thread()

    def reject(request):
        assert (current_thread() is caller) is (not bounded)
        requests.append(request)
        return httpx.Response(
            400, json={"error": {"message": "test", "type": "invalid_request_error"}}
        )

    provider.client = provider.client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(reject))
    )
    original_timeout = provider.client.timeout
    original_retries = provider.client.max_retries
    if bounded:
        from backend.llm.llm_providers import _completion_client

        bounded_client = _completion_client(
            provider.client, 17, type(provider.client.timeout)
        )
        assert bounded_client.max_retries == original_retries
        assert provider.client.max_retries == original_retries == 2
    with pytest.raises(Exception) as caught:
        provider.create_completion(
            [], "", [], **({"timeout_seconds": 17} if bounded else {})
        )
    assert caught.value.status_code == 400
    assert len(requests) == 1
    timeout = requests[0].extensions["timeout"]
    assert timeout["connect"] == 5.0
    assert timeout["read"] == (17 if bounded else original_timeout.read)
    assert timeout["write"] == (17 if bounded else original_timeout.write)
    assert timeout["pool"] == (17 if bounded else original_timeout.pool)
    provider.client.close()


@pytest.mark.parametrize("provider_class", [OpenAIProvider, ClaudeProvider])
@pytest.mark.parametrize("hang_on_retry", [False, True])
def test_transient_error_retries_within_total_deadline(provider_class, hang_on_retry):
    import importlib
    from threading import Event

    import httpx as legacy_httpx

    provider = provider_class("test-key")
    httpx = importlib.import_module(
        "httpx"
        if isinstance(provider.client.timeout, legacy_httpx.Timeout)
        else "httpx2"
    )
    release, finished = Event(), Event()
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                500,
                headers={"retry-after-ms": "1"},
                json={"error": {"message": "transient", "type": "api_error"}},
            )
        if hang_on_retry:
            release.wait(2)
        finished.set()
        payload = (
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": provider.model,
                "content": [{"type": "text", "text": "Recovered"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            if provider_class is ClaudeProvider
            else {
                "id": "chat_test",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Recovered"},
                    "finish_reason": "stop",
                }],
            }
        )
        return httpx.Response(200, json=payload)

    provider.client = provider.client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        if hang_on_retry:
            with pytest.raises(TimeoutError, match="LLM call exceeded"):
                provider.create_completion([], "", [], timeout_seconds=0.1)
            assert not finished.is_set()
        else:
            response = provider.create_completion([], "", [], timeout_seconds=1)
            assert response.content == [{"type": "text", "text": "Recovered"}]
        assert len(requests) == 2
    finally:
        release.set()
        assert finished.wait(1)
        provider.client.close()
