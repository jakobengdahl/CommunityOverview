# covers: backend/ui/conversation_state.py, backend/ui/chat_service.py
"""The taint outlives the turn: signed conversation state (ADR 0010 §5)."""

import functools
import json
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.runtime.authorization import use_request_authorization
from backend.ui import conversation_state

FIXTURE = Path(__file__).parents[3] / "docs/fixtures/chat_turn2_request.json"
WRITE = {"name": "add_nodes", "input": {"nodes": [{"name": "W", "type": "Actor"}]}}
ACTOR = {"x-communityoverview-actor-id": "a", "x-communityoverview-actor-type": "user"}
GET_SCHEMA = {"name": "get_schema", "input": {}}
ISSUE = functools.partial(conversation_state.issue, actor={}, messages=[], reply="x")


@pytest.fixture(autouse=True)
def _fresh_taint_record(monkeypatch):
    monkeypatch.setattr(conversation_state, "_tainted", OrderedDict())
    monkeypatch.setattr(conversation_state, "_evicted_through", float("-inf"))


def turn(service, llm, messages, state=None, calls=(), headers=ACTOR, cid=None):
    llm.reset()
    llm.mock_tool_call_sequence = [list(calls)] if calls else None
    with use_request_authorization(headers=headers):
        return service.process_message(  # a copy: tool turns append to it
            [*messages], conversation_state=state, conversation_id=cid
        )


def followed_by(result, text):
    return [
        {"role": "assistant", "content": result["content"]},
        {"role": "user", "content": text},
    ]


def test_turn2_fixture_with_issued_token_starts_clean(fastapi_test_client):
    client, _, _ = fastapi_test_client
    body = json.loads(FIXTURE.read_text())
    first = client.post("/ui/chat", json={"messages": body["messages"][:1]}).json()
    assert first["content"] == body["messages"][1]["content"]
    body["conversation_state"] = first["conversation_state"]
    body["conversation_id"] = first["conversation_id"]
    assert client.post("/ui/chat", json=body).json()["untrusted_content_used"] is False


CLEAN_CHANGES = (
    "none surrogate_message empty_reply other_process_attachments_off read_elsewhere"
).split()
TAINTING_CHANGES = (
    "shared_key dropped forged non_ascii_signature surrogate_body expired edited "
    "actor other_process other_process_no_grant replayed tainted tainted_no_access "
    "rollback rollback_first_message rollback_other_id rollback_no_id "
    "rollback_evicted rollback_concurrent"
).split()


@pytest.mark.parametrize("change", CLEAN_CHANGES + TAINTING_CHANGES)
def test_continued_conversation_is_clean_only_with_valid_clean_state(
    chat_service, monkeypatch, change
):
    service, llm = chat_service
    tainted = change in TAINTING_CHANGES
    # Inherited taint applies only where the instance enables attachments.
    if change != "other_process_attachments_off":
        monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read")
    service._assistant_attachment_access = "read"
    if change in ("shared_key", "read_elsewhere"):
        monkeypatch.setenv("ATTACHMENT_CONVERSATION_STATE_KEY", "shared")
    service._processor.tools_map["get_schema"] = lambda: {"untrusted_content": True}
    first_calls = [GET_SCHEMA] if change.startswith("tainted") else ()
    # A lone surrogate passes JSON validation; it is hashed, not a 500.
    text = "hi\ud800" if change == "surrogate_message" else "hi"
    messages = [{"role": "user", "content": text}]
    with monkeypatch.context() as broken:
        if change == "empty_reply":  # an error without text yields an empty reply
            broken.setattr(llm, "create_completion", Mock(side_effect=RuntimeError))
        first = turn(service, llm, messages, calls=first_calls)
    if change == "empty_reply":
        assert first["content"] == conversation_state.EMPTY_REPLY_TEXT
    assert first["untrusted_content_used"] is change.startswith("tainted")
    messages += followed_by(first, text.replace("hi", "write"))
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
        messages += followed_by(second, "write")
    elif change == "read_elsewhere":
        # Another process serves cid under a fresh id, so its taint is not cid's.
        with monkeypatch.context() as other:
            other.setattr(conversation_state, "_PROCESS_KEY", secrets.token_bytes(32))
            other.setattr(conversation_state, "_tainted", OrderedDict())
            read = turn(service, llm, messages[:1], calls=[GET_SCHEMA], cid=cid)
        assert read["untrusted_content_used"] and read["conversation_id"] != cid
    elif change.startswith("rollback"):
        cid = None if change == "rollback_no_id" else cid
        read = turn(service, llm, messages, state, [GET_SCHEMA], cid=cid)
        assert read["untrusted_content_used"]
        if change == "rollback_first_message":
            messages = messages[:1]
        elif change == "rollback_other_id":
            cid = secrets.token_hex(8)
        elif change == "rollback_evicted":
            monkeypatch.setattr(conversation_state, "_TAINTED_MAX", 1)
            ISSUE(tainted=True, conversation_id="other")
        elif change == "rollback_concurrent":
            # Checked before the mark, issued after; replayed once it expired.
            start = time.time()
            clock = SimpleNamespace(time=lambda: start + 100)
            monkeypatch.setattr(conversation_state, "time", clock)
            with monkeypatch.context() as race:
                race.setattr(conversation_state, "taint_status", lambda *a: "clean")
                late = turn(service, llm, messages, state, cid=cid)
            clock.time = lambda: start + 86400 + 50
            messages += followed_by(late, "write")
            state = late["conversation_state"]
    result = turn(service, llm, messages, state, [WRITE], headers, cid)
    assert result["untrusted_content_used"] is tainted
    # Tainted from the start: the write is refused into a pending action.
    assert bool(result["pending_actions"]) is tainted
    if change in ("tainted", "other_process_attachments_off"):
        state = result["conversation_state"]
        if change == "other_process_attachments_off":
            # With access still off, the carried taint does not hold writes.
            messages += followed_by(result, "write")
            later = turn(service, llm, messages, state, [WRITE], cid=cid)
            assert not (later["untrusted_content_used"] or later["pending_actions"])
            state, result = later["conversation_state"], later
        # The unverifiable state is carried forward, not laundered clean.
        monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read")
        messages += followed_by(result, "again")
        again = turn(service, llm, messages, state, cid=cid)
        assert again["untrusted_content_used"] is True


@pytest.mark.parametrize("drop", ["overflow", "expiry"])
def test_turn_started_before_a_dropped_mark_is_issued_tainted(monkeypatch, drop):
    """A mark made after a turn started taints it even once evicted or expired."""
    start = time.time()
    clock = SimpleNamespace(time=lambda: start + 1)
    monkeypatch.setattr(conversation_state, "time", clock)
    ISSUE(tainted=True, conversation_id="c")
    if drop == "overflow":
        monkeypatch.setattr(conversation_state, "_TAINTED_MAX", 1)
    else:
        clock.time = lambda: start + 86400 + 50
    ISSUE(tainted=True, conversation_id="other")
    assert len(conversation_state._tainted) == 1
    token = ISSUE(tainted=False, conversation_id="c", started_at=start)
    replay = [{"role": "assistant", "content": "x"}]
    status = conversation_state.taint_status(replay, token, {}, "c")
    assert status == conversation_state.TAINTED
