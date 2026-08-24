"""Tests for the SavedView/VisualizationView metadata helpers.

A SavedView node's ``annotation_document``/``annotations`` are ordinary node
metadata, written through the generic ``add_nodes``/``update_node`` tools
rather than through ``SessionStore.apply_state_op`` — so they never went
through ``image_annotation_error`` on their own. These helpers close that gap:
``saved_view_annotation_error`` applies the identical rule at the point a
SavedView's metadata is persisted (``backend/service/mutations.py``), and
``sanitize_saved_view_metadata`` strips any non-embedded image URL that
reached storage some other way before it can reach an ``<img src>`` on read
(``backend/service/views.py``, ``backend/service/serializers.py``).
"""

from backend.core.session_annotations import (
    iter_saved_view_annotations,
    sanitize_saved_view_metadata,
    saved_view_annotation_error,
)

EMBEDDED_URL = "data:image/webp;base64,UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="
REMOTE_URL = "https://attacker.example/tracker.png"


def _image_annotation(url, annotation_id="img-1"):
    return {
        "id": annotation_id,
        "type": "image",
        "kind": "image",
        "position": {"x": 0, "y": 0},
        "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
        "image": {"url": url, "width": 10, "height": 10},
        "alt": "",
    }


class TestIterSavedViewAnnotations:
    def test_yields_from_annotation_document(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(EMBEDDED_URL)],
            }
        }
        assert list(iter_saved_view_annotations(metadata)) == [
            _image_annotation(EMBEDDED_URL)
        ]

    def test_yields_from_legacy_annotations(self):
        metadata = {"annotations": [_image_annotation(EMBEDDED_URL, "img-2")]}
        assert list(iter_saved_view_annotations(metadata)) == [
            _image_annotation(EMBEDDED_URL, "img-2")
        ]

    def test_yields_from_both_when_present(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(EMBEDDED_URL, "img-1")],
            },
            "annotations": [_image_annotation(EMBEDDED_URL, "img-2")],
        }
        ids = [a["id"] for a in iter_saved_view_annotations(metadata)]
        assert ids == ["img-1", "img-2"]

    def test_tolerates_missing_or_malformed_fields(self):
        assert list(iter_saved_view_annotations({})) == []
        assert list(iter_saved_view_annotations(None)) == []
        assert list(iter_saved_view_annotations({"annotation_document": "oops"})) == []
        assert list(iter_saved_view_annotations({"annotations": "oops"})) == []
        assert (
            list(
                iter_saved_view_annotations(
                    {"annotation_document": {"annotations": ["not-a-dict"]}}
                )
            )
            == []
        )


class TestSavedViewAnnotationError:
    def test_none_when_every_image_is_embedded(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(EMBEDDED_URL)],
            },
            "annotations": [_image_annotation(EMBEDDED_URL)],
        }
        assert saved_view_annotation_error(metadata) is None

    def test_none_for_non_image_annotations(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    {
                        "id": "note-1",
                        "type": "note",
                        "text": "hello",
                        "position": {"x": 1, "y": 2},
                    }
                ],
            }
        }
        assert saved_view_annotation_error(metadata) is None

    def test_rejects_remote_url_in_annotation_document(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(REMOTE_URL)],
            }
        }
        error = saved_view_annotation_error(metadata)
        assert error is not None
        assert "embedded" in error

    def test_rejects_remote_url_in_legacy_annotations(self):
        metadata = {"annotations": [_image_annotation(REMOTE_URL)]}
        assert saved_view_annotation_error(metadata) is not None

    def test_no_byte_identical_exemption_for_a_fresh_save(self):
        """Unlike a live SessionStore op, a saved-view write has no legitimate
        *existing* annotation to compare against — every image must already
        be embedded, even one that matches some other stored copy verbatim."""
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(REMOTE_URL)],
            }
        }
        assert saved_view_annotation_error(metadata) is not None

    def test_first_offending_annotation_short_circuits(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    _image_annotation(REMOTE_URL, "img-1"),
                    _image_annotation(EMBEDDED_URL, "img-2"),
                ],
            }
        }
        assert saved_view_annotation_error(metadata) is not None


class TestSanitizeSavedViewMetadata:
    def test_strips_remote_url_from_annotation_document(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(REMOTE_URL)],
            }
        }
        sanitized = sanitize_saved_view_metadata(metadata)
        stripped = sanitized["annotation_document"]["annotations"][0]
        assert "url" not in stripped["image"]
        # width/height and every other field survive the strip.
        assert stripped["image"]["width"] == 10

    def test_strips_remote_url_from_legacy_annotations(self):
        metadata = {"annotations": [_image_annotation(REMOTE_URL)]}
        sanitized = sanitize_saved_view_metadata(metadata)
        assert "url" not in sanitized["annotations"][0]["image"]

    def test_leaves_embedded_urls_untouched(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(EMBEDDED_URL)],
            },
            "annotations": [_image_annotation(EMBEDDED_URL)],
        }
        sanitized = sanitize_saved_view_metadata(metadata)
        assert sanitized == metadata

    def test_leaves_non_image_annotations_untouched(self):
        note = {
            "id": "note-1",
            "type": "note",
            "text": "hello",
            "position": {"x": 1, "y": 2},
        }
        metadata = {"annotation_document": {"schema_version": 1, "annotations": [note]}}
        sanitized = sanitize_saved_view_metadata(metadata)
        assert sanitized["annotation_document"]["annotations"][0] == note

    def test_never_mutates_the_input(self):
        original_image = {"url": REMOTE_URL, "width": 10}
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    {
                        "id": "img-1",
                        "type": "image",
                        "image": original_image,
                    }
                ],
            }
        }
        sanitize_saved_view_metadata(metadata)
        assert metadata["annotation_document"]["annotations"][0]["image"] is (
            original_image
        )
        assert original_image["url"] == REMOTE_URL

    def test_preserves_unrelated_metadata_fields(self):
        metadata = {
            "node_ids": ["a", "b"],
            "positions": {"a": {"x": 0, "y": 0}},
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_image_annotation(REMOTE_URL)],
            },
        }
        sanitized = sanitize_saved_view_metadata(metadata)
        assert sanitized["node_ids"] == ["a", "b"]
        assert sanitized["positions"] == {"a": {"x": 0, "y": 0}}

    def test_tolerates_non_dict_metadata(self):
        assert sanitize_saved_view_metadata(None) is None
        assert sanitize_saved_view_metadata("oops") == "oops"

    def test_missing_url_key_is_untouched(self):
        """`image` serialised as `{}` (a move/resize echo with no pixel
        content) must not be treated as something to strip."""
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [{"id": "img-1", "type": "image", "image": {}}],
            }
        }
        sanitized = sanitize_saved_view_metadata(metadata)
        assert sanitized == metadata
