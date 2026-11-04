# covers: backend/service/attachments.py, backend/service/attachment_tools.py
"""
Node attachment routes and service functions (ADR 0008 sections 4-8).

Each per-route row of ADR 0008's section 7 table, each limit of section 4 and
each refusal of section 5 has a test here.
"""

import asyncio
import json
import os

import pytest
from starlette.requests import ClientDisconnect
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import config_loader
from backend.core import GraphStorage, Node
from backend.core.attachment_store import (
    AttachmentLimits,
    FileAttachmentStore,
    hashed_key,
)
from backend.core.attachment_types import PDF, TEXT
from backend.runtime.authorization import (
    GRAPH_ACTION_READ,
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.service import GraphService, create_rest_router
from backend.service.attachments import AttachmentSettings, sanitize_filename

NS = "test"
LIMITS = AttachmentLimits(
    max_file_bytes=100, max_files_per_node=3, max_bytes_per_node=250
)
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 20
FEDERATED_ID = "federated::remote::abc"


@pytest.fixture(autouse=True)
def schema_with_attachment_type(tmp_path):
    config_file = tmp_path / "schema_config.json"
    config_file.write_text(
        json.dumps(
            {
                "schema": {
                    "node_types": {
                        "Document": {"fields": ["name"], "allows_attachments": True},
                        "Actor": {"fields": ["name"]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    previous = os.environ.get("SCHEMA_FILE")
    os.environ["SCHEMA_FILE"] = str(config_file)
    config_loader.reset_loader()
    yield
    if previous is None:
        os.environ.pop("SCHEMA_FILE", None)
    else:
        os.environ["SCHEMA_FILE"] = previous
    config_loader.reset_loader()


@pytest.fixture
def storage(tmp_path) -> GraphStorage:
    storage = GraphStorage(json_path=str(tmp_path / "graph.json"))
    storage.add_nodes(
        [
            Node(id="doc", type="Document", name="Doc"),
            Node(id="actor", type="Actor", name="Actor"),
            Node(id="old", type="Document", name="Old", archived=True),
            Node(id=FEDERATED_ID, type="Document", name="Remote"),
            Node(
                id="foreign",
                type="Document",
                name="Foreign",
                metadata={"origin_graph_id": "other-graph"},
            ),
        ],
        [],
    )
    return storage


@pytest.fixture
def store(tmp_path) -> FileAttachmentStore:
    return FileAttachmentStore(tmp_path / "attachments")


def _settings(store, **overrides):
    values = {"store": store, "namespace": NS, "limits": LIMITS}
    values.update(overrides)
    return AttachmentSettings(**values)


def _client(service: GraphService) -> TestClient:
    app = FastAPI()
    app.include_router(create_rest_router(service), prefix="/api")
    return TestClient(app)


@pytest.fixture
def service(storage, store) -> GraphService:
    return GraphService(storage, attachment_settings=_settings(store))


@pytest.fixture
def client(service) -> TestClient:
    return _client(service)


def _upload(client, node_id, data=b"hello world", filename="note.txt", **kwargs):
    headers = kwargs.pop("headers", {"content-type": "application/octet-stream"})
    return client.post(
        f"/api/nodes/{node_id}/attachments",
        params={"filename": filename},
        content=data,
        headers=headers,
        **kwargs,
    )


def _seed(store, node_id, data=b"seeded", filename="seed.txt"):
    """Put a file on a node directly through the store, bypassing the routes."""
    from backend.core.attachment_store import NewAttachment

    async def chunks():
        yield data

    async def run():
        staged = await store.stage(NS, node_id, chunks(), 10**6)
        return await store.commit(
            staged,
            NewAttachment(filename, "text/plain", TEXT, None),
            AttachmentLimits(),
            lambda: None,
        )

    return asyncio.run(run())


def _tmp_is_empty(store) -> bool:
    tmp = store.root / ".tmp"
    return not tmp.exists() or not any(tmp.iterdir())


def _error(response):
    return response.json()["detail"]["error"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_upload_list_download_delete(self, client, store):
        created = _upload(client, "doc", PDF_BYTES, "report.pdf")
        assert created.status_code == 201
        record = created.json()
        assert record["filename"] == "report.pdf"
        assert record["content_type"] == PDF
        assert record["size"] == len(PDF_BYTES)
        assert "declared_content_type" not in record

        listed = client.get("/api/nodes/doc/attachments").json()
        assert listed == {
            "attachments": [record],
            "limits": LIMITS.to_dict(),
            "uploads_allowed": True,
        }

        download = client.get(f"/api/nodes/doc/attachments/{record['id']}")
        assert download.status_code == 200
        assert download.content == PDF_BYTES

        deleted = client.delete(f"/api/nodes/doc/attachments/{record['id']}")
        assert deleted.status_code == 200
        assert deleted.json() == {"success": True}
        assert client.get("/api/nodes/doc/attachments").json()["attachments"] == []

    def test_download_carries_the_hardening_headers(self, client):
        record = _upload(client, "doc", PDF_BYTES, "report.pdf").json()
        response = client.get(f"/api/nodes/doc/attachments/{record['id']}")
        assert response.headers["content-type"] == PDF
        assert response.headers["content-disposition"] == (
            "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (
            response.headers["content-security-policy"] == "default-src 'none'; sandbox"
        )
        assert response.headers["cache-control"] == "private, no-store"

    def test_text_download_declares_utf8(self, client):
        record = _upload(client, "doc", "åäö".encode(), "notes.txt").json()
        response = client.get(f"/api/nodes/doc/attachments/{record['id']}")
        assert response.headers["content-type"] == "text/plain; charset=utf-8"

    def test_non_ascii_and_quote_in_name_cannot_break_the_header(self, client):
        record = _upload(client, "doc", b"x", 'rå "data";.txt').json()
        response = client.get(f"/api/nodes/doc/attachments/{record['id']}")
        assert record["filename"] == 'rå "data";.txt'
        assert response.headers["content-disposition"] == (
            'attachment; filename="r_ _data_;.txt"; '
            "filename*=UTF-8''r%C3%A5%20%22data%22%3B.txt"
        )

    def test_upload_does_not_touch_the_node(self, client, storage):
        before = storage.get_node("doc").model_dump()
        assert _upload(client, "doc").status_code == 201
        assert storage.get_node("doc").model_dump() == before

    def test_declared_type_is_never_served(self, client):
        record = _upload(
            client, "doc", PDF_BYTES, "x.pdf", headers={"content-type": "text/html"}
        ).json()
        response = client.get(f"/api/nodes/doc/attachments/{record['id']}")
        assert response.headers["content-type"] == PDF


# ---------------------------------------------------------------------------
# Per-route behaviour (ADR 0008 section 7 table)
# ---------------------------------------------------------------------------


class _DenyHook:
    def __init__(self, deny_action=None):
        self.deny_action = deny_action

    def evaluate(self, context):
        if self.deny_action in (None, context.action):
            return GraphAuthorizationDecision(allowed=False, reason="nope")
        return GraphAuthorizationDecision(allowed=True)


class _NarrowHook:
    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=True,
            graph_access=GraphAccessNarrowing(enabled=True, allow_local_graph=True),
        )


class TestAuthorization:
    def test_denied_decision_is_403_on_every_route(self, storage, store):
        record = _seed(store, "doc")
        client = _client(
            GraphService(
                storage,
                authorization_hook=_DenyHook(),
                attachment_settings=_settings(store),
            )
        )
        responses = [
            client.get("/api/nodes/doc/attachments"),
            _upload(client, "doc"),
            client.get(f"/api/nodes/doc/attachments/{record.id}"),
            client.delete(f"/api/nodes/doc/attachments/{record.id}"),
        ]
        assert [r.status_code for r in responses] == [403, 403, 403, 403]
        assert store.list(NS, "doc") == [record]
        assert _tmp_is_empty(store)

    def test_read_only_access_can_list_and_download_but_not_change(
        self, storage, store
    ):
        record = _seed(store, "doc")
        client = _client(
            GraphService(
                storage,
                authorization_hook=_DenyHook(deny_action="mutate"),
                attachment_settings=_settings(store),
            )
        )
        assert client.get("/api/nodes/doc/attachments").status_code == 200
        assert client.get(f"/api/nodes/doc/attachments/{record.id}").status_code == 200
        assert _upload(client, "doc").status_code == 403
        assert (
            client.delete(f"/api/nodes/doc/attachments/{record.id}").status_code == 403
        )
        assert store.list(NS, "doc") == [record]

    def test_actions_and_targets_follow_the_adr(self, storage, store):
        seen = []

        class RecordingHook:
            def evaluate(self, context):
                seen.append((context.action, context.target))
                return GraphAuthorizationDecision(allowed=True)

        record = _seed(store, "doc")
        client = _client(
            GraphService(
                storage,
                authorization_hook=RecordingHook(),
                attachment_settings=_settings(store),
            )
        )
        client.get("/api/nodes/doc/attachments")
        client.get(f"/api/nodes/doc/attachments/{record.id}")
        client.delete(f"/api/nodes/doc/attachments/{record.id}")
        _upload(client, "doc")
        assert (GRAPH_ACTION_READ, "list_node_attachments") in seen
        assert (GRAPH_ACTION_READ, "get_node_attachment") in seen
        assert ("mutate", "delete_node_attachment") in seen
        assert ("mutate", "add_node_attachment") in seen

    def test_invisible_node_is_indistinguishable_from_a_missing_one(
        self, storage, store
    ):
        record = _seed(store, "foreign")
        client = _client(
            GraphService(
                storage,
                authorization_hook=_NarrowHook(),
                attachment_settings=_settings(store),
            )
        )
        for node_id, attachment_id in (("foreign", record.id), ("missing", record.id)):
            responses = [
                client.get(f"/api/nodes/{node_id}/attachments"),
                _upload(client, node_id),
                client.get(f"/api/nodes/{node_id}/attachments/{attachment_id}"),
                client.delete(f"/api/nodes/{node_id}/attachments/{attachment_id}"),
            ]
            assert [r.status_code for r in responses] == [404] * 4
            assert {_error(r) for r in responses} == {"not_found"}
        assert store.list(NS, "foreign") == [record]


class TestFederatedNodes:
    def test_list_is_empty_and_uploads_are_not_allowed(self, client, store):
        _seed(store, FEDERATED_ID)
        body = client.get(f"/api/nodes/{FEDERATED_ID}/attachments").json()
        assert body["attachments"] == []
        assert body["uploads_allowed"] is False

    def test_upload_is_refused_before_reading_the_body(self, client, store):
        response = _upload(client, FEDERATED_ID, PDF_BYTES)
        assert response.status_code == 422
        assert _error(response) == "federated_node"
        assert store.list(NS, FEDERATED_ID) == []

    def test_download_and_delete_are_not_found(self, client, store):
        record = _seed(store, FEDERATED_ID)
        url = f"/api/nodes/{FEDERATED_ID}/attachments/{record.id}"
        assert client.get(url).status_code == 404
        assert client.delete(url).status_code == 404
        assert store.list(NS, FEDERATED_ID) == [record]


class TestAttachmentIds:
    @pytest.mark.parametrize(
        "bad_id", ["0" * 32, "A" * 32, "abc", "index.json", "..%2Findex.json"]
    )
    def test_malformed_or_unknown_id_is_not_found(self, client, store, bad_id):
        _seed(store, "doc")
        assert client.get(f"/api/nodes/doc/attachments/{bad_id}").status_code == 404
        assert client.delete(f"/api/nodes/doc/attachments/{bad_id}").status_code == 404
        assert len(store.list(NS, "doc")) == 1

    def test_id_of_another_nodes_file_is_not_found(self, client, store):
        record = _seed(store, "actor")
        assert client.get(f"/api/nodes/doc/attachments/{record.id}").status_code == 404
        assert (
            client.delete(f"/api/nodes/doc/attachments/{record.id}").status_code == 404
        )


class TestSchemaFlagAndArchive:
    def test_type_without_flag_refuses_uploads(self, client, store):
        response = _upload(client, "actor")
        assert response.status_code == 422
        assert _error(response) == "attachments_not_allowed"
        assert store.list(NS, "actor") == []

    def test_type_without_flag_keeps_existing_files_reachable(self, client, store):
        record = _seed(store, "actor")
        body = client.get("/api/nodes/actor/attachments").json()
        assert [a["id"] for a in body["attachments"]] == [record.id]
        assert body["uploads_allowed"] is False
        assert (
            client.get(f"/api/nodes/actor/attachments/{record.id}").status_code == 200
        )
        assert (
            client.delete(f"/api/nodes/actor/attachments/{record.id}").status_code
            == 200
        )

    def test_archived_node_refuses_uploads_but_keeps_its_files(self, client, store):
        record = _seed(store, "old")
        response = _upload(client, "old")
        assert response.status_code == 409
        assert _error(response) == "node_archived"
        body = client.get("/api/nodes/old/attachments").json()
        assert body["uploads_allowed"] is False
        assert client.get(f"/api/nodes/old/attachments/{record.id}").status_code == 200
        assert (
            client.delete(f"/api/nodes/old/attachments/{record.id}").status_code == 200
        )


# ---------------------------------------------------------------------------
# Limits (ADR 0008 section 4)
# ---------------------------------------------------------------------------


class TestLimits:
    def test_file_over_the_per_file_limit_is_413(self, client, store):
        response = _upload(client, "doc", b"x" * 101)
        assert response.status_code == 413
        assert _error(response) == "attachment_too_large"
        assert store.list(NS, "doc") == []
        assert _tmp_is_empty(store)

    def test_file_at_exactly_the_per_file_limit_is_accepted(self, client):
        assert _upload(client, "doc", b"x" * 100).status_code == 201

    def test_oversized_content_length_is_refused_before_reading(self, service, store):
        consumed = []

        async def body():
            consumed.append(True)
            yield b"x"

        result = asyncio.run(
            service.add_node_attachment(
                "doc",
                filename="a.txt",
                declared_content_type=None,
                content=body(),
                content_length=101,
            )
        )
        assert result["error_code"] == "attachment_too_large"
        assert consumed == []

    def test_understated_content_length_is_caught_while_streaming(self, client, store):
        response = _upload(
            client,
            "doc",
            b"x" * 150,
            headers={"content-type": "text/plain", "content-length": "10"},
        )
        assert response.status_code == 413
        assert _error(response) == "attachment_too_large"
        assert store.list(NS, "doc") == []
        assert _tmp_is_empty(store)

    def test_chunked_body_without_content_length_is_capped(self, client, store):
        def body():
            for _ in range(11):
                yield b"x" * 10

        response = client.post(
            "/api/nodes/doc/attachments",
            params={"filename": "a.txt"},
            content=body(),
        )
        assert response.status_code == 413
        assert _error(response) == "attachment_too_large"
        assert store.list(NS, "doc") == []
        assert _tmp_is_empty(store)

    def test_count_limit_is_409_before_any_byte_is_read(self, service, store):
        for _ in range(3):
            _seed(store, "doc", b"x")
        consumed = []

        async def body():
            consumed.append(True)
            yield b"x"

        result = asyncio.run(
            service.add_node_attachment(
                "doc", filename="a.txt", declared_content_type=None, content=body()
            )
        )
        assert result["error_code"] == "node_attachment_limit"
        assert consumed == []

    def test_count_limit_over_http_is_409(self, client, store):
        for _ in range(3):
            assert _upload(client, "doc", b"x").status_code == 201
        response = _upload(client, "doc", b"x")
        assert response.status_code == 409
        assert _error(response) == "node_attachment_limit"

    def test_node_total_smaller_than_file_limit_reports_node_limit(self, client, store):
        _seed(store, "doc", b"x" * 200)  # 50 bytes of budget left, file limit 100
        response = _upload(client, "doc", b"x" * 60)
        assert response.status_code == 409
        assert _error(response) == "node_attachment_limit"
        assert _upload(client, "doc", b"x" * 50).status_code == 201

    def test_node_total_breached_while_streaming_reports_node_limit(
        self, client, store
    ):
        _seed(store, "doc", b"x" * 200)

        def body():
            for _ in range(6):
                yield b"x" * 10

        response = client.post(
            "/api/nodes/doc/attachments", params={"filename": "a.txt"}, content=body()
        )
        assert response.status_code == 409
        assert _error(response) == "node_attachment_limit"
        assert _tmp_is_empty(store)

    def test_tie_between_the_caps_reports_the_file_limit(self, client, store):
        _seed(store, "doc", b"x" * 150)  # exactly 100 bytes of budget left
        response = _upload(client, "doc", b"x" * 101)
        assert response.status_code == 413
        assert _error(response) == "attachment_too_large"

    def test_exhausted_budget_refuses_even_a_small_file(self, client, store):
        _seed(store, "doc", b"x" * 250)
        response = _upload(client, "doc", b"x")
        assert response.status_code == 409
        assert _error(response) == "node_attachment_limit"


class TestCommitPrecondition:
    def _upload_while(self, service, storage, mutate):
        async def body():
            yield b"first "
            mutate(storage)
            yield b"second"

        return asyncio.run(
            service.add_node_attachment(
                "doc", filename="a.txt", declared_content_type=None, content=body()
            )
        )

    def test_node_archived_mid_upload_is_409_and_stores_nothing(
        self, service, storage, store
    ):
        result = self._upload_while(
            service, storage, lambda s: s.set_nodes_archived(["doc"], True)
        )
        assert result["error_code"] == "node_archived"
        assert store.list(NS, "doc") == []
        assert _tmp_is_empty(store)

    def test_node_deleted_mid_upload_is_404_and_stores_nothing(
        self, service, storage, store
    ):
        result = self._upload_while(
            service, storage, lambda s: s.delete_nodes(["doc"], confirmed=True)
        )
        assert result["error_code"] == "not_found"
        assert store.list(NS, "doc") == []
        assert _tmp_is_empty(store)


# ---------------------------------------------------------------------------
# Types and names (ADR 0008 sections 5 and 7)
# ---------------------------------------------------------------------------


class TestTypePolicy:
    @pytest.mark.parametrize(
        "data",
        [
            b"<!doctype html><script>alert(1)</script>",
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            b"MZ\x90\x00binary\x00",
            b"PK\x05\x06" + b"\x00" * 18,
        ],
    )
    def test_unsupported_content_is_415_and_leaves_nothing(self, client, store, data):
        response = _upload(client, "doc", data, "innocent.pdf")
        assert response.status_code == 415
        assert _error(response) == "unsupported_attachment_type"
        assert store.list(NS, "doc") == []
        assert _tmp_is_empty(store)

    def test_allowlist_narrows_the_accepted_types(self, storage, store):
        client = _client(
            GraphService(
                storage,
                attachment_settings=_settings(store, allowed_types=frozenset({PDF})),
            )
        )
        response = _upload(client, "doc", b"plain text", "a.txt")
        assert response.status_code == 415
        assert _upload(client, "doc", PDF_BYTES, "a.pdf").status_code == 201


class TestFilenames:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\me\\report.pdf", "report.pdf"),
            ("a\r\nb\x00c\x7f.txt", "abc.txt"),
            ("invoice\u202efdp.exe", "invoicefdp.exe"),
            ("  spaced.txt  ", "spaced.txt"),
            ("", "attachment"),
            (None, "attachment"),
            ("dir/", "attachment"),
            ("e\u0301.txt", "\u00e9.txt"),
        ],
    )
    def test_sanitize_filename(self, raw, expected):
        assert sanitize_filename(raw) == expected

    def test_long_name_is_cut_on_a_character_boundary(self):
        name = sanitize_filename("å" * 200)
        assert len(name.encode("utf-8")) <= 255
        assert name == "å" * 127

    def test_upload_stores_the_sanitised_name(self, client):
        record = _upload(client, "doc", b"x", "../secret/\r\nname.txt").json()
        assert record["filename"] == "name.txt"


# ---------------------------------------------------------------------------
# Lifecycle and availability
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_deleting_a_node_removes_its_attachments(self, service, store):
        _seed(store, "doc")
        _seed(store, "actor")
        result = service.delete_nodes(["doc"], confirmed=True)
        assert result["deleted_node_ids"] == ["doc"]
        assert list(store.iter_nodes(NS)) == ["actor"]

    def test_unconfirmed_delete_keeps_the_attachments(self, service, store):
        record = _seed(store, "doc")
        service.delete_nodes(["doc"], confirmed=False)
        assert service.storage.get_node("doc") is not None
        assert store.list(NS, "doc") == [record]

    def test_archiving_a_node_keeps_its_attachments(self, service, store):
        record = _seed(store, "doc")
        service.archive_nodes(["doc"])
        assert store.list(NS, "doc") == [record]


class TestUnavailable:
    def test_routes_answer_503_without_attachment_settings(self, storage):
        client = _client(GraphService(storage))
        responses = [
            client.get("/api/nodes/doc/attachments"),
            _upload(client, "doc"),
            client.get(f"/api/nodes/doc/attachments/{'a' * 32}"),
            client.delete(f"/api/nodes/doc/attachments/{'a' * 32}"),
        ]
        assert [r.status_code for r in responses] == [503] * 4
        assert {_error(r) for r in responses} == {"attachments_unavailable"}

    def test_node_delete_without_attachment_settings_still_works(self, storage):
        service = GraphService(storage)
        assert service.delete_nodes(["doc"], confirmed=True)["deleted_node_ids"] == [
            "doc"
        ]


class TestNonHttpCallers:
    def test_service_upload_accepts_plain_bytes(self, service, store):
        result = asyncio.run(
            service.add_node_attachment(
                "doc",
                filename="a.pdf",
                declared_content_type="application/pdf",
                content=PDF_BYTES,
            )
        )
        assert result["success"] is True
        record = service.get_node_attachment_record("doc", result["attachment"]["id"])
        assert record["attachment"]["content_type"] == PDF
        opened = service.open_node_attachment("doc", result["attachment"]["id"])
        with opened["stream"] as handle:
            assert handle.read() == PDF_BYTES

    def test_service_upload_of_oversized_bytes_is_refused(self, service, store):
        result = asyncio.run(
            service.add_node_attachment(
                "doc", filename="a.txt", declared_content_type=None, content=b"x" * 101
            )
        )
        assert result["error_code"] == "attachment_too_large"
        assert store.list(NS, "doc") == []

    def test_uploader_is_recorded_from_the_request_actor(self, service, store):
        from backend.runtime.authorization import use_request_authorization

        async def run():
            with use_request_authorization(actor_id="user-1", actor_type="user"):
                return await service.add_node_attachment(
                    "doc", filename="a.txt", declared_content_type=None, content=b"x"
                )

        result = asyncio.run(run())
        assert result["attachment"]["uploaded_by"] == "user-1"


class TestRefusalsReadNoBody:
    """Every pre-check refusal answers before the body iterator is touched."""

    def _upload_counting(self, service, node_id):
        consumed = []

        async def body():
            consumed.append(True)
            yield PDF_BYTES

        result = asyncio.run(
            service.add_node_attachment(
                node_id, filename="a.pdf", declared_content_type=None, content=body()
            )
        )
        return result, consumed

    @pytest.mark.parametrize(
        "node_id, code",
        [
            (FEDERATED_ID, "federated_node"),
            ("actor", "attachments_not_allowed"),
            ("old", "node_archived"),
            ("missing", "not_found"),
        ],
    )
    def test_node_refusals(self, service, node_id, code):
        result, consumed = self._upload_counting(service, node_id)
        assert result["error_code"] == code
        assert consumed == []

    def test_denied_authorization(self, storage, store):
        service = GraphService(
            storage,
            authorization_hook=_DenyHook(),
            attachment_settings=_settings(store),
        )
        result, consumed = self._upload_counting(service, "doc")
        assert result["error_code"] == "access_denied"
        assert consumed == []


class TestStagedFileCleanup:
    def test_unexpected_commit_failure_removes_the_staged_file(
        self, service, store, monkeypatch
    ):
        async def broken_commit(*args, **kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(store, "commit", broken_commit)
        with pytest.raises(RuntimeError):
            asyncio.run(
                service.add_node_attachment(
                    "doc",
                    filename="a.pdf",
                    declared_content_type=None,
                    content=PDF_BYTES,
                )
            )
        assert _tmp_is_empty(store)
        assert store.list(NS, "doc") == []

    def test_cancelled_upload_removes_the_staged_file(
        self, service, store, monkeypatch
    ):
        async def cancelled_commit(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(store, "commit", cancelled_commit)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                service.add_node_attachment(
                    "doc",
                    filename="a.pdf",
                    declared_content_type=None,
                    content=PDF_BYTES,
                )
            )
        assert _tmp_is_empty(store)


class TestExtensionAgreesWithType:
    @pytest.mark.parametrize(
        "filename, data, stored",
        [
            ("report.pdf", PDF_BYTES, "report.pdf"),
            ("REPORT.PDF", PDF_BYTES, "REPORT.PDF"),
            ("report.exe", PDF_BYTES, "report.exe.pdf"),
            ("report", PDF_BYTES, "report.pdf"),
            ("run.bat", b"echo hi", "run.bat.txt"),
            ("run.bat. ", b"echo hi", "run.bat.txt"),
            ("page.HTML", b"hello <b>x</b>", "page.HTML.txt"),
            ("notes.md", b"# notes", "notes.md"),
            ("data.json", b'{"a": 1}', "data.json"),
            ("invoice.rtf", b"{\\rtf1 hello}", "invoice.rtf.txt"),
            ("host.rdp", b"full address:s:x", "host.rdp.txt"),
            ("tool.py", b"print(1)", "tool.py.txt"),
            ("README", b"hello", "README"),
            ("table.csv", b"a,b", "table.csv"),
        ],
    )
    def test_stored_name(self, client, filename, data, stored):
        response = _upload(client, "doc", data, filename)
        assert response.status_code == 201
        assert response.json()["filename"] == stored

    def test_macro_document_name_is_saved_as_its_plain_type(self, storage, store):
        client = _client(
            GraphService(
                storage,
                attachment_settings=_settings(
                    store, limits=AttachmentLimits(max_file_bytes=10_000)
                ),
            )
        )
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<x/>")
            archive.writestr("word/vbaProject.bin", "macro")
        response = _upload(client, "doc", buffer.getvalue(), "budget.docm")
        assert response.status_code == 201
        assert response.json()["filename"] == "budget.docm.docx"

    def test_appended_extension_keeps_the_name_within_255_bytes(self, client):
        response = _upload(client, "doc", b"x", "a" * 251 + ".bat")
        name = response.json()["filename"]
        assert name.endswith(".txt")
        assert len(name.encode("utf-8")) <= 255


class TestNodeDeleteIsolation:
    def test_failing_attachment_cleanup_never_fails_the_node_delete(
        self, service, store, monkeypatch
    ):
        _seed(store, "doc")

        def broken(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(store, "delete_node", broken)
        result = service.delete_nodes(["doc"], confirmed=True)
        assert result["deleted_node_ids"] == ["doc"]
        assert service.storage.get_node("doc") is None


class TestReusedNodeId:
    """A node id created again never inherits the files of the node it had."""

    def _leave_orphan(self, service, store, monkeypatch, node_id="doc"):
        record = _seed(store, node_id)
        # The graph delete succeeds but the attachment cleanup does not.
        monkeypatch.setattr(store, "delete_node", lambda *a, **k: 0)
        service.delete_nodes([node_id], confirmed=True)
        monkeypatch.undo()
        assert store.list(NS, node_id) == [record]
        return record

    def test_recreated_id_lists_none_of_the_deleted_nodes_files(
        self, service, client, store, monkeypatch
    ):
        orphan = self._leave_orphan(service, store, monkeypatch)
        result = service.add_nodes(
            [{"id": "doc", "type": "Document", "name": "New"}], []
        )
        assert result["success"] is True
        response = client.get("/api/nodes/doc/attachments")
        assert response.json()["attachments"] == []
        assert client.get(f"/api/nodes/doc/attachments/{orphan.id}").status_code == 404
        # Quarantined, not destroyed.
        quarantine = store.root / hashed_key(NS) / ".quarantine"
        (target,) = quarantine.iterdir()
        assert (target / "blobs" / orphan.id).read_bytes() == b"seeded"

    def test_new_nodes_own_uploads_are_kept(self, service, client, store, monkeypatch):
        self._leave_orphan(service, store, monkeypatch)
        service.add_nodes([{"id": "doc", "type": "Document", "name": "New"}], [])
        uploaded = _upload(client, "doc").json()
        listed = client.get("/api/nodes/doc/attachments").json()["attachments"]
        assert [r["id"] for r in listed] == [uploaded["id"]]

    def test_upload_landing_between_create_and_quarantine_is_kept(
        self, service, store, monkeypatch
    ):
        orphan = self._leave_orphan(service, store, monkeypatch)
        real_add = service.storage.add_nodes
        landed = []

        def add_then_upload(*args, **kwargs):
            result = real_add(*args, **kwargs)
            landed.append(_seed(store, "doc", b"fresh"))
            return result

        monkeypatch.setattr(service.storage, "add_nodes", add_then_upload)
        service.add_nodes([{"id": "doc", "type": "Document", "name": "New"}], [])
        assert store.list(NS, "doc") == landed
        assert store.get_record(NS, "doc", orphan.id) is None

    def test_refused_create_leaves_a_live_nodes_files_alone(self, service, store):
        record = _seed(store, "doc")
        result = service.add_nodes(
            [{"id": "doc", "type": "Document", "name": "Dup"}], []
        )
        assert result["success"] is False
        assert store.list(NS, "doc") == [record]

    def test_failing_quarantine_never_fails_the_create(
        self, service, store, monkeypatch
    ):
        self._leave_orphan(service, store, monkeypatch)

        def broken(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(store, "quarantine", broken)
        result = service.add_nodes(
            [{"id": "doc", "type": "Document", "name": "New"}], []
        )
        assert result["success"] is True
        assert service.storage.get_node("doc") is not None

    def test_failing_snapshot_never_fails_the_create(self, service, store, monkeypatch):
        self._leave_orphan(service, store, monkeypatch)

        def broken(*args, **kwargs):
            raise OSError("unreadable")

        monkeypatch.setattr(store, "list", broken)
        result = service.add_nodes(
            [{"id": "doc", "type": "Document", "name": "New"}], []
        )
        assert result["success"] is True

    @pytest.mark.parametrize(
        "extra_nodes, edges",
        [
            ([], [{"source": "doc", "target": "no-such-node", "type": "RELATES_TO"}]),
            ([{"id": "actor", "type": "Actor", "name": "Dup"}], []),
            (
                [],
                [
                    {"id": "e1", "source": "doc", "target": "actor"},
                    {"id": "e1", "source": "doc", "target": "actor"},
                ],
            ),
        ],
        ids=["bad-edge", "duplicate-later-in-batch", "duplicate-edge-id"],
    )
    def test_node_kept_by_a_failed_call_loses_the_leftovers_too(
        self, service, store, monkeypatch, extra_nodes, edges
    ):
        orphan = self._leave_orphan(service, store, monkeypatch)
        nodes = [{"id": "doc", "type": "Document", "name": "New"}, *extra_nodes]
        result = service.add_nodes(nodes, edges)
        assert result["success"] is False
        assert "landed_node_ids" not in result
        assert service.storage.get_node("doc") is not None
        assert store.get_record(NS, "doc", orphan.id) is None

    def test_create_refused_by_the_store_moves_nothing(
        self, service, store, monkeypatch
    ):
        # The id is absent from this instance's view but taken in the store,
        # e.g. by a node another instance created: its files are not leftovers.
        record = self._leave_orphan(service, store, monkeypatch)
        from backend.core.storage_backends import EntityRevisionConflict

        def refused(*args, **kwargs):
            raise EntityRevisionConflict("node", "doc", None, 3)

        monkeypatch.setattr(service.storage, "add_nodes", refused)
        result = service.add_nodes(
            [{"id": "doc", "type": "Document", "name": "New"}], []
        )
        assert result["success"] is False
        assert store.list(NS, "doc") == [record]

    @pytest.mark.parametrize(
        "extra_nodes, edges",
        [
            ([], [{"source": "doc", "target": "no-such-node"}]),
            ([{"id": "actor", "type": "Actor", "name": "Dup"}], []),
            (
                [],
                [
                    {"id": "e1", "source": "doc", "target": "actor"},
                    {"id": "e1", "source": "doc", "target": "actor"},
                ],
            ),
        ],
        ids=["bad-edge", "duplicate-later-in-batch", "duplicate-edge-id"],
    )
    @pytest.mark.parametrize("store_reread", [False, True], ids=["undo", "re-read"])
    def test_node_the_store_refuses_on_a_failed_call_moves_nothing(
        self, service, store, monkeypatch, extra_nodes, edges, store_reread
    ):
        # The call fails part-way; persisting the node that landed is then
        # refused by the store, which undoes it - or re-reads a graph where
        # another writer's node holds the id. Ours never stayed.
        record = self._leave_orphan(service, store, monkeypatch)
        from backend.core.storage_backends import EntityRevisionConflict

        storage = service.storage

        def refused(changes, undo, *args, **kwargs):
            storage._undo(undo)
            if store_reread:
                theirs = Node(id="doc", type="Document", name="Theirs")
                storage.nodes["doc"] = theirs
            raise EntityRevisionConflict("node", "doc", None, 3)

        monkeypatch.setattr(storage, "_commit", refused)
        nodes = [{"id": "doc", "type": "Document", "name": "New"}, *extra_nodes]
        result = service.add_nodes(nodes, edges)
        assert result["success"] is False
        assert store.list(NS, "doc") == [record]

    def test_leftovers_on_several_ids_are_all_quarantined(
        self, service, store, monkeypatch
    ):
        orphans = {n: self._leave_orphan(service, store, monkeypatch, n) for n in "abc"}
        nodes = [{"id": n, "type": "Document", "name": n} for n in "abc"]
        assert service.add_nodes(nodes, [])["success"] is True
        for node_id, orphan in orphans.items():
            assert store.get_record(NS, node_id, orphan.id) is None

    def test_first_failing_quarantine_does_not_spare_the_other_ids(
        self, service, store, monkeypatch
    ):
        orphans = {n: self._leave_orphan(service, store, monkeypatch, n) for n in "abc"}
        real_quarantine = store.quarantine
        order = []

        def first_fails(namespace, node_id, ids):
            order.append(node_id)
            if node_id == "a":
                raise OSError("read-only filesystem")
            return real_quarantine(namespace, node_id, ids)

        monkeypatch.setattr(store, "quarantine", first_fails)
        nodes = [{"id": n, "type": "Document", "name": n} for n in "abc"]
        assert service.add_nodes(nodes, [])["success"] is True
        assert order == ["a", "b", "c"]  # the failing one is tried first
        assert store.get_record(NS, "a", orphans["a"].id) == orphans["a"]
        assert store.get_record(NS, "b", orphans["b"].id) is None
        assert store.get_record(NS, "c", orphans["c"].id) is None

    def test_creates_without_leftovers_never_touch_the_store(
        self, service, store, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(store, "quarantine", lambda *a: calls.append(a) or 0)
        _seed(store, "doc")
        service.add_nodes([{"type": "Document", "name": "No id"}], [])
        service.add_nodes([{"id": "fresh", "type": "Document", "name": "F"}], [])
        service.add_nodes([{"id": "doc", "type": "Document", "name": "Live"}], [])
        assert calls == []
        assert not (store.root / hashed_key(NS) / ".quarantine").exists()


class TestStreamCapUsesNodeBudget:
    def test_stream_without_length_stops_at_the_remaining_budget(self, service, store):
        _seed(store, "doc", b"x" * 200)  # 50 bytes of node budget left
        sent = []

        async def body():
            for _ in range(10):
                sent.append(10)
                yield b"x" * 10

        result = asyncio.run(
            service.add_node_attachment(
                "doc", filename="a.txt", declared_content_type=None, content=body()
            )
        )
        assert result["error_code"] == "node_attachment_limit"
        # The cap is the 50-byte budget, not the 100-byte file limit.
        assert sum(sent) <= 60
        assert _tmp_is_empty(store)

    def test_budget_already_overrun_by_lowered_limit_refuses(self, storage, store):
        _seed(store, "doc", b"x" * 200)
        service = GraphService(
            storage,
            attachment_settings=_settings(
                store,
                limits=AttachmentLimits(
                    max_file_bytes=100, max_files_per_node=3, max_bytes_per_node=150
                ),
            ),
        )
        result = asyncio.run(
            service.add_node_attachment(
                "doc", filename="a.txt", declared_content_type=None, content=b"x"
            )
        )
        assert result["error_code"] == "node_attachment_limit"


class TestFederatedPrefixIsExact:
    def test_single_colon_id_is_an_ordinary_node(self, storage, client):
        storage.add_nodes([Node(id="federated:x", type="Document", name="Local")], [])
        assert _upload(client, "federated:x").status_code == 201


class TestContentDispositionHeader:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("a/b.txt", "attachment; filename=\"a/b.txt\"; filename*=UTF-8''a%2Fb.txt"),
            (
                'q"\\\r\n.txt',
                "attachment; filename=\"q____.txt\"; filename*=UTF-8''q%22%5C%0D%0A.txt",
            ),
        ],
    )
    def test_header_is_ascii_and_cannot_be_split(self, name, expected):
        from backend.service.rest_api import _content_disposition

        header = _content_disposition(name)
        assert header == expected
        assert header.isascii()
        assert "\r" not in header and "\n" not in header

    def test_csv_download_declares_utf8_and_length(self, client):
        record = _upload(client, "doc", b"a,b\n1,2\n", "t.csv").json()
        response = client.get(f"/api/nodes/doc/attachments/{record['id']}")
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert response.headers["content-length"] == "8"


class TestRequestContextReachesOffLoopRoutes:
    def test_hook_sees_request_actor_on_every_route(self, storage, store):
        actors = []

        class ActorHook:
            def evaluate(self, context):
                actors.append((context.target, context.actor.get("actor_id")))
                return GraphAuthorizationDecision(allowed=True)

        record = _seed(store, "doc")
        client = _client(
            GraphService(
                storage,
                authorization_hook=ActorHook(),
                attachment_settings=_settings(store),
            )
        )
        headers = {"x-communityoverview-actor-id": "alice"}
        client.get("/api/nodes/doc/attachments", headers=headers)
        client.get(f"/api/nodes/doc/attachments/{record.id}", headers=headers)
        client.delete(f"/api/nodes/doc/attachments/{record.id}", headers=headers)
        assert actors == [
            ("list_node_attachments", "alice"),
            ("get_node_attachment", "alice"),
            ("delete_node_attachment", "alice"),
        ]


class TestLateChecks:
    def test_content_length_over_node_budget_is_refused_without_reading(
        self, service, store
    ):
        _seed(store, "doc", b"x" * 200)  # 50 bytes of budget left, file limit 100
        consumed = []

        async def body():
            consumed.append(True)
            yield b"x" * 60

        result = asyncio.run(
            service.add_node_attachment(
                "doc",
                filename="a.txt",
                declared_content_type=None,
                content=body(),
                content_length=60,
            )
        )
        assert result["error_code"] == "node_attachment_limit"
        assert consumed == []

    def test_node_becoming_invisible_mid_upload_stores_nothing(self, storage, store):
        class NarrowingHook:
            """Local graph only: a node tagged with another graph is invisible."""

            def evaluate(self, context):
                return GraphAuthorizationDecision(
                    allowed=True,
                    graph_access=GraphAccessNarrowing(
                        enabled=True, allow_local_graph=True
                    ),
                )

        service = GraphService(
            storage,
            authorization_hook=NarrowingHook(),
            attachment_settings=_settings(store),
        )

        async def body():
            yield b"first "
            # The node moves to a graph this caller's narrowing cannot see.
            storage.update_node("doc", {"metadata": {"origin_graph_id": "elsewhere"}})
            yield b"second"

        result = asyncio.run(
            service.add_node_attachment(
                "doc", filename="a.txt", declared_content_type=None, content=body()
            )
        )
        assert result["error_code"] == "not_found"
        assert store.list(NS, "doc") == []
        assert _tmp_is_empty(store)


class TestAlignExtensionUnit:
    @pytest.mark.parametrize(
        "name, content_type, expected",
        [
            ("x.bat.", TEXT, "x.bat.txt"),
            ("x.bat . ", TEXT, "x.bat.txt"),
            ("a.pdf .", PDF, "a.pdf ."),
            ("x.png", "image/jpeg", "x.png.jpg"),
            ("photo.jpeg", "image/jpeg", "photo.jpeg"),
        ],
    )
    def test_align_extension(self, name, content_type, expected):
        from backend.service.attachments import align_extension

        assert align_extension(name, content_type) == expected

    def test_truncation_never_leaves_a_partial_character(self):
        from backend.service.attachments import align_extension

        name = "\U0001f600" * 63 + ".bin"  # 256 bytes before the suffix
        result = align_extension(name, PDF)
        assert result.endswith(".pdf")
        assert len(result.encode("utf-8")) <= 255
        assert "�" not in result


class TestSchemaClientSurface:
    """Clients read ``allows_attachments`` unconditionally from both surfaces."""

    @staticmethod
    def _mcp_get_schema(service):
        from unittest.mock import MagicMock, Mock

        from backend.service import register_mcp_tools

        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        return register_mcp_tools(mock_mcp, service)["get_schema"]()

    @pytest.fixture(params=["rest", "mcp"])
    def payload(self, request, service, client):
        if request.param == "rest":
            response = client.get("/api/schema")
            assert response.status_code == 200
            return response.json()
        return self._mcp_get_schema(service)

    def test_every_node_type_carries_a_bool_flag(self, payload):
        node_types = payload["node_types"]
        assert {"Document", "Actor"} <= set(node_types)
        for name, cfg in node_types.items():
            assert isinstance(cfg["allows_attachments"], bool), name

    def test_flag_reflects_the_declared_schema(self, payload):
        assert payload["node_types"]["Document"]["allows_attachments"] is True
        assert payload["node_types"]["Actor"]["allows_attachments"] is False

    def test_payload_matches_the_loader_projection(self, payload):
        assert payload == config_loader.get_schema()


# ---------------------------------------------------------------------------
# Residue of the review loop: guards the happy-path tests do not reach
# ---------------------------------------------------------------------------


def _spy_store(store):
    """A store whose every public call is recorded; behaviour is unchanged."""
    from unittest.mock import MagicMock

    return MagicMock(wraps=store, spec=FileAttachmentStore)


class TestIdGuardsPrecedeTheStore:
    BAD_ID = "A" * 32

    def test_malformed_id_never_reaches_the_store_on_any_route(self, storage, store):
        spy = _spy_store(store)
        client = _client(GraphService(storage, attachment_settings=_settings(spy)))
        url = f"/api/nodes/doc/attachments/{self.BAD_ID}"
        assert client.get(url).status_code == 404
        assert client.delete(url).status_code == 404
        # The ops-level record lookup has no route of its own.
        result = GraphService(
            storage, attachment_settings=_settings(spy)
        ).get_node_attachment_record("doc", self.BAD_ID)
        assert result["error_code"] == "not_found"
        federated = GraphService(
            storage, attachment_settings=_settings(spy)
        ).get_node_attachment_record(FEDERATED_ID, "0" * 32)
        assert federated["error_code"] == "not_found"
        assert spy.method_calls == []

    def test_federated_node_never_reaches_the_store_on_read_and_delete(
        self, storage, store
    ):
        spy = _spy_store(store)
        client = _client(GraphService(storage, attachment_settings=_settings(spy)))
        valid_id = "0" * 32
        url = f"/api/nodes/{FEDERATED_ID}/attachments/{valid_id}"
        assert client.get(url).status_code == 404
        assert client.delete(url).status_code == 404
        assert spy.method_calls == []


class TestAllowsAttachmentsIsExactlyTrue:
    # The schema model coerces "yes" or 1 to True before this check sees them,
    # so the check is exercised on the schema dict it reads.
    @pytest.mark.parametrize("flag", ["false", "yes", 1, {"on": True}, None])
    def test_a_truthy_non_boolean_flag_does_not_enable_uploads(self, monkeypatch, flag):
        from backend.service.attachments import type_allows_attachments

        monkeypatch.setattr(
            config_loader,
            "get_schema",
            lambda: {"node_types": {"Document": {"allows_attachments": flag}}},
        )
        assert type_allows_attachments(Node(id="d", type="Document", name="D")) is False

    def test_the_boolean_true_flag_enables_uploads(self, monkeypatch):
        from backend.service.attachments import type_allows_attachments

        monkeypatch.setattr(
            config_loader,
            "get_schema",
            lambda: {"node_types": {"Document": {"allows_attachments": True}}},
        )
        assert type_allows_attachments(Node(id="d", type="Document", name="D")) is True


class TestDeclaredContentTypeSanitising:
    def test_is_cut_at_255_characters_and_shorter_values_are_kept(self):
        from backend.service.attachments import _sanitize_declared_content_type

        assert _sanitize_declared_content_type("t" * 255) == "t" * 255
        assert _sanitize_declared_content_type("t" * 300) == "t" * 255

    def test_non_printable_characters_are_dropped_and_the_value_trimmed(self):
        from backend.service.attachments import _sanitize_declared_content_type

        assert _sanitize_declared_content_type(" text/\x00pl\nain\x7f​ ") == (
            "text/plain"
        )
        assert _sanitize_declared_content_type(None) == ""
        # Filtered first, then cut: control characters do not eat the budget.
        assert _sanitize_declared_content_type("\x00" * 10 + "t" * 255) == "t" * 255
        # Trim after the filter: a character that only the filter removes
        # must not leave a trailing space behind.
        assert _sanitize_declared_content_type("a \x00") == "a"
        # Trim before the cut: leading whitespace does not eat the budget.
        assert _sanitize_declared_content_type(" " + "t" * 255) == "t" * 255

    def test_the_stored_declared_type_is_the_sanitised_one(self, client, store):
        headers = {"content-type": "text/plain; x=" + "a" * 400}
        assert _upload(client, "doc", headers=headers).status_code == 201
        declared = store.list(NS, "doc")[0].declared_content_type
        assert len(declared) == 255

    def test_no_control_characters_survive_end_to_end(self, client, store):
        headers = {"content-type": "text/pl\x01ain; x=\x7f1"}
        assert _upload(client, "doc", headers=headers).status_code == 201
        declared = store.list(NS, "doc")[0].declared_content_type
        assert declared and declared.isprintable()


class TestFilenameByteBoundary:
    def test_255_bytes_are_kept_and_256_are_cut_to_255(self):
        assert sanitize_filename("a" * 255) == "a" * 255
        assert sanitize_filename("a" * 256) == "a" * 255


class TestQueryAndDownloadHandling:
    def test_filename_query_is_capped_at_4096_characters(self, client, store):
        at_cap = client.post(
            "/api/nodes/doc/attachments",
            params={"filename": "n" * 4096},
            content=b"x",
        )
        over_cap = client.post(
            "/api/nodes/doc/attachments",
            params={"filename": "n" * 4097},
            content=b"x",
        )
        assert at_cap.status_code == 201
        assert over_cap.status_code == 422
        assert len(store.list(NS, "doc")) == 1

    def test_download_closes_the_stored_file_handle(self, storage, store):
        record = _seed(store, "doc", b"payload")
        opened = []
        real_open_read = store.open_read

        def tracking(*args):
            handle = real_open_read(*args)
            opened.append(handle)
            return handle

        store.open_read = tracking
        client = _client(GraphService(storage, attachment_settings=_settings(store)))
        response = client.get(f"/api/nodes/doc/attachments/{record.id}")
        assert response.content == b"payload"
        assert len(opened) == 1 and opened[0].closed

    @pytest.mark.parametrize("spec_version", [None, "2.4"])
    def test_download_closes_the_handle_when_the_client_disconnects(
        self, storage, store, spec_version
    ):
        record = _seed(store, "doc", b"x" * 90)
        opened = []
        real_open_read = store.open_read

        def tracking(*args):
            handle = real_open_read(*args)
            opened.append(handle)
            return handle

        store.open_read = tracking
        app = FastAPI()
        app.include_router(
            create_rest_router(
                GraphService(storage, attachment_settings=_settings(store))
            ),
            prefix="/api",
        )

        async def run():
            sent_start = asyncio.Event()

            async def receive():
                await sent_start.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    sent_start.set()
                    if spec_version is None:
                        # Stall until the disconnect cancels this send, so it
                        # always lands mid-stream.
                        await asyncio.Event().wait()
                elif spec_version is not None:
                    # ASGI spec 2.4+: a send to a gone client raises.
                    raise OSError("client disconnected")

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": f"/api/nodes/doc/attachments/{record.id}",
                "raw_path": b"",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            }
            if spec_version is not None:
                scope["asgi"]["spec_version"] = spec_version
            raised = False
            try:
                await app(scope, receive, send)
            except ClientDisconnect:
                # Spec 2.4+ servers see this raised out of the app.
                raised = True
            assert raised == (spec_version is not None)
            # Closed by the time the response returns, not whenever garbage
            # collection frees the body iterator.
            assert len(opened) == 1 and opened[0].closed

        asyncio.run(run())

    # Only the 2.4 case exercises the deferred close: below 2.4 anyio waits for
    # the reading thread before the response's finally runs, so [None] passes
    # with or without it and guards against starlette/anyio regressions only.
    @pytest.mark.parametrize("spec_version", [None, "2.4"])
    def test_disconnect_never_closes_the_handle_during_an_active_read(
        self, storage, store, spec_version
    ):
        import threading

        record = _seed(store, "doc", b"x" * 90)
        app = FastAPI()
        app.include_router(
            create_rest_router(
                GraphService(storage, attachment_settings=_settings(store))
            ),
            prefix="/api",
        )

        async def run():
            loop = asyncio.get_running_loop()
            read_blocked = asyncio.Event()
            release = threading.Event()
            closed = threading.Event()
            events = []

            class BlockingStream:
                """Second read blocks until released; records the call order."""

                def __init__(self):
                    self.reads = 0
                    self.reading = False

                def read(self, size=-1):
                    self.reads += 1
                    if self.reads != 2:
                        return b"chunk" if self.reads == 1 else b""
                    self.reading = True
                    loop.call_soon_threadsafe(read_blocked.set)
                    # Failsafe only: the test releases this read itself.
                    released = release.wait(timeout=10)
                    self.reading = False
                    events.append("read returned" if released else "read timed out")
                    return b"chunk"

                def close(self):
                    events.append("close during read" if self.reading else "close")
                    closed.set()
                    # A close that does not wait must not hang the test.
                    release.set()

            stream = BlockingStream()
            store.open_read = lambda *args: stream

            async def receive():
                await read_blocked.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                pass

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": f"/api/nodes/doc/attachments/{record.id}",
                "raw_path": b"",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            }
            if spec_version is not None:
                scope["asgi"]["spec_version"] = spec_version
            request = asyncio.create_task(app(scope, receive, send))
            await asyncio.wait_for(read_blocked.wait(), timeout=10)
            if spec_version is not None:
                # A 2.4+ app never polls receive, so a server drops the request
                # mid-read by cancelling its task, a native cancel that anyio's
                # wait for the worker thread does not cover.
                request.cancel()
            # While the read blocks, nothing but the loop runs, so these turns
            # deterministically let a close that does not wait for the read
            # happen first.
            for _ in range(50):
                await asyncio.sleep(0)
            release.set()
            try:
                await asyncio.wait_for(request, timeout=10)
            except (ClientDisconnect, asyncio.CancelledError):
                pass
            # After a native cancel the reading thread does the close, so it
            # may land after the request has ended.
            assert await asyncio.to_thread(closed.wait, 10)
            assert events == ["read returned", "close"]

        asyncio.run(run())


class TestCloseAfterRead:
    def test_a_read_after_close_returns_eof_without_touching_the_stream(self):
        from backend.service.rest_api import _CloseAfterRead

        class Stream:
            def __init__(self):
                self.reads = 0
                self.closes = 0

            def read(self, size=-1):
                self.reads += 1
                return b"data"

            def close(self):
                self.closes += 1

        stream = Stream()
        guarded = _CloseAfterRead(stream)
        guarded.close()

        # The body iterator may still ask for a chunk after the response's
        # finally closed the source; it must see EOF, not read a closed file.
        assert guarded.read(1024) == b""
        assert stream.reads == 0
        assert stream.closes == 1


class TestStoreCallsRunOffTheEventLoop:
    """Each store call must run on a worker thread, never on the loop's thread.

    The loop runs on this test's own thread (asyncio.run), so a call made on
    it would block every other request while the store reads files or waits
    for an OS lock.
    """

    def _tracking_store(self, store, threads):
        import threading

        for name in ("list", "get_record", "open_read", "delete"):
            real = getattr(store, name)

            def wrapper(*args, _real=real, _name=name):
                threads[_name] = threading.get_ident()
                return _real(*args)

            setattr(store, name, wrapper)
        return store

    def _run(self, storage, store, scenario):
        import httpx

        app = FastAPI()
        app.include_router(
            create_rest_router(
                GraphService(storage, attachment_settings=_settings(store))
            ),
            prefix="/api",
        )

        async def run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return await scenario(http)

        return asyncio.run(run())

    def test_list_download_and_delete_do_not_run_store_calls_on_the_loop(
        self, storage, store
    ):
        import threading

        record = _seed(store, "doc", b"payload")
        threads = {}
        self._tracking_store(store, threads)
        loop_thread = threading.get_ident()

        async def scenario(http):
            assert (await http.get("/api/nodes/doc/attachments")).status_code == 200
            url = f"/api/nodes/doc/attachments/{record.id}"
            assert (await http.get(url)).status_code == 200
            assert (await http.delete(url)).status_code == 200

        self._run(storage, store, scenario)
        assert set(threads) == {"list", "get_record", "open_read", "delete"}
        assert all(ident != loop_thread for ident in threads.values()), threads

    def test_upload_does_not_run_the_index_read_on_the_loop(self, storage, store):
        import threading

        threads = {}
        self._tracking_store(store, threads)
        loop_thread = threading.get_ident()

        async def scenario(http):
            response = await http.post(
                "/api/nodes/doc/attachments",
                params={"filename": "a.txt"},
                content=b"hello",
            )
            assert response.status_code == 201

        self._run(storage, store, scenario)
        assert threads["list"] != loop_thread, threads

    def test_upload_over_an_unreadable_index_is_refused_and_stages_nothing(
        self, storage, store
    ):
        from backend.core.attachment_store import AttachmentIndexCorrupt

        def corrupt(*args):
            raise AttachmentIndexCorrupt("unreadable")

        store.list = corrupt
        staged = []
        real_stage = store.stage

        async def tracking_stage(*args):
            staged.append(args)
            return await real_stage(*args)

        store.stage = tracking_stage
        client = _client(GraphService(storage, attachment_settings=_settings(store)))
        response = _upload(client, "doc")
        assert "attachment_index_corrupt" in response.text
        assert response.status_code != 201
        assert staged == []

    def test_type_sniffing_runs_off_the_loop(self, storage, store, monkeypatch):
        import threading

        from backend.service import attachments as attachment_ops

        seen = []
        real = attachment_ops.detect_attachment_type

        def recording(source, filename):
            seen.append(threading.get_ident())
            return real(source, filename)

        monkeypatch.setattr(attachment_ops, "detect_attachment_type", recording)
        loop_thread = threading.get_ident()

        async def scenario(http):
            response = await http.post(
                "/api/nodes/doc/attachments",
                params={"filename": "a.txt"},
                content=b"hello",
            )
            assert response.status_code == 201

        self._run(storage, store, scenario)
        assert len(seen) == 1 and seen[0] != loop_thread


@pytest.mark.parametrize("level", ["off", "read", "read_write"])
@pytest.mark.parametrize("deny", [False, True])
def test_mcp_attachment_registration_filters_map_and_mount(
    service, monkeypatch, level, deny
):
    """Registration and execution share the grant and hook for all five tools."""
    from unittest.mock import MagicMock, Mock
    from backend.runtime.attachment_access import (
        ATTACHMENT_TOOLS,
        ATTACHMENT_READ_TOOLS,
    )
    from backend.service.mcp_tools import register_mcp_tools

    functions = {}
    calls = []
    for name in ATTACHMENT_TOOLS:

        def tool():
            calls.append(True)
            return {"success": True}

        tool.__name__ = name
        functions[name] = tool
    monkeypatch.setattr(
        "backend.service.mcp_tools.make_system_create_tools", lambda *a, **k: functions
    )
    hook = Mock()
    hook.evaluate.return_value = GraphAuthorizationDecision(allowed=not deny)
    monkeypatch.setattr(service, "_authorization_hook", hook)
    registered = {}
    mcp = Mock()
    mcp.tool = MagicMock(return_value=lambda f: registered.setdefault(f.__name__, f))
    tools = register_mcp_tools(mcp, service, attachment_access=level)
    expected = (
        set()
        if level == "off"
        else (ATTACHMENT_READ_TOOLS if level == "read" else ATTACHMENT_TOOLS)
    )
    assert ATTACHMENT_TOOLS.intersection(tools) == expected
    assert ATTACHMENT_TOOLS.intersection(registered) == expected
    for name in expected:
        result = tools[name]()
        assert result.get("error_code") == ("access_denied" if deny else None)
    assert len(calls) == (0 if deny else len(expected))


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
        use_request_authorization(actor_id="reader"),
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
