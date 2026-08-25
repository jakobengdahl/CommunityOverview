"""Tests for the MCP `create_image_annotation` tool registered in
``backend/service/mcp_tools.py``.

This tool is a thin wrapper over ``backend.core.image_ingest`` (decode/fetch/
optimize — covered in ``backend/core/tests/test_image_ingest.py``) and
``SessionManager.upsert_image_annotation`` (the image-specific session/
document byte budgets — covered in
``backend/core/tests/test_session_manager.py``); these tests cover the tool's
own glue: routing image_ingest errors to MCP error codes, the
image_data/image_url mutual-exclusivity check, and the same id-collision/
authorization contract the other generic annotation tools share (see
``test_mcp_generic_annotation_tools.py``).
"""

import base64
import io
import os
from unittest.mock import MagicMock, Mock

import httpx2 as httpx
import pytest
from PIL import Image

from backend.core import GraphStorage, image_ingest
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, mcp_tools, register_mcp_tools


def _png_bytes(size=(4, 4), color=(255, 0, 0)):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_data_url(**kwargs):
    encoded = base64.b64encode(_png_bytes(**kwargs)).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _bmp_data_url():
    image = Image.new("RGB", (4, 4), (1, 2, 3))
    buffer = io.BytesIO()
    image.save(buffer, format="BMP")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/bmp;base64,{encoded}"


def _seed_large_image_annotation(manager, session, *, annotation_id, data_bytes):
    """Persist an `image` annotation with an embedded data URI of exactly
    ``data_bytes`` decoded bytes, via ``upsert_image_annotation`` directly —
    bypassing ``optimize_image``'s real re-encode, whose WebP compression
    would make a solid-color test fixture like ``_png_data_url`` collapse to
    a few bytes regardless of pixel count. Same trick as
    ``test_session_manager.py``'s ``_image_annotation`` helper, sized to
    exceed the flat op-batch cap while staying under the per-image budget —
    the exact realistic-image scenario the flat cap wrongly rejected."""
    encoded = base64.b64encode(b"x" * data_bytes).decode("ascii")
    annotation = {
        "id": annotation_id,
        "type": "image",
        "position": {"x": 0, "y": 0},
        "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
        "image": {
            "url": f"data:{image_ingest.OPTIMIZED_CONTENT_TYPE};base64,{encoded}"
        },
        "alt": "",
    }
    return manager.upsert_image_annotation(
        session.id, "mcp-agent", annotation, optimized_image_bytes=data_bytes
    )


def _install_transport(monkeypatch, handler):
    """Same pattern as test_image_ingest.py's _StubHttpxModule: the tool calls
    ``image_ingest.fetch_image_bytes``, which builds its own ``httpx.Client()``
    off the module-level ``httpx`` name, so that name is what must be patched."""

    class _StubHttpxModule:
        HTTPError = httpx.HTTPError

        def __init__(self, transport):
            self._transport = transport

        def Client(self, **kwargs):
            return httpx.Client(transport=self._transport, **kwargs)

    monkeypatch.setattr(
        image_ingest, "httpx", _StubHttpxModule(httpx.MockTransport(handler))
    )


@pytest.fixture
def image_tools(tmp_path):
    """tools_map wired to an in-memory shared-session manager, plus that manager."""
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


class TestCreateImageAnnotationFromData:
    def test_creates_from_image_data(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=1, y=2, image_data=_png_data_url()
        )

        assert result["success"] is True
        annotation = result["annotation"]
        assert annotation["type"] == "image"
        assert annotation["x"] == 1 and annotation["y"] == 2
        assert isinstance(annotation["id"], str) and annotation["id"]
        assert annotation["content"]["image"]["url"].startswith(
            "data:image/webp;base64,"
        )

    def test_accepts_bare_base64(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()
        encoded = base64.b64encode(_png_bytes()).decode("ascii")

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=encoded
        )

        assert result["success"] is True

    def test_defaults_w_h_to_image_dimensions(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=_png_data_url(size=(10, 20)),
        )

        annotation = result["annotation"]
        assert annotation["w"] == 10 and annotation["h"] == 20

    def test_explicit_w_h_override_image_dimensions(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=_png_data_url(size=(10, 20)),
            w=100,
            h=50,
        )

        annotation = result["annotation"]
        assert annotation["w"] == 100 and annotation["h"] == 50

    def test_invalid_base64_is_invalid_image(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data="not valid base64 !!!"
        )

        assert result["success"] is False
        assert result["error"] == "invalid_image"
        assert session.state["annotations"] == []

    def test_unsupported_format_is_rejected(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_bmp_data_url()
        )

        assert result["success"] is False
        assert result["error"] == "unsupported_type"
        assert session.state["annotations"] == []

    def test_optimized_too_large_is_rejected(self, image_tools, monkeypatch):
        """The per-image cap (image_ingest.optimize_image's own threshold,
        already covered by TestOptimizeImageSizeCap in test_image_ingest.py)
        is exercised here only for the tool's error-code mapping: patch
        optimize_image itself so the test doesn't depend on constructing an
        image that's actually incompressible below the real byte cap."""
        tools_map, manager = image_tools
        session = manager.create_session()

        def _raise_too_large(_raw, **_kwargs):
            raise image_ingest.OptimizedImageTooLarge("optimized image too large")

        monkeypatch.setattr(mcp_tools, "optimize_image", _raise_too_large)

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )

        assert result["success"] is False
        assert result["error"] == "too_large"
        assert session.state["annotations"] == []


class TestCreateImageAnnotationFromUrl:
    @pytest.fixture(autouse=True)
    def _public_dns(self, monkeypatch):
        """``example.invalid`` used below must resolve to a public IP for
        ``fetch_image_bytes``'s SSRF check (see backend/core/image_ingest.py);
        SSRF-blocking behaviour itself is covered by
        TestFetchImageBytesSSRF in test_image_ingest.py.
        """
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 0))],
        )

    def test_fetches_and_creates(self, image_tools, monkeypatch):
        tools_map, manager = image_tools
        session = manager.create_session()
        raw = _png_bytes()

        def handler(request):
            return httpx.Response(200, content=raw)

        _install_transport(monkeypatch, handler)

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_url="https://example.invalid/pic.png",
        )

        assert result["success"] is True
        assert result["annotation"]["type"] == "image"

    def test_fetch_failure_is_reported(self, image_tools, monkeypatch):
        tools_map, manager = image_tools
        session = manager.create_session()

        def handler(request):
            return httpx.Response(404, content=b"not found")

        _install_transport(monkeypatch, handler)

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_url="https://example.invalid/missing.png",
        )

        assert result["success"] is False
        assert result["error"] == "fetch_failed"
        assert session.state["annotations"] == []

    def test_source_too_large_is_rejected(self, image_tools, monkeypatch):
        """Per-source-fetch cap; the real threshold is covered by
        TestFetchImageBytes in test_image_ingest.py — here only the tool's
        error-code mapping for image_url sources is under test."""
        tools_map, manager = image_tools
        session = manager.create_session()

        def _raise_too_large(_url, **_kwargs):
            raise image_ingest.SourceImageTooLarge("downloaded image too large")

        monkeypatch.setattr(mcp_tools, "fetch_image_bytes", _raise_too_large)

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_url="https://example.invalid/big.png",
        )

        assert result["success"] is False
        assert result["error"] == "too_large"
        assert session.state["annotations"] == []


class TestCreateImageAnnotationSourceValidation:
    def test_neither_source_given_is_invalid_source(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_image_annotation"](session_id=session.id, x=0, y=0)

        assert result["success"] is False
        assert result["error"] == "invalid_source"
        assert session.state["annotations"] == []

    def test_both_sources_given_is_invalid_source(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=_png_data_url(),
            image_url="https://example.invalid/pic.png",
        )

        assert result["success"] is False
        assert result["error"] == "invalid_source"
        assert session.state["annotations"] == []

    def test_invalid_session_id(self, image_tools):
        tools_map, _ = image_tools
        result = tools_map["create_image_annotation"](
            session_id="nope", x=0, y=0, image_data=_png_data_url()
        )
        assert result["success"] is False

    def test_missing_manager_is_reported(self):
        storage = GraphStorage(json_path="/tmp/does-not-matter-image-ann.json")
        service = GraphService(storage)
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        tools_map = register_mcp_tools(mock_mcp, service)  # no session_manager
        result = tools_map["create_image_annotation"](
            session_id="1111-2222", x=0, y=0, image_data=_png_data_url()
        )
        assert result["success"] is False


class TestCreateImageAnnotationIdCollisions:
    def test_upserts_by_matching_id(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()
        tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=_png_data_url(),
            annotation_id="img-1",
        )

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=5,
            y=5,
            image_data=_png_data_url(),
            annotation_id="img-1",
        )

        assert result["success"] is True
        assert len(session.state["annotations"]) == 1
        assert result["annotation"]["x"] == 5

    def test_note_annotation_id_is_rejected_not_overwritten(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=_png_data_url(),
            annotation_id="note-1",
        )

        assert result["success"] is False
        assert result["error"] == "wrong_type"
        surviving = session.state["annotations"][0]
        assert surviving["type"] == "note"

    def test_cross_generic_type_collision_is_rejected(self, image_tools):
        """create_image_annotation must never silently convert a shape into
        an image."""
        tools_map, manager = image_tools
        session = manager.create_session()
        manager.upsert_annotation(
            session.id, "mcp-agent", {"id": "ann-1", "type": "shape"}
        )

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=_png_data_url(),
            annotation_id="ann-1",
        )

        assert result["success"] is False
        assert result["error"] == "wrong_type"
        surviving = session.state["annotations"][0]
        assert surviving["type"] == "shape"


class TestCreateImageAnnotationConcurrencyAndBudgets:
    def test_revision_conflict_is_reported(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()
        tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=1,
            y=1,
            image_data=_png_data_url(),
            expected_revision=0,
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        assert result["current_revision"] == session.seq

    def test_busy_when_lock_held(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        class _HeldLock:
            def locked(self):
                return True

        manager._lock = lambda _sid: _HeldLock()
        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )
        assert result["success"] is False
        assert result["error"] == "busy"

    def test_session_image_budget_is_reported_as_too_large(
        self, image_tools, monkeypatch
    ):
        """The session/document byte budgets themselves (and that a same-id
        replace excludes its own old copy) are covered by
        TestUpsertImageAnnotation in test_session_manager.py; here only the
        tool's routing of ImageBudgetExceeded to error "too_large" is under
        test, via a manager whose budget is patched down to zero."""
        tools_map, manager = image_tools
        session = manager.create_session()
        original = manager.upsert_image_annotation

        def _zero_budget(*args, **kwargs):
            kwargs["max_session_image_bytes"] = 0
            return original(*args, **kwargs)

        monkeypatch.setattr(manager, "upsert_image_annotation", _zero_budget)

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )

        assert result["success"] is False
        assert result["error"] == "too_large"
        assert session.state["annotations"] == []

    def test_id_collision_race_after_precheck_is_a_clean_error(
        self, image_tools, monkeypatch
    ):
        """Regression test for smallfix-mcp-image-annotation-opError-race:
        the id/type pre-check above only guards against a collision that
        already existed when the tool started — image_url fetch + optimize
        can take seconds, a real window for a concurrent write to create the
        same id as a different type before this tool's own write lands.
        SessionStore.apply_state_op then raises OpError for the type-change
        attempt (backend/core/session_store.py's "cannot change type via
        upsert" guard) rather than the tool silently retyping the annotation.

        Simulate that window here: the pre-check passes because 'race-1'
        does not exist yet, then a same-id 'shape' annotation is written
        (as if by another collaborator) while optimize_image is "in flight",
        so upsert_image_annotation's own write is the one that collides.
        The generic `except OpError` this tool already carries (in place
        since the tool's original commit, 9a58f17) must turn that into a
        clean error result instead of an unhandled exception, and the
        colliding shape annotation must be left untouched."""
        tools_map, manager = image_tools
        session = manager.create_session()
        real_optimize = mcp_tools.optimize_image

        def _racing_optimize(raw, **kwargs):
            manager.upsert_annotation(
                session.id, "other-collaborator", {"id": "race-1", "type": "shape"}
            )
            return real_optimize(raw, **kwargs)

        monkeypatch.setattr(mcp_tools, "optimize_image", _racing_optimize)

        result = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=_png_data_url(),
            annotation_id="race-1",
        )

        assert result["success"] is False
        assert "cannot change type via upsert" in result["error"]
        assert len(session.state["annotations"]) == 1
        assert session.state["annotations"][0]["type"] == "shape"


class TestCreateImageAnnotationAuthorization:
    """Same authorization seam as the other generic annotation tools (see
    TestGenericAnnotationToolsAuthorization in
    test_mcp_generic_annotation_tools.py)."""

    def test_deny_all_blocks_create(self, image_tools, monkeypatch):
        tools_map, manager = image_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )

        assert result.get("error_code") == "access_denied"
        assert session.state["annotations"] == []

    def test_read_only_blocks_create_and_creates_nothing(
        self, image_tools, monkeypatch
    ):
        tools_map, manager = image_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert session.state["annotations"] == []


class TestImageIngestIsTheOnlyWayIn:
    """docs/ANNOTATION_CONTRACT.md "Image ingest enforcement": no MCP tool may
    persist an image annotation whose pixel content skipped this tool's
    validated ingest. The generic envelope used to accept `type="image"` with
    an arbitrary `content.image.url` — the bypass these tests cover."""

    def test_generic_create_refuses_image_type(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="image",
            x=0,
            y=0,
            content={"image": {"url": "https://example.com/logo.png"}},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_type"
        assert "create_image_annotation" in result["message"]
        assert session.state["annotations"] == []

    def test_generic_create_refuses_bare_image_envelope(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="image", x=0, y=0
        )

        assert result["success"] is False
        assert result["error"] == "invalid_type"
        assert session.state["annotations"] == []

    def test_generic_update_cannot_replace_the_embedded_image(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()
        created = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )
        embedded_url = created["annotation"]["content"]["image"]["url"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"image": {"url": "https://example.com/logo.png"}},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert session.state["annotations"][0]["image"]["url"] == embedded_url

    def test_generic_update_still_edits_other_fields_of_an_image(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()
        created = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )
        embedded_url = created["annotation"]["content"]["image"]["url"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            x=25,
            y=35,
            content={"alt": "a red square"},
        )

        assert result["success"] is True
        assert result["annotation"]["x"] == 25
        assert result["annotation"]["content"]["alt"] == "a red square"
        assert result["annotation"]["content"]["image"]["url"] == embedded_url

    def test_duplicating_an_image_keeps_the_embedded_copy(self, image_tools):
        tools_map, manager = image_tools
        session = manager.create_session()
        created = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, image_data=_png_data_url()
        )
        embedded_url = created["annotation"]["content"]["image"]["url"]

        result = tools_map["duplicate_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            dx=20,
            dy=0,
        )

        assert result["success"] is True
        assert result["annotation"]["content"]["image"]["url"] == embedded_url

    def test_duplicating_a_realistic_sized_image_succeeds(self, image_tools):
        """Regression test for smallfix-duplicate-image-annotation-op-cap:
        ``duplicate_annotation`` routes the copy through ``upsert_annotation``,
        which used to apply the small generic op-batch cap (256KB) to the
        copy even though the original — the same bytes — was created through
        ``create_image_annotation``'s much larger image budget. A realistic
        picture (well over 256KB, still under the 2MB per-image budget) used
        to fail here with `too_large` although creating it succeeded."""
        tools_map, manager = image_tools
        session = manager.create_session()
        _seed_large_image_annotation(
            manager, session, annotation_id="img-1", data_bytes=400_000
        )

        result = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id="img-1", dx=20, dy=0
        )

        assert result["success"] is True
        assert result["annotation"]["content"]["image"]["url"].startswith(
            f"data:{image_ingest.OPTIMIZED_CONTENT_TYPE};base64,"
        )
        assert len(session.state["annotations"]) == 2


class TestLegacyRemoteUrlAnnotationsThroughTheTools:
    """The store-level half of this is in
    ``backend/core/tests/test_session_annotations_image_guard.py``'s
    ``TestLegacyRemoteUrlAnnotationsStayUsable``; these pin what the generic
    *tools* do with an annotation whose stored URL predates the ingest rule.

    Duplication is the one envelope-ish operation that is refused: the copy
    lands on a new id, so there is no stored URL to match and it counts as
    introducing a new reference to unvalidated content. It fails gracefully
    (``success: False`` with the ingest error) rather than raising, and the
    operations that keep the annotation on its own id still work.
    """

    def _session_with_legacy_image(self, manager):
        session = manager.create_session()
        # Seeded directly: the guard is exactly what stops this being created
        # through any tool or op today.
        session.state["annotations"].append(
            {
                "id": "img-legacy",
                "type": "image",
                "kind": "image",
                "position": {"x": 0, "y": 0},
                "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
                "image": {"url": "https://example.com/legacy.png"},
                "alt": "",
            }
        )
        manager.store.persist(session)
        return session

    def test_duplicate_is_refused(self, image_tools):
        tools_map, manager = image_tools
        session = self._session_with_legacy_image(manager)

        result = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id="img-legacy", dx=20, dy=0
        )

        assert result["success"] is False
        # duplicate_annotation surfaces the store's OpError text verbatim.
        assert "embedded image produced by server-side ingest" in result["error"]
        assert [a["id"] for a in session.state["annotations"]] == ["img-legacy"]

    def test_reorder_and_lock_still_succeed(self, image_tools):
        tools_map, manager = image_tools
        session = self._session_with_legacy_image(manager)

        reordered = tools_map["reorder_annotation"](
            session_id=session.id, annotation_id="img-legacy", z=7
        )
        locked = tools_map["set_annotation_lock"](
            session_id=session.id, annotation_id="img-legacy", locked=True
        )

        assert reordered["success"] is True
        assert locked["success"] is True
        stored = session.state["annotations"][0]
        assert stored["z"] == 7
        assert stored["locked"] is True
