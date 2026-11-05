# covers: backend/service/attachment_tools.py
"""Attachment read tool envelopes, budgets, authorization, and dispatch."""

import asyncio

import pytest

from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.service import GraphService
from backend.service.tests.test_node_attachments import (
    FEDERATED_ID,
    _upload,
    client as client,
    schema_with_attachment_type as schema_with_attachment_type,
    service as service,
    storage as storage,
    store as store,
)


@pytest.fixture(autouse=True)
def enable_attachment_ceiling(monkeypatch):
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")


# ADR 0010: the tool shape remains separate from REST attachment responses.
def test_attachment_tools_hide_missing_and_invisible_nodes(service, storage):
    """G1: invisible and missing nodes have identical tool responses."""

    class NarrowHook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True, graph_access=GraphAccessNarrowing(enabled=True)
            )

    service._authorization_hook = NarrowHook()
    for method, args in (
        (service.attachment_tool_list, {}),
        (service.attachment_tool_read, {"attachment_id": "a" * 32}),
    ):
        assert method(node_id="foreign", **args) == method(node_id="missing", **args)
    result = service.attachment_tool_search(
        "policy", ["foreign", "missing", FEDERATED_ID]
    )
    assert result["unresolved_node_ids"] == ["foreign", "missing"]
    assert result["files"] == result["passages"] == []


def test_attachment_tool_passages_cite_sources_and_audit_once(service, client, caplog):
    """G2: every passage identifies its source; audit counts only returned text."""
    from backend.runtime.authorization import use_request_authorization

    record = _upload(client, "doc", b"policy human review").json()
    with (
        caplog.at_level("INFO", logger="attachments.audit"),
        use_request_authorization(
            actor_id="reader", headers={"x-correlation-id": "read-123"}
        ),
    ):
        result = service.attachment_tool_search("policy", ["doc"])
    passage = result["passages"][0]
    assert (passage["node_id"], passage["attachment_id"], passage["filename"]) == (
        "doc",
        record["id"],
        record["filename"],
    )
    assert passage["locator"]
    assert result["untrusted_content"] is True
    audits = [r for r in caplog.records if r.name == "attachments.audit"]
    assert len(audits) == 1
    assert audits[0].chars == len(passage["text"])
    assert audits[0].sha256 == record["sha256"]
    assert audits[0].actor == {"type": "user", "id": "reader"}
    import json
    import logging
    from backend.api_host.logging_config import StructuredJsonFormatter, TEXT_LOG_FORMAT

    for formatter in (logging.Formatter(TEXT_LOG_FORMAT), StructuredJsonFormatter()):
        line = formatter.format(audits[0])
        message = (
            json.loads(line)["message"]
            if isinstance(formatter, StructuredJsonFormatter)
            else line.split(": ", 1)[1]
        )
        emitted = json.loads(message)
        assert {
            k: emitted[k]
            for k in (
                "actor",
                "node_id",
                "attachment_id",
                "sha256",
                "chars",
                "outcome",
                "correlation_id",
            )
        } == {
            "actor": {"type": "user", "id": "reader"},
            "node_id": "doc",
            "attachment_id": record["id"],
            "sha256": record["sha256"],
            "chars": len(passage["text"]),
            "outcome": "ok",
            "correlation_id": "read-123",
        }


@pytest.mark.parametrize(
    "status", ["no_text_layer", "unsupported_type", "too_large", "timeout", "failed"]
)
def test_attachment_tool_reports_unreadable_and_budget_skips(
    service, client, monkeypatch, status
):
    """G3: every file not searched in full is reported with its status."""
    from dataclasses import replace
    from backend.core.attachment_extract import ExtractionResult
    from backend.service import attachment_tools

    first = _upload(client, "doc", b"policy").json()
    second = _upload(client, "doc", b"more policy").json()
    tools = service._attachment_read_tools
    tools.limits = replace(tools.limits, search_max_files=1)
    monkeypatch.setattr(
        attachment_tools.extraction, "extract", lambda *a: ExtractionResult(status)
    )
    result = service.attachment_tool_search("policy", ["doc"])
    assert {f["attachment_id"] for f in result["files"]} == {first["id"], second["id"]}
    assert {f["status"] for f in result["files"]} == {status, "skipped_budget"}
    assert result["passages"] == []
    assert service.attachment_tool_search("policy", ["doc"])["files"] == result["files"]


@pytest.mark.parametrize("status", ["ok", "failed"])
def test_attachment_search_rejected_text_exhausts_total_budget(
    service, client, monkeypatch, caplog, status
):
    from dataclasses import replace
    from backend.core.attachment_extract import ExtractionResult, Segment
    from backend.service import attachment_tools

    uploaded = [
        _upload(client, "doc", text, f"{i}.txt").json()
        for i, text in enumerate((b"first", b"oversized", b"last"))
    ]
    tools = service._attachment_read_tools
    tools.limits = replace(tools.limits, search_max_total_chars=10)
    calls = []

    def extract(data, *args):
        calls.append(data)
        return ExtractionResult(
            status if data == b"oversized" else "ok",
            (Segment("policy" if data == b"first" else "policy " * 2, {"line": 1}),),
        )

    monkeypatch.setattr(attachment_tools.extraction, "extract", extract)
    with caplog.at_level("INFO", logger="attachments.audit"):
        result = service.attachment_tool_search("policy", ["doc"])
    assert calls == [b"first", b"oversized"]
    assert result["searched_files"] == 2
    assert {f["attachment_id"]: f["status"] for f in result["files"]} == {
        record["id"]: "skipped_budget" for record in uploaded[1:]
    }
    assert {p["attachment_id"] for p in result["passages"]} == {uploaded[0]["id"]}
    audits = [r for r in caplog.records if r.name == "attachments.audit"]
    assert {r.attachment_id: r.outcome for r in audits} == {
        uploaded[0]["id"]: "ok",
        uploaded[1]["id"]: "skipped_budget",
    }
    assert len(audits) == 2


@pytest.mark.parametrize("warm_cache", [False, True])
def test_attachment_search_limits_extraction_to_remaining_chars(
    service, client, monkeypatch, warm_cache
):
    from dataclasses import replace
    from backend.core.attachment_extract import ExtractionResult, Segment
    from backend.service import attachment_tools

    uploaded = [
        _upload(client, "doc", text, f"{i}.txt").json()
        for i, text in enumerate((b"policy", b"policy second", b"last"))
    ]
    tools = service._attachment_read_tools
    tools.limits = replace(tools.limits, search_max_total_chars=10)
    calls = []

    def extract(data, content_type, limits):
        calls.append((data, limits.max_chars))
        text = data.decode()
        return ExtractionResult(
            "ok",
            (Segment(text[: limits.max_chars], {"line": 1}),),
            truncated=len(text) > limits.max_chars,
        )

    monkeypatch.setattr(attachment_tools.extraction, "extract", extract)
    if warm_cache:
        service.attachment_tool_read("doc", uploaded[1]["id"])
        calls.clear()
    result = service.attachment_tool_search("policy", ["doc"])
    assert calls == [(b"policy", 10), (b"policy second", 4)]
    assert result["searched_files"] == 2
    assert sum(len(p["text"]) for p in result["passages"]) <= 10
    assert {
        f["attachment_id"]: (f["status"], f["truncated"]) for f in result["files"]
    } == {
        uploaded[1]["id"]: ("ok", True),
        uploaded[2]["id"]: ("skipped_budget", False),
    }
    assert not tools.cache if not warm_cache else len(tools.cache) == 1
    # A limited search result must never replace the full attachment in the cache.
    read = service.attachment_tool_read("doc", uploaded[1]["id"])
    assert "".join(s["text"] for s in read["segments"]) == "policy second"


@pytest.mark.parametrize("failure", [None, "enumeration", "ranking"])
def test_attachment_empty_search_audits_once(service, monkeypatch, caplog, failure):
    from backend.service import attachment_tools

    def fail(*args, **kwargs):
        raise RuntimeError("confidential failure details")

    if failure == "enumeration":
        monkeypatch.setattr(service.attachment_settings.store, "list", fail)
    elif failure == "ranking":
        monkeypatch.setattr(attachment_tools, "rank_passages", fail)
    with caplog.at_level("INFO", logger="attachments.audit"):
        result = service.attachment_tool_search("policy", ["doc"])
    if failure:
        assert result["error_code"] == "failed"
    else:
        assert result["passages"] == result["files"] == []
    audits = [r for r in caplog.records if r.name == "attachments.audit"]
    assert len(audits) == 1
    assert audits[0].event == "attachment_content_read"
    assert audits[0].tool == "search_node_attachments"
    assert audits[0].outcome == ("failed" if failure else "ok")
    assert audits[0].attachment_id is None
    assert audits[0].chars == 0
    assert "confidential failure details" not in repr(audits[0].__dict__)


@pytest.mark.parametrize("failure", ["enumeration", "ranking"])
@pytest.mark.parametrize("warm_cache", [False, True])
def test_attachment_search_preserves_read_audits_on_later_failure(
    service, client, monkeypatch, caplog, failure, warm_cache
):
    from backend.service import attachment_tools

    secret = b"policy confidential content"
    record = _upload(client, "doc", secret).json()
    if warm_cache:
        service.attachment_tool_read("doc", record["id"])
    caplog.clear()

    def fail(*args, **kwargs):
        raise RuntimeError("later operation failed")

    if failure == "ranking":
        monkeypatch.setattr(attachment_tools, "rank_passages", fail)
    else:
        store = service.attachment_settings.store
        original_list = store.list

        def list_records(namespace, node_id):
            return fail() if node_id == "actor" else original_list(namespace, node_id)

        monkeypatch.setattr(store, "list", list_records)
    with caplog.at_level("INFO", logger="attachments.audit"):
        result = service.attachment_tool_search("policy", ["doc", "actor"])
    assert result["success"] is False
    audits = [r for r in caplog.records if r.name == "attachments.audit"]
    reads = [r for r in audits if r.attachment_id is not None]
    assert len(reads) == 1
    assert (reads[0].node_id, reads[0].attachment_id, reads[0].sha256) == (
        "doc",
        record["id"],
        record["sha256"],
    )
    assert reads[0].outcome == "ok"
    assert reads[0].chars == 0
    assert all(secret.decode() not in repr(r.__dict__) for r in audits)


@pytest.mark.parametrize("query", ["policy", "absent"])
def test_attachment_search_audits_every_extraction(
    service, client, monkeypatch, caplog, query
):
    from dataclasses import replace
    from backend.core.attachment_extract import ExtractionResult, Segment
    from backend.service import attachment_tools

    uploaded = [
        _upload(client, "doc", text, f"{i}.txt").json()
        for i, text in enumerate((b"first", b"second", b"failed"))
    ]
    tools = service._attachment_read_tools
    tools.limits = replace(tools.limits, search_max_top_k=1)
    secret = "policy confidential extraction text"

    def extract(data, *args):
        return (
            ExtractionResult("failed")
            if data == b"failed"
            else ExtractionResult("ok", (Segment(secret, {"line": 1}),))
        )

    monkeypatch.setattr(attachment_tools.extraction, "extract", extract)
    # A second call also verifies audit coverage for cached extraction results.
    for _ in range(2):
        caplog.clear()
        with caplog.at_level("INFO", logger="attachments.audit"):
            result = service.attachment_tool_search(query, ["doc"])
        audits = [r for r in caplog.records if r.name == "attachments.audit"]
        assert len(audits) == 3
        by_id = {r.attachment_id: r for r in audits}
        assert set(by_id) == {r["id"] for r in uploaded}
        for record in uploaded:
            audit = by_id[record["id"]]
            assert audit.node_id == "doc"
            assert audit.sha256 == record["sha256"]
            assert audit.outcome == ("failed" if record == uploaded[2] else "ok")
            assert audit.chars == sum(
                len(p["text"])
                for p in result["passages"]
                if p["attachment_id"] == record["id"]
            )
            assert secret not in repr(audit.__dict__)
        assert sum(r.chars == 0 for r in audits) >= 2


def test_attachment_tool_enforces_budgets_and_read_windows(
    service, client, monkeypatch
):
    """G4: passage text and read windows never exceed their character caps."""
    from dataclasses import replace
    from backend.core.attachment_extract import ExtractionResult, Segment
    from backend.service import attachment_tools

    record = _upload(client, "doc", b"policy").json()
    monkeypatch.setattr(
        attachment_tools.extraction,
        "extract",
        lambda *a: ExtractionResult(
            "ok",
            (
                Segment("policy " * 30, {"page": 1}),
                Segment("policy " * 30, {"page": 2}),
            ),
        ),
    )
    tools = service._attachment_read_tools
    tools.limits = replace(
        tools.limits,
        chunk_chars=30,
        chunk_overlap_chars=0,
        result_max_chars=35,
        read_max_chars=15,
    )
    result = service.attachment_tool_search("policy", ["doc"], top_k=20)
    assert 0 < sum(len(p["text"]) for p in result["passages"]) <= 35
    assert result["passages_truncated"] is True
    window = service.attachment_tool_read("doc", record["id"], offset=205, max_chars=50)
    assert window["returned_chars"] == 15
    assert [s["locator"] for s in window["segments"]] == [{"page": 1}, {"page": 2}]
    assert window["next_offset"] == 220
    assert (
        service.attachment_tool_read("doc", record["id"], offset=420)["segments"] == []
    )
    assert (
        service.attachment_tool_search("policy", ["doc"] * 21)["error_code"]
        == "too_many_nodes"
    )
    assert (
        service.attachment_tool_read("doc", record["id"], offset=-1)["error_code"]
        == "invalid_arguments"
    )


def test_attachment_text_cache_is_bounded_ephemeral_and_reauthorized(
    service, client, monkeypatch
):
    """G5: caches are bounded memory only and never bypass authorization."""
    from dataclasses import replace
    from backend.core.attachment_extract import ExtractionResult, Segment
    from backend.service import attachment_tools

    record = _upload(client, "doc", b"policy").json()
    calls = []

    def extract(*args):
        calls.append(None)
        return ExtractionResult("ok", (Segment("policy", {"line": 1}),))

    monkeypatch.setattr(attachment_tools.extraction, "extract", extract)
    tools = service._attachment_read_tools
    tools.limits = replace(tools.limits, text_cache_max_chars=400)
    service.attachment_tool_read("doc", record["id"])
    service.attachment_tool_read("doc", record["id"])
    assert len(calls) == 1
    other = _upload(client, "doc", b"policy", filename="other.txt").json()
    service.attachment_tool_read("doc", other["id"])
    service.attachment_tool_read("doc", record["id"])
    assert len(calls) == 3  # The least recently used entry was evicted.
    assert tools.cache_chars <= 400
    fresh = GraphService(
        service.storage, attachment_settings=service.attachment_settings
    )
    assert not fresh._attachment_read_tools.cache
    fresh.attachment_tool_read("doc", record["id"])
    assert len(calls) == 4

    class DenyHook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=context.target != "get_node_attachment"
            )

    service._authorization_hook = DenyHook()
    assert (
        service.attachment_tool_read("doc", record["id"])["error_code"]
        == "access_denied"
    )
    assert len(calls) == 4
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "off")
    assert fresh.attachment_tool_list("doc")["error_code"] == "access_denied"
    assert (
        fresh.attachment_tool_read("doc", record["id"])["error_code"] == "access_denied"
    )
    assert (
        fresh.attachment_tool_search("policy", ["doc"])["error_code"] == "access_denied"
    )
    assert len(calls) == 4


def test_attachment_read_dispatchers_use_tool_wrappers(service, client, caplog):
    """All dispatchers use envelopes, and Agent dispatch cannot reach REST reads."""
    from unittest.mock import MagicMock
    from backend.agents.mcp_loader import MCPLoader
    from backend.service.mcp_tools import register_mcp_tools
    from backend.ui.chat_service import ChatService

    record = _upload(client, "doc", b"policy").json()
    mcp = MagicMock()
    registered = {}

    def register(func):
        registered[func.__name__] = func
        return func

    mcp.tool.return_value = register
    tools = register_mcp_tools(mcp, service, attachment_access="read")
    chat = ChatService.__new__(ChatService)
    chat._graph_service = service
    chat_tools = chat._build_tools_map()
    loader = MCPLoader([])
    with caplog.at_level("INFO", logger="attachments.audit"):
        for name, args in (
            ("list_node_attachments", {"node_id": "doc"}),
            ("read_node_attachment", {"node_id": "doc", "attachment_id": record["id"]}),
            ("search_node_attachments", {"query": "policy", "node_ids": ["doc"]}),
        ):
            assert tools[name](**args)["untrusted_content"] is True
            assert asyncio.run(registered[name](**args))["untrusted_content"] is True
            assert chat_tools[name](**args)["untrusted_content"] is True
            assert (
                loader._execute_graph_tool(name, args, service, "agent")[
                    "untrusted_content"
                ]
                is True
            )
    assert {
        r.actor["type"] for r in caplog.records if r.name == "attachments.audit"
    } == {"mcp", "user", "agent"}
    for name in ("open_node_attachment", "get_node_attachment_record"):
        result = loader._execute_graph_tool(name, {}, service, "agent")
        assert set(result) == {"error"}
    assert "read_node_attachment" not in register_mcp_tools(mcp, service)
    empty = GraphService(service.storage)
    assert "list_node_attachments" not in register_mcp_tools(
        mcp, empty, attachment_access="read"
    )


def test_attachment_defaults_require_explicit_opt_in(service, monkeypatch):
    from backend.api_host.config import AppConfig
    from backend.runtime.attachment_access import attachment_tool_allowed

    monkeypatch.delenv("ATTACHMENT_CONTENT_ACCESS")
    monkeypatch.delenv("ASSISTANT_ATTACHMENT_ACCESS", raising=False)
    assert not attachment_tool_allowed("read_node_attachment", "read")
    assert service.attachment_tool_list("doc")["error_code"] == "access_denied"
    assert AppConfig().assistant_attachment_access == "off"


@pytest.mark.parametrize("kind", ["xlsx", "ods"])
def test_nameless_sheets_have_distinct_locators(kind):
    from backend.core import attachment_extract as ae
    from backend.core.tests.test_attachment_extract import _xlsx, _odf, _replace_member

    if kind == "xlsx":
        data = _xlsx({"A": [["i:one"]], "B": [["i:two"]]})
        import zipfile
        import io

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = (
                archive.read("xl/workbook.xml")
                .decode()
                .replace('name="A"', "")
                .replace('name="B"', "")
            )
        data = _replace_member(data, "xl/workbook.xml", body)
        result = ae.extract(data, ae.XLSX)
    else:
        table = "<table:table><table:table-row><table:table-cell><text:p>x</text:p></table:table-cell></table:table-row></table:table>"
        result = ae.extract(
            _odf(ae.ODS, "<office:spreadsheet>" + table * 2 + "</office:spreadsheet>"),
            ae.ODS,
        )
    assert result.status == "ok"
    assert [s.locator for s in result.segments] == [{"sheet": 1}, {"sheet": 2}]


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatcher", ["chat", "execute_tool"])
async def test_attachment_dispatch_keeps_extraction_off_loop(
    service, client, monkeypatch, dispatcher
):
    import threading
    from backend.service import attachment_tools
    from backend.ui.chat_logic import run_chat_off_event_loop
    from backend.core.attachment_extract import ExtractionResult, Segment
    from backend.runtime.authorization import use_request_authorization

    record = _upload(client, "doc", b"policy").json()
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    graph_threads = []
    original = service.storage.get_node

    def get_node(node_id):
        graph_threads.append(threading.get_ident())
        return original(node_id)

    def extract(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return ExtractionResult("ok", (Segment("policy", {"line": 1}),))

    monkeypatch.setattr(service.storage, "get_node", get_node)
    monkeypatch.setattr(attachment_tools.extraction, "extract", extract)
    name = "read_node_attachment"
    args = {"node_id": "doc", "attachment_id": record["id"]}
    if dispatcher == "chat":
        from backend.ui.chat_logic import ChatProcessor
        from backend.ui.tests.conftest import MockLLMProvider

        processor = ChatProcessor({name: service.attachment_tool_read})
        llm = MockLLMProvider()
        llm.mock_tool_calls = [{"name": name, "input": args}]
        call = run_chat_off_event_loop(
            processor.process_message,
            messages=[{"role": "user", "content": "read"}],
            llm_provider=llm,
            attachment_access=lambda _: "read",
        )
    else:
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from backend.api_host.tool_routes import register_tool_routes

        app = FastAPI()
        register_tool_routes(app, service, {name: service.attachment_tool_read}, True)

        async def execute():
            async with AsyncClient(
                transport=ASGITransport(app), base_url="http://test"
            ) as http:
                response = await http.post(
                    "/execute_tool", json={"tool_name": name, "arguments": args}
                )
                assert response.status_code == 200
                assert response.json()["returned_chars"] == 6

        call = execute()
    with use_request_authorization(actor_id="reader"):
        pending = asyncio.create_task(call)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert graph_threads == [loop_thread]
        finally:
            release.set()
        await pending
