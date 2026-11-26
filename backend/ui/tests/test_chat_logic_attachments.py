# covers: backend/ui/chat_logic.py
"""Chat processor coverage: attachment refusal audit and system prompt rules."""

import re
from unittest.mock import MagicMock

import pytest

from backend.ui.tests.test_chat_service import _tool_result_payloads


@pytest.mark.parametrize("chained", [False, True])
def test_refused_arguments_stay_out_of_model_and_history(
    chat_service, monkeypatch, chained
):
    import json

    monkeypatch.setenv("ATTACHMENT_PENDING_ACTIONS_PER_TURN", "1")
    service, llm = chat_service
    processor = service._processor
    processor.tools_map["get_schema"] = lambda: {"untrusted_content": True}
    write = MagicMock()
    processor.tools_map["future_write"] = write
    arguments = {"nested": {"values": ["private-bound-argument"]}}
    calls = [
        {"name": "future_write", "input": arguments},
        {"name": "future_write", "input": {"value": "private-over-limit"}},
    ]
    read = {"name": "get_schema", "input": {}}
    llm.mock_tool_call_sequence = [[read], calls] if chained else [[read, *calls]]
    messages = [{"role": "user", "content": "read files"}]

    result = processor.process_message(messages, llm_provider=llm)

    write.assert_not_called()
    (action,) = result["pending_actions"]
    assert action["tool"] == "future_write"
    assert action["arguments"] == arguments
    for history in [messages, *llm.received_messages]:
        serialized = json.dumps(history)
        assert "private-bound-argument" not in serialized
        assert "private-over-limit" not in serialized
        assert action["token"] not in serialized
    blocks = [
        block
        for message in messages
        if message["role"] == "assistant"
        for block in message["content"]
        if block.get("name") == "future_write"
    ]
    assert len(blocks) == 2
    assert all(block["input"] == {} for block in blocks)
    refused = [
        block
        for message in messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if json.loads(block["content"]).get("error_code") == "confirmation_required"
    ]
    assert [block["tool_use_id"] for block in refused] == [
        block["id"] for block in blocks
    ]


def test_clean_tool_arguments_remain_in_history(chat_service):
    service, llm = chat_service
    write = MagicMock(return_value={"success": True})
    service._processor.tools_map["future_write"] = write
    arguments = {"value": "ordinary-input"}
    llm.mock_tool_calls = [{"name": "future_write", "input": arguments}]
    messages = [{"role": "user", "content": "write"}]

    result = service._processor.process_message(messages, llm_provider=llm)

    write.assert_called_once()
    assert result["pending_actions"] == []
    assert messages[1]["content"][0]["input"] == arguments


@pytest.mark.parametrize("name", ["read_node_attachment", "search_node_attachments"])
def test_attachment_hook_refusal_formatted_audit(
    chat_service, monkeypatch, caplog, name
):
    import json
    import logging
    from backend.runtime.authorization import GraphAuthorizationDecision

    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    service, llm = chat_service
    service._assistant_attachment_access = "read"
    raw = MagicMock()
    service._processor.tools_map[name] = raw
    service._processor._authorization_hook = MagicMock()
    service._processor._authorization_hook.evaluate.return_value = (
        GraphAuthorizationDecision(allowed=False)
    )
    secret = "private-query-content-filename.txt"
    llm.mock_tool_calls = [
        {"name": name, "input": {"query": secret, "node_id": secret}}
    ]
    with caplog.at_level("INFO", logger="attachments.audit"):
        service.process_message(messages=[{"role": "user", "content": "read files"}])
    raw.assert_not_called()
    lines = [
        logging.Formatter("%(message)s").format(r)
        for r in caplog.records
        if r.name == "attachments.audit"
    ]
    assert len(lines) == 1
    assert secret not in lines[0]
    record = json.loads(lines[0])
    assert record["tool"] == name
    assert record["outcome"] == "access_denied"
    assert record["actor"] == {"type": "user", "id": None}
    assert [record[k] for k in ("node_id", "attachment_id", "sha256", "chars")] == [
        None,
        None,
        None,
        0,
    ]


@pytest.mark.parametrize("name", ["read_node_attachment", "search_node_attachments"])
@pytest.mark.parametrize("refusal", ["access_denied", "invalid_arguments"])
def test_attachment_early_refusal_formatted_audit(
    chat_service, caplog, monkeypatch, name, refusal
):
    import json
    import logging
    from backend.service.attachment_tools import tool_caller

    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    service, llm = chat_service
    processor = service._processor
    secret = "private-query-content-filename.txt"
    raw = MagicMock()

    def tool(node_ids, query):
        return raw(node_ids, query)

    processor.tools_map[name] = tool
    processor.tool_definitions.append({"name": name})
    llm.mock_tool_calls = [{"name": name, "input": {"query": secret}}]
    with (
        caplog.at_level("INFO", logger="attachments.audit"),
        tool_caller("kiosk", "collection"),
    ):
        processor.process_message(
            messages=[{"role": "user", "content": "read files"}],
            tool_allowlist=["search_graph"] if refusal == "access_denied" else [name],
            attachment_access=lambda _: "read",
        )
    raw.assert_not_called()
    result = _tool_result_payloads(llm)[0]
    if refusal == "access_denied":
        assert "allowlist" in result["error"]
    else:
        assert result["error_code"] == refusal
    lines = [
        logging.Formatter("%(message)s").format(r)
        for r in caplog.records
        if r.name == "attachments.audit"
    ]
    assert len(lines) == 1
    assert secret not in lines[0]
    record = json.loads(lines[0])
    assert record["actor"] == {"type": "kiosk", "id": "collection"}
    assert record["tool"] == name
    assert record["outcome"] == refusal
    assert [record[k] for k in ("node_id", "attachment_id", "sha256", "chars")] == [
        None,
        None,
        None,
        0,
    ]


@pytest.mark.parametrize("name", ["read_node_attachment", "search_node_attachments"])
def test_attachment_non_object_audit(chat_service, monkeypatch, caplog, name):
    import json
    import logging

    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    service, llm = chat_service
    raw = MagicMock()
    service._processor.tools_map[name] = raw
    llm.mock_tool_calls = [{"name": name, "input": ["SECRET-file-query"]}]
    with caplog.at_level("INFO"):
        service._processor.process_message(
            messages=[{"role": "user", "content": "read files"}],
            attachment_access=lambda _: "read",
        )
    raw.assert_not_called()
    lines = [
        logging.Formatter("%(message)s").format(r)
        for r in caplog.records
        if r.name == "attachments.audit"
    ]
    assert len(lines) == 1
    assert "SECRET-file-query" not in lines[0]
    assert json.loads(lines[0])["outcome"] == "invalid_arguments"


def test_write_verification_rules_reach_prompt_and_name_only_offered_tools(
    chat_service,
):
    """All four rules are in the prompt, call only advertised tools, and let
    add_nodes() edges name nodes created in the same call."""
    from backend.ui import chat_logic

    service, _ = chat_service
    prompt = chat_logic._build_system_prompt()
    offered = {t["name"] for t in service._processor.tool_definitions}
    rules = chat_logic._WRITE_VERIFICATION_RULES
    called = {name for rule in rules for name in re.findall(r"(\w+)\(", rule)}

    add_nodes = next(
        t for t in service._processor.tool_definitions if t["name"] == "add_nodes"
    )
    node_fields = add_nodes["input_schema"]["properties"]["nodes"]["items"]

    assert len(rules) == 4
    assert all(rule in prompt for rule in rules)
    assert called and called <= offered
    # add_nodes() offers no nodes[].id, so ID-first must exempt same-call nodes.
    assert "id" not in node_fields["properties"]
    assert rules[3].endswith(chat_logic._SAME_CALL_EDGE_EXEMPTION)


def test_prompt_prefix_and_suffix_still_wrap_the_base_prompt(monkeypatch):
    """Presentation prefix opens and suffix closes around the added rules."""
    from backend.ui import chat_logic

    presentation = {"prompt_prefix": "PREFIX-MARK", "prompt_suffix": "SUFFIX-MARK"}
    monkeypatch.setattr(
        chat_logic.config_loader, "get_presentation", lambda: presentation
    )
    prompt = chat_logic._build_system_prompt()
    section = prompt.index(chat_logic._WRITE_VERIFICATION_SECTION)

    assert prompt.startswith("PREFIX-MARK")
    assert section < prompt.index("SUFFIX-MARK")


@pytest.mark.asyncio
async def test_attachment_refusal_runs_on_event_loop_in_chat_chain(
    chat_service, monkeypatch
):
    import threading
    from backend.ui import chat_logic

    service, llm = chat_service
    loop_thread = threading.get_ident()
    seen = []
    original = chat_logic.attachment_tool_refusal

    def record(*args, **kwargs):
        seen.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(chat_logic, "attachment_tool_refusal", record)
    llm.mock_tool_call_sequence = [
        [{"name": "get_schema", "input": {}}],
        [{"name": "read_node_attachment", "input": {}}],
    ]
    await chat_logic.run_chat_off_event_loop(
        service.process_message, messages=[{"role": "user", "content": "read"}]
    )
    assert seen == [loop_thread]


def _exercise_pending_loop_hop(ready):
    import asyncio
    import threading
    from backend.ui import chat_logic

    async def run():
        loop = asyncio.get_running_loop()
        second_started = threading.Event()
        original = chat_logic.from_thread.run_sync
        second = []

        async def start_second():
            second_started.set()
            await chat_logic.run_chat_off_event_loop(lambda: None)

        def hop(func, *args):
            loop.call_soon_threadsafe(
                lambda: second.append(asyncio.create_task(start_second()))
            )
            assert second_started.wait(2)
            return original(func, *args)

        chat_logic.from_thread.run_sync = hop
        await chat_logic.run_chat_off_event_loop(
            lambda: chat_logic.run_on_event_loop(lambda: None)
        )
        await second[0]
        assert second_started.is_set()

    ready.set()
    asyncio.run(run())


def test_pending_loop_hop_does_not_block_another_chat():
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_exercise_pending_loop_hop, args=(ready,))
    process.start()
    started = ready.wait(30)
    process.join(5 if started else 0)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail(
            "Chat process did not start or complete while a loop hop was pending"
        )
    assert started
    assert process.exitcode == 0


def test_browser_capture_request_does_not_report_model_claim_as_success(chat_service):
    service, llm = chat_service
    llm.mock_tool_call_sequence = [
        [{"name": "save_view", "input": {"name": "Overview"}}],
        [{"name": "get_graph_stats", "input": {}}],
    ]
    llm.mock_text_response = "Saved successfully"
    result = service._processor.process_message(
        [{"role": "user", "content": "save"}], llm_provider=llm
    )
    assert result["content"] != llm.mock_text_response
    assert result["toolResult"]["extra_actions"][0]["action"] == "save_view"
