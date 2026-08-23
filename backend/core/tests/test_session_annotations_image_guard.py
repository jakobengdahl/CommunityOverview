"""Tests for the image-ingest guard on every annotation write path.

docs/ANNOTATION_CONTRACT.md's "Image ingest enforcement" requires that an
`image` annotation's `content.image.url` is always the embedded result of
server-side ingest (``backend/core/image_ingest.py``) — never a remote URL
the annotation would then depend on staying reachable. Before this guard the
rule was enforced only by the dedicated ``create_image_annotation`` MCP tool,
while the generic ``create_annotation``/``update_annotation`` envelope and a
raw ``annotation_created`` op both stored a supplied URL verbatim.

The guard lives in ``session_annotations.image_annotation_error`` and is
applied by ``SessionStore.apply_state_op``'s ``annotation_created``/
``annotation_updated`` branches — the two points every write of image pixel
content passes through. These tests cover the helper, the store op paths, the
``apply_ops`` batch path a browser uses, and annotations persisted before the
rule existed.
"""

import pytest

from backend.core.image_ingest import OPTIMIZED_CONTENT_TYPE
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

    def test_prefixes_pin_to_what_ingest_actually_emits(self):
        """Not the wider set of formats ingest *accepts*: every accepted source
        is re-encoded, so keeping a prefix no path ever produces would only
        widen what a forged data URI may claim to be."""
        assert EMBEDDED_IMAGE_URL_PREFIXES == (
            f"data:{OPTIMIZED_CONTENT_TYPE};base64,",
        )

    @pytest.mark.parametrize(
        "url",
        [
            "data:image/png;base64,iVBORw0KGgo=",
            "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
        ],
    )
    def test_accepted_input_formats_are_not_accepted_as_stored_urls(self, url):
        assert image_annotation_error(_image_annotation(url=url)) is not None

    def test_unchanged_legacy_url_is_exempt(self):
        """An annotation persisted before this rule must stay movable: the
        browser echoes the whole annotation, image payload included, on every
        update (`sessionSyncClient.js`)."""
        legacy = _image_annotation(url="https://example.com/legacy.png")
        assert image_annotation_error(legacy, existing=legacy) is None

    def test_a_different_remote_url_is_still_refused_on_legacy_data(self):
        legacy = _image_annotation(url="https://example.com/legacy.png")
        swapped = _image_annotation(url="https://example.com/other.png")
        assert image_annotation_error(swapped, existing=legacy) is not None

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


class TestLegacyRemoteUrlAnnotationsStayUsable:
    """Annotations persisted through the old `create_annotation(type="image",
    content={"image": {"url": ...}})` path really exist — it was documented
    behaviour. The guard must not strand them: every envelope-only op re-sends
    the stored URL, so a blanket refusal would make such an annotation
    permanently unmovable and its deletion un-undoable.

    Duplication is the deliberate exception — the copy gets a new id, so there
    is no stored URL to match and it is refused; that is pinned at the tool
    level in
    ``backend/service/tests/test_mcp_image_annotation_tool.py``'s
    ``TestLegacyRemoteUrlAnnotationsThroughTheTools``."""

    def _session_with_legacy_image(self):
        manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
        session = manager.create_session()
        # Seeded directly: the guard is exactly what stops this being created
        # through any op today.
        session.state["annotations"].append(
            _image_annotation(url="https://example.com/legacy.png")
        )
        manager.store.persist(session)
        return manager, session

    @pytest.mark.asyncio
    async def test_move_and_lock_still_apply(self):
        manager, session = self._session_with_legacy_image()
        moved = _image_annotation(url="https://example.com/legacy.png")
        moved["position"] = {"x": 90, "y": 12}
        moved["locked"] = True

        result = await manager.apply_ops(
            session.id,
            "browser-1",
            session.seq,
            [{"op": "annotation_updated", "annotation": moved}],
        )

        assert len(result["applied"]) == 1
        stored = manager.get_session(session.id).state["annotations"][0]
        assert stored["position"] == {"x": 90, "y": 12}
        assert stored["locked"] is True

    @pytest.mark.asyncio
    async def test_delete_then_undo_round_trips(self):
        manager, session = self._session_with_legacy_image()
        await manager.apply_ops(
            session.id,
            "browser-1",
            session.seq,
            [{"op": "annotation_deleted", "annotation_id": "img-1"}],
        )
        assert manager.get_session(session.id).state["annotations"] == []

        manager.undo_last_action(session.id, "browser-1")

        restored = manager.get_session(session.id).state["annotations"]
        assert [a["image"]["url"] for a in restored] == [
            "https://example.com/legacy.png"
        ]

    @pytest.mark.asyncio
    async def test_swapping_in_a_new_remote_url_is_still_refused(self):
        manager, session = self._session_with_legacy_image()
        swapped = _image_annotation(url="https://example.com/attacker.png")

        with pytest.raises(OpError):
            await manager.apply_ops(
                session.id,
                "browser-1",
                session.seq,
                [{"op": "annotation_updated", "annotation": swapped}],
            )

        stored = manager.get_session(session.id).state["annotations"][0]
        assert stored["image"]["url"] == "https://example.com/legacy.png"


class TestApplyOpsRejectsUnvalidatedImageWrites:
    @pytest.mark.asyncio
    async def test_undoing_a_deleted_image_restores_the_embedded_copy(self):
        """Undo replays the inverse `annotation_created` op with the stored
        annotation, so the guard must let a session's own history back in."""
        manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
        session = manager.create_session()
        manager.upsert_image_annotation(
            session.id, "client-a", _image_annotation(), optimized_image_bytes=100
        )
        await manager.apply_ops(
            session.id,
            "client-a",
            manager.get_session(session.id).seq,
            [{"op": "annotation_deleted", "annotation_id": "img-1"}],
        )

        manager.undo_last_action(session.id, "client-a")

        restored = manager.get_session(session.id).state["annotations"]
        assert [a["image"]["url"] for a in restored] == [EMBEDDED_URL]

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

    @pytest.mark.asyncio
    async def test_a_poisoned_op_rolls_back_the_whole_batch(self):
        """apply_ops is all-or-nothing: a valid op earlier in the same batch
        must not survive a later refusal."""
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
                        "annotation": {
                            "id": "lbl-1",
                            "type": "label",
                            "position": {"x": 0, "y": 0},
                            "text": "kept?",
                        },
                    },
                    {
                        "op": "annotation_created",
                        "annotation": _image_annotation(
                            url="https://example.com/x.png"
                        ),
                    },
                ],
            )

        reloaded = manager.get_session(session.id)
        assert reloaded.state["annotations"] == []
        assert reloaded.seq == 0
