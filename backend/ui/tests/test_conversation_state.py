# covers: backend/ui/conversation_state.py, backend/ui/chat_service.py
"""The taint outlives the turn: signed conversation state (ADR 0010 §5)."""

import json
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.runtime.authorization import use_request_authorization
from backend.ui import conversation_state

FIXTURE = Path(__file__).parents[3] / "docs/fixtures/chat_turn2_request.json"
WRITE = {"name": "add_nodes", "input": {"nodes": [{"name": "W", "type": "Actor"}]}}
ACTOR = {"x-communityoverview-actor-id": "a", "x-communityoverview-actor-type": "user"}


@pytest.fixture(autouse=True)
def _fresh_taint_record(monkeypatch):
    monkeypatch.setattr(conversation_state, "_tainted", OrderedDict())
    monkeypatch.setattr(conversation_state, "_evicted_through", float("-inf"))


def turn(service, llm, messages, state=None, calls=(), headers=ACTOR, cid=None):
    llm.reset()
    llm.mock_tool_call_sequence = [list(calls)] if calls else None
    with use_request_authorization(headers=headers):
        return service.process_message(
            messages, conversation_state=state, conversation_id=cid
        )


def test_turn2_fixture_with_issued_token_starts_clean(fastapi_test_client):
    client, _, _ = fastapi_test_client
    body = json.loads(FIXTURE.read_text())
    first = client.post("/ui/chat", json={"messages": body["messages"][:1]}).json()
    assert first["content"] == body["messages"][1]["content"]
    body["conversation_state"] = first["conversation_state"]
    body["conversation_id"] = first["conversation_id"]
    assert client.post("/ui/chat", json=body).json()["untrusted_content_used"] is False


@pytest.mark.parametrize(
    "change, tainted",
    [
        ("none", False),
        ("shared_key", True),
        ("surrogate_message", False),
        ("dropped", True),
        ("forged", True),
        ("non_ascii_signature", True),
        ("surrogate_body", True),
        ("expired", True),
        ("edited", True),
        ("actor", True),
        ("other_process", True),
        ("other_process_no_grant", True),
        ("other_process_attachments_off", False),
        ("replayed", True),
        ("tainted", True),
        ("tainted_no_access", True),
    ],
)
def test_continued_conversation_is_clean_only_with_valid_clean_state(
    chat_service, monkeypatch, change, tainted
):
    service, llm = chat_service
    # Inherited taint applies only where the instance enables attachments.
    if change != "other_process_attachments_off":
        monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read")
    service._assistant_attachment_access = "read"
    if change == "shared_key":
        monkeypatch.setenv("ATTACHMENT_CONVERSATION_STATE_KEY", "shared")
    first_calls = ()
    if change in ("tainted", "tainted_no_access"):
        service._processor.tools_map["get_schema"] = lambda: {"untrusted_content": True}
        first_calls = [{"name": "get_schema", "input": {}}]
    # A lone surrogate passes JSON validation; it is hashed, not a 500.
    text = "hi\ud800" if change == "surrogate_message" else "hi"
    messages = [{"role": "user", "content": text}]
    first = turn(service, llm, messages, calls=first_calls)
    assert first["untrusted_content_used"] is change.startswith("tainted")
    messages += [
        {"role": "assistant", "content": first["content"]},
        {"role": "user", "content": text.replace("hi", "write")},
    ]
    state, headers, cid = first["conversation_state"], ACTOR, first["conversation_id"]
    if change == "dropped":
        state = None
    elif change == "forged":
        body, signature = state.split(".")
        state = f"{body}.{signature[::-1]}"
    elif change == "non_ascii_signature":
        state = f"{state[:-1]}\u00e9"
    elif change == "surrogate_body":
        state = f"\ud800{state}"
    elif change == "expired":
        later = time.time() + 86401
        monkeypatch.setattr(
            conversation_state, "time", SimpleNamespace(time=lambda: later)
        )
    elif change == "edited":
        messages[0] = {"role": "user", "content": "hi!"}
    elif change == "actor":
        headers = {**ACTOR, "x-communityoverview-actor-id": "b"}
    elif change.startswith("other_process") or change == "shared_key":
        # Even with a shared key: that process's taint record is not here.
        monkeypatch.setattr(conversation_state, "_PROCESS_KEY", secrets.token_bytes(32))
        monkeypatch.setattr(conversation_state, "_PROCESS_ID", "other")
        if change == "other_process_no_grant":
            # A revoked grant must not launder an unverifiable state clean.
            service._assistant_attachment_access = "off"
    elif change == "tainted_no_access":
        # A verified taint holds even where no attachment can be read now.
        service._assistant_attachment_access = "off"
    elif change == "replayed":
        second = turn(service, llm, messages, state, cid=cid)
        assert second["untrusted_content_used"] is False
        messages += [
            {"role": "assistant", "content": second["content"]},
            {"role": "user", "content": "write"},
        ]
    result = turn(service, llm, messages, state, [WRITE], headers, cid)
    assert result["untrusted_content_used"] is tainted
    # Tainted from the start: the write is refused into a pending action.
    assert bool(result["pending_actions"]) is tainted
    if change in ("tainted", "other_process_attachments_off"):
        state = result["conversation_state"]
        if change == "other_process_attachments_off":
            # With access still off, the carried taint does not hold writes.
            messages += [
                {"role": "assistant", "content": result["content"]},
                {"role": "user", "content": "write"},
            ]
            later = turn(service, llm, messages, state, [WRITE], cid=cid)
            assert later["untrusted_content_used"] is False
            assert not later["pending_actions"]
            state = later["conversation_state"]
            result = later
        # The unverifiable state is carried forward, not laundered clean.
        monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read")
        messages += [
            {"role": "assistant", "content": result["content"]},
            {"role": "user", "content": "again"},
        ]
        again = turn(service, llm, messages, state, cid=cid)
        assert again["untrusted_content_used"] is True


ROLLBACKS = "truncated first_message other_id no_id evicted concurrent".split()


@pytest.mark.parametrize("replay", ROLLBACKS)
def test_rollback_to_a_clean_state_stays_tainted(chat_service, monkeypatch, replay):
    service, llm = chat_service
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read")
    service._processor.tools_map["get_schema"] = lambda: {"untrusted_content": True}
    hi = {"role": "user", "content": "hi"}
    first = turn(service, llm, [hi])
    cid = None if replay == "no_id" else first["conversation_id"]
    clean = [hi, {"role": "assistant", "content": first["content"]}]
    state, get_schema = first["conversation_state"], {"name": "get_schema", "input": {}}
    read = turn(service, llm, [*clean, hi], state, [get_schema], cid=cid)
    assert read["untrusted_content_used"]
    history = [] if replay == "first_message" else clean
    key = secrets.token_hex(8) if replay == "other_id" else cid
    if replay == "evicted":
        monkeypatch.setattr(conversation_state, "_TAINTED_MAX", 1)
        conversation_state.issue(
            tainted=True, actor={}, messages=[], reply="x", conversation_id="other"
        )
    elif replay == "concurrent":
        # Checked before the mark, issued after; replayed once the mark expired.
        start = time.time()
        clock = SimpleNamespace(time=lambda: start + 100)
        monkeypatch.setattr(conversation_state, "time", clock)
        with monkeypatch.context() as race:
            race.setattr(conversation_state, "taint_status", lambda *a: "clean")
            late = turn(service, llm, [*clean, hi], state, cid=cid)
        clock.time = lambda: start + 86400 + 50
        history = [*clean, hi, {"role": "assistant", "content": late["content"]}]
        state = late["conversation_state"]
    result = turn(service, llm, [*history, hi], state, [WRITE], cid=key)
    assert result["untrusted_content_used"] and result["pending_actions"]


def test_only_the_minting_process_serves_a_conversation(chat_service, monkeypatch):
    service, llm = chat_service
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read")
    monkeypatch.setenv("ATTACHMENT_CONVERSATION_STATE_KEY", "shared")
    service._processor.tools_map["get_schema"] = lambda: {"untrusted_content": True}
    hi = {"role": "user", "content": "hi"}
    first = turn(service, llm, [hi])
    cid, clean = first["conversation_id"], [hi, {"role": "assistant"}, hi]
    clean[1]["content"] = first["content"]
    with monkeypatch.context() as other:
        other.setattr(conversation_state, "_PROCESS_KEY", secrets.token_bytes(32))
        other.setattr(conversation_state, "_tainted", OrderedDict())
        read = turn(
            service, llm, [hi], calls=[{"name": "get_schema", "input": {}}], cid=cid
        )
        assert read["untrusted_content_used"] and read["conversation_id"] != cid
    # The taint went to another id, so this process's record for cid is complete.
    back = turn(service, llm, clean, first["conversation_state"], cid=cid)
    assert back["conversation_id"] == cid and not back["untrusted_content_used"]


def test_empty_reply_is_a_fixed_text_that_replays_clean(chat_service, monkeypatch):
    service, llm = chat_service
    messages = [{"role": "user", "content": "hi"}]

    def fail(*args, **kwargs):
        raise RuntimeError()  # an error without text yields an empty reply

    monkeypatch.setattr(llm, "create_completion", fail)
    with use_request_authorization(headers=ACTOR):
        first = service.process_message(messages)
    assert first["content"] == conversation_state.EMPTY_REPLY_TEXT
    monkeypatch.undo()
    messages += [
        {"role": "assistant", "content": first["content"]},
        {"role": "user", "content": "next"},
    ]
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read")
    second = turn(
        service,
        llm,
        messages,
        first["conversation_state"],
        cid=first["conversation_id"],
    )
    assert second["untrusted_content_used"] is False
