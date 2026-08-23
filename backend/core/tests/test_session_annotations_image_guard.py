"""Tests for the image-ingest guard on every annotation write path.

docs/ANNOTATION_CONTRACT.md's "Image ingest enforcement" requires that an
`image` annotation's `content.image.url` is always the embedded result of
server-side ingest (``backend/core/image_ingest.py``) — never a remote URL
the annotation would then depend on staying reachable. Before this guard the
rule was enforced only by the dedicated ``create_image_annotation`` MCP tool,
while the generic ``create_annotation``/``update_annotation`` envelope and a
raw ``annotation_created`` op both stored a supplied URL verbatim.

The guard lives in ``session_annotations.image_annotation_error`` and is
applied by ``SessionStore._validate_annotation``, the one point every write
passes through. These tests cover the helper, the store op paths, and the
``apply_ops`` batch path a browser uses.
"""

import pytest

from backend.core.image_ingest import ALLOWED_CONTENT_TYPES
from backend.core.session_annotations import (
    EMBEDDED_IMAGE_URL_PREFIXES,
    image_annotation_error,
    is_embedded_image_url,
)
from backend.core.session_manager import SessionManager
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    OpError,
    SessionStore,
)

EMBEDDED_URL = "data:image/webp;base64,UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="


def _image_annotation(url=EMBEDDED_URL, annotation_id="img-1"):
    return {
        "id": annotation_id,
        "type": "image",
        "kind": "image",
        "position": {"x": 0, "y": 0},
        "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
        "image": {"url": url, "width": 10, "height": 10},
        "alt": "",
    }


class TestImageAnnotationError:
    def test_accepts_embedded_data_uri(self):
        assert image_annotation_error(_image_annotation()) is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/logo.png",
            "http://example.com/logo.png",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
            "",
            123,
        ],
    )
    def test_rejects_anything_not_embedded(self, url):
        assert image_annotation_error(_image_annotation(url=url)) is not None

    def test_rejects_non_object_image_payload(self):
        annotation = _image_annotation()
        annotation["image"] = "https://example.com/logo.png"
        assert image_annotation_error(annotation) is not None

    @pytest.mark.parametrize("payload", [{}, None, {"width": 10}])
    def test_payload_without_a_url_sets_no_pixel_content(self, payload):
        """A browser echoes an image annotation back on a move with `image`
        serialised as `{}` when it holds no picture; rejecting that would fail
        the whole op batch over an annotation that references nothing."""
        annotation = _image_annotation()
        annotation["image"] = payload
        assert image_annotation_error(annotation) is None

    def test_patch_without_image_payload_is_unaffected(self):
        """A move/resize/alt-text patch on an image carries no `image` key."""
        assert (
            image_annotation_error(
                {"id": "img-1", "type": "image", "position": {"x": 5, "y": 5}}
            )
            is None
        )

    def test_other_types_are_unaffected(self):
        assert (
            image_annotation_error(
                {"id": "l-1", "type": "label", "text": "https://example.com/x.png"}
            )
            is None
        )

    def test_prefixes_cover_exactly_the_ingestable_content_types(self):
        """The literal prefixes must not drift from image_ingest's allow-list."""
        assert {
            prefix[len("data:") : -len(";base64,")]
            for prefix in EMBEDDED_IMAGE_URL_PREFIXES
        } == set(ALLOWED_CONTENT_TYPES)

    def test_is_embedded_image_url(self):
        assert is_embedded_image_url(EMBEDDED_URL)
        assert not is_embedded_image_url("https://example.com/logo.png")


class TestStoreRejectsUnvalidatedImageWrites:
    def _store_session(self):
        store = SessionStore(InMemorySessionPersistenceBackend())
        return store, store.create()

    def test_annotation_created_with_remote_url_is_refused(self):
        """The regression test for the bypass: a generic create/op could
        persist an arbitrary remote image URL, skipping ingest entirely."""
        store, session = self._store_session()

        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                {
                    "op": "annotation_created",
                    "annotation": _image_annotation(url="https://example.com/x.png"),
                },
            )

        assert session.state["annotations"] == []
        assert session.seq == 0

    def test_annotation_created_with_embedded_url_is_stored(self):
        store, session = self._store_session()

        applied = store.apply_state_op(
            session, {"op": "annotation_created", "annotation": _image_annotation()}
        )

        assert applied["annotation"]["image"]["url"] == EMBEDDED_URL
        assert session.seq == 1

    def test_annotation_updated_cannot_swap_in_a_remote_url(self):
        store, session = self._store_session()
        store.apply_state_op(
            session, {"op": "annotation_created", "annotation": _image_annotation()}
        )

        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                {
                    "op": "annotation_updated",
                    "annotation": _image_annotation(url="https://example.com/x.png"),
                },
            )

        assert session.state["annotations"][0]["image"]["url"] == EMBEDDED_URL
        assert session.seq == 1

    def test_moving_an_image_annotation_still_works(self):
        """The guard must not block the envelope-only ops (move/resize/lock),
        which clients send with the full annotation echoed back."""
        store, session = self._store_session()
        store.apply_state_op(
            session, {"op": "annotation_created", "annotation": _image_annotation()}
        )

        moved = _image_annotation()
        moved["position"] = {"x": 40, "y": 60}
        applied = store.apply_state_op(
            session, {"op": "annotation_updated", "annotation": moved}
        )

        assert applied["annotation"]["position"] == {"x": 40, "y": 60}


class TestApplyOpsRejectsUnvalidatedImageWrites:
    @pytest.mark.asyncio
    async def test_op_batch_with_remote_image_url_is_refused(self):
        """The browser/REST path (`POST /sessions/{id}/ops`) shares the guard."""
        manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
        session = manager.create_session()

        with pytest.raises(OpError):
            await manager.apply_ops(
                session.id,
                "browser-1",
                session.seq,
                [
                    {
                        "op": "annotation_created",
                        "annotation": _image_annotation(
                            url="https://example.com/x.png"
                        ),
                    }
                ],
            )

        assert manager.get_session(session.id).state["annotations"] == []
