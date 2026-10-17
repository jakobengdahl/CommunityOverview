"""
Node attachment routes and service functions (ADR 0008 sections 4-8).

Each per-route row of ADR 0008's section 7 table, each limit of section 4 and
each refusal of section 5 has a test here.
"""

import asyncio
import json
import os

import pytest
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
