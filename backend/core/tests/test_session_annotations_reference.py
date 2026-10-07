"""The `reference` annotation's payload rules, and the one that matters most:
an unsafe-scheme URL target cannot be persisted by any write path.

A reference's `target` is rendered into something a viewer activates, so the
scheme gate is not a cosmetic validation the way an out-of-set `shape` name is
(``docs/ANNOTATION_CONTRACT.md``'s "Reference tiles"). These tests pin it at
every layer that can write one:

* the pure payload validator (``reference_content_error`` /
  ``reference_url_error``),
* the object-model builders the MCP tools go through
  (``build_annotation``/``build_annotation_patch``),
* a raw session op (``SessionStore.apply_state_op``), which is the path a
  browser's own op batch takes and therefore the one that bypasses every MCP
  tool,
* SavedView node metadata (``saved_view_annotation_error`` /
  ``sanitize_saved_view_metadata``).

``TestRejectionCannotBeTurnedIntoAcceptance`` is the falsifiability half: it
asserts the *absence* of an accepting path, so a mutation that loosens the gate
in one layer still fails here rather than quietly passing because the other
layers happened to cover for it.
"""

import pytest

from backend.core.session_annotations import (
    DEFAULT_REFERENCE_SIZE,
    GENERIC_ANNOTATION_TYPES,
    REFERENCE_PREVIEW_FIELDS,
    REFERENCE_SAFE_URL_SCHEMES,
    REFERENCE_TARGET_KINDS,
    REFERENCE_TYPE,
    build_annotation,
    build_annotation_patch,
    project_annotation,
    reference_annotation_error,
    reference_content_error,
    reference_url_error,
    sanitize_saved_view_metadata,
    saved_view_annotation_error,
)
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    OpError,
    SessionStore,
)

# The four schemes the task names explicitly, plus the families a reader would
# reasonably expect to be covered by the same rule.
UNSAFE_TARGETS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "  javascript:alert(1)",
    "jAvAsCrIpT:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "DATA:text/html;base64,PHNjcmlwdD4=",
    "file:///etc/passwd",
    "FILE:///etc/passwd",
    "vbscript:msgbox(1)",
    "VBScript:MsgBox(1)",
    # No scheme at all: resolved by a browser against the app's own origin,
    # so an innocuous-looking tile would point back into the app.
    "//evil.example/x",
    "/admin/delete-everything",
    "evil.example/x",
    "",
    "   ",
    # A control character inside the scheme — a browser strips it before
    # resolving, so a gate that does not see it refuses nothing.
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "java\rscript:alert(1)",
    "java\x00script:alert(1)",
    "\x01javascript:alert(1)",
    # Allowlisted-looking but hostless.
    "http://",
    "https://",
    "http:///path",
]

SAFE_TARGETS = [
    "https://example.org/handbook",
    "http://example.org",
    "https://example.org:8443/a/b?c=d#e",
    "HTTPS://example.org/shouty-scheme",
    "  https://example.org/padded  ",
]


def _reference(**fields):
    return {"type": REFERENCE_TYPE, "kind": REFERENCE_TYPE, "id": "r1", **fields}


class TestReferenceIsAGenericAnnotationType:
    def test_reference_is_in_the_generic_type_set(self):
        assert REFERENCE_TYPE in GENERIC_ANNOTATION_TYPES

    def test_the_three_target_kinds_are_exactly_session_url_resource(self):
        assert set(REFERENCE_TARGET_KINDS) == {"session", "url", "resource"}

    def test_only_http_and_https_are_safe_schemes(self):
        assert set(REFERENCE_SAFE_URL_SCHEMES) == {"http", "https"}


class TestUrlGate:
    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_an_unsafe_target_is_refused(self, target):
        assert reference_url_error(target) is not None

    @pytest.mark.parametrize("target", SAFE_TARGETS)
    def test_a_safe_target_is_accepted(self, target):
        assert reference_url_error(target) is None

    def test_a_non_string_target_is_refused(self):
        for value in (None, 7, [], {}, True):
            assert reference_url_error(value) is not None

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_the_gate_applies_through_the_content_validator(self, target):
        error = reference_content_error({"target_kind": "url", "target": target})
        assert error is not None

    def test_a_session_target_is_not_held_to_the_url_rule(self):
        # The gate is per target kind: a session id is not a URL and must not
        # be refused for failing to be one.
        assert (
            reference_content_error(
                {"target_kind": "session", "target": "8244-1742-3391-0057"}
            )
            is None
        )

    def test_a_resource_target_is_not_held_to_the_url_rule(self):
        assert (
            reference_content_error(
                {"target_kind": "resource", "target": "resource-method-guide"}
            )
            is None
        )

    def test_a_session_target_may_not_carry_control_characters_either(self):
        # Not the scheme rule — the "validated string is the stored string"
        # rule, which every target kind is held to.
        assert (
            reference_content_error({"target_kind": "session", "target": "8244\n-1742"})
            is not None
        )


class TestPayloadRules:
    def test_target_kind_must_be_one_of_the_three(self):
        for bad in ("node", "graph", "", None, 7, "URL", "Session"):
            assert (
                reference_content_error({"target_kind": bad, "target": "x"}) is not None
            ), bad

    def test_target_must_be_a_non_empty_string(self):
        for bad in ("", "   ", None, 7, [], {}):
            assert (
                reference_content_error({"target_kind": "resource", "target": bad})
                is not None
            ), bad

    def test_a_complete_payload_requires_both_kind_and_target(self):
        assert (
            reference_content_error(
                {"target": "https://example.org"}, require_complete=True
            )
            is not None
        )
        assert (
            reference_content_error({"target_kind": "url"}, require_complete=True)
            is not None
        )
        assert (
            reference_content_error(
                {"target_kind": "url", "target": "https://example.org"},
                require_complete=True,
            )
            is None
        )

    def test_a_partial_patch_is_judged_only_on_what_it_sets(self):
        # Renaming a reference touches neither kind nor target, so it is not
        # held to the complete-payload rule.
        assert reference_content_error({"label": "Handbook"}) is None

    def test_label_and_icon_must_be_strings_within_their_caps(self):
        assert reference_content_error({"label": 7}) is not None
        assert reference_content_error({"icon": []}) is not None
        assert reference_content_error({"label": "a" * 201}) is not None
        assert reference_content_error({"icon": "a" * 65}) is not None
        assert reference_content_error({"label": "a" * 200}) is None

    def test_target_is_capped(self):
        long_url = "https://example.org/" + "a" * 2100
        assert (
            reference_content_error({"target_kind": "url", "target": long_url})
            is not None
        )

    def test_preview_is_an_object_limited_to_its_documented_text_fields(self):
        assert reference_content_error({"preview": "not an object"}) is not None
        assert reference_content_error({"preview": {"title": "T"}}) is None
        assert (
            reference_content_error(
                {"preview": {"title": "T", "description": "D", "site": "S"}}
            )
            is None
        )
        assert set(REFERENCE_PREVIEW_FIELDS) == {"title", "description", "site"}

    def test_a_preview_may_not_smuggle_a_remote_reference(self):
        # The reason the preview field set is closed rather than free-form: a
        # key holding a remote URL would be fetched by every viewer's browser
        # on open, going around image ingest entirely.
        for key in ("image", "image_url", "url", "thumbnail", "icon_url", "href"):
            assert (
                reference_content_error({"preview": {key: "https://evil.example/x"}})
                is not None
            ), key

    def test_preview_values_must_be_strings_within_their_cap(self):
        assert reference_content_error({"preview": {"title": 7}}) is not None
        assert reference_content_error({"preview": {"title": "a" * 501}}) is not None
        assert reference_content_error({"preview": {"title": "a" * 500}}) is None


class TestPatchResolvesTheStoredTargetKind:
    """A patch that sets only `target` carries no kind, so the gate has to read
    the stored one or it would check nothing at all."""

    def test_repointing_a_stored_url_reference_is_held_to_the_scheme_rule(self):
        stored = _reference(target_kind="url", target="https://example.org")
        error = reference_annotation_error({"target": "javascript:alert(1)"}, stored)
        assert error is not None

    def test_repointing_a_stored_session_reference_is_not(self):
        stored = _reference(target_kind="session", target="8244-1742")
        assert reference_annotation_error({"target": "1111-2222"}, stored) is None

    def test_switching_kind_and_target_together_is_judged_on_the_new_kind(self):
        stored = _reference(target_kind="session", target="8244-1742")
        assert (
            reference_annotation_error(
                {"target_kind": "url", "target": "javascript:alert(1)"}, stored
            )
            is not None
        )
        assert (
            reference_annotation_error(
                {"target_kind": "url", "target": "https://example.org"}, stored
            )
            is None
        )

    def test_a_stored_value_is_never_an_exemption(self):
        # Unlike the image guard, re-sending a value that is already stored
        # does NOT make it acceptable — nothing unsafe can legitimately be
        # stored, so an exemption could only ever admit something that got in
        # by a path the gate does not cover.
        stored = _reference(target_kind="url", target="javascript:alert(1)")
        assert (
            reference_annotation_error(
                {"target_kind": "url", "target": "javascript:alert(1)"}, stored
            )
            is not None
        )


class TestBuilders:
    def test_a_safe_reference_builds_and_round_trips(self):
        annotation = build_annotation(
            type=REFERENCE_TYPE,
            x=10,
            y=20,
            content={
                "target_kind": "url",
                "target": "https://example.org/handbook",
                "label": "Handbook",
                "preview": {"title": "Handbook", "site": "example.org"},
            },
        )
        projected = project_annotation(annotation)
        assert projected["type"] == REFERENCE_TYPE
        assert projected["content"]["target"] == "https://example.org/handbook"
        assert projected["content"]["target_kind"] == "url"
        assert projected["content"]["label"] == "Handbook"
        assert projected["content"]["preview"] == {
            "title": "Handbook",
            "site": "example.org",
        }

    def test_a_reference_gets_a_default_box_so_it_draws_something(self):
        annotation = build_annotation(
            type=REFERENCE_TYPE,
            x=0,
            y=0,
            content={"target_kind": "resource", "target": "r1"},
        )
        assert annotation["geometry"]["w"] == DEFAULT_REFERENCE_SIZE["w"]
        assert annotation["geometry"]["h"] == DEFAULT_REFERENCE_SIZE["h"]

    def test_each_side_defaults_independently(self):
        annotation = build_annotation(
            type=REFERENCE_TYPE,
            x=0,
            y=0,
            w=400,
            content={"target_kind": "resource", "target": "r1"},
        )
        assert annotation["geometry"]["w"] == 400
        assert annotation["geometry"]["h"] == DEFAULT_REFERENCE_SIZE["h"]

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_build_annotation_refuses_an_unsafe_target(self, target):
        with pytest.raises(ValueError):
            build_annotation(
                type=REFERENCE_TYPE,
                x=0,
                y=0,
                content={"target_kind": "url", "target": target},
            )

    def test_build_annotation_refuses_a_create_with_no_target_kind(self):
        # Without the complete-payload rule this would skip the scheme gate
        # entirely: the gate reads the target kind, and there would be none.
        with pytest.raises(ValueError):
            build_annotation(
                type=REFERENCE_TYPE,
                x=0,
                y=0,
                content={"target": "javascript:alert(1)"},
            )

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_build_annotation_patch_refuses_an_unsafe_repoint(self, target):
        stored = build_annotation(
            type=REFERENCE_TYPE,
            x=0,
            y=0,
            content={"target_kind": "url", "target": "https://example.org"},
        )
        stored["id"] = "r1"
        with pytest.raises(ValueError):
            build_annotation_patch(stored, content={"target": target})

    def test_moving_a_reference_preserves_its_payload_and_size(self):
        stored = build_annotation(
            type=REFERENCE_TYPE,
            x=0,
            y=0,
            w=240,
            h=80,
            content={
                "target_kind": "session",
                "target": "8244-1742",
                "label": "Overview",
                "icon": "flag",
                "preview": {"title": "Overview"},
            },
        )
        stored["id"] = "r1"
        patch = build_annotation_patch(stored, x=500, y=600)
        merged = {**stored, **patch}
        assert merged["target_kind"] == "session"
        assert merged["target"] == "8244-1742"
        assert merged["label"] == "Overview"
        assert merged["icon"] == "flag"
        assert merged["preview"] == {"title": "Overview"}
        assert merged["geometry"]["w"] == 240
        assert merged["geometry"]["h"] == 80
        assert merged["position"] == {"x": 500, "y": 600}


@pytest.fixture
def store():
    return SessionStore(InMemorySessionPersistenceBackend())


def _create_op(annotation):
    return {"op": "annotation_created", "annotation": annotation}


def _safe_reference_op():
    return _create_op(
        _reference(
            target_kind="url",
            target="https://example.org",
            position={"x": 0, "y": 0},
        )
    )


class TestRawSessionOpPath:
    """The path a browser's own op batch takes — every MCP tool is bypassed
    here, which is exactly why the gate cannot live only in the tools."""

    def test_a_safe_reference_is_stored(self, store):
        session = store.create()
        assert store.apply_state_op(session, _safe_reference_op()) is not None
        assert [a["target"] for a in session.state["annotations"]] == [
            "https://example.org"
        ]

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_an_unsafe_create_op_is_refused(self, store, target):
        session = store.create()
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                _create_op(
                    _reference(
                        target_kind="url", target=target, position={"x": 0, "y": 0}
                    )
                ),
            )
        assert session.state["annotations"] == []

    def test_a_create_op_with_no_target_kind_is_refused(self, store):
        session = store.create()
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                _create_op(
                    _reference(target="javascript:alert(1)", position={"x": 0, "y": 0})
                ),
            )
        assert session.state["annotations"] == []

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_an_unsafe_update_op_is_refused(self, store, target):
        session = store.create()
        store.apply_state_op(session, _safe_reference_op())
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "r1",
                        "type": REFERENCE_TYPE,
                        "target": target,
                    },
                },
            )
        assert session.state["annotations"][0]["target"] == "https://example.org"

    def test_moving_a_reference_through_an_op_is_allowed(self, store):
        session = store.create()
        store.apply_state_op(session, _safe_reference_op())
        store.apply_state_op(
            session,
            {
                "op": "annotation_updated",
                "annotation": {
                    "id": "r1",
                    "type": REFERENCE_TYPE,
                    "position": {"x": 400, "y": 500},
                },
            },
        )
        stored = session.state["annotations"][0]
        assert stored["position"] == {"x": 400, "y": 500}
        assert stored["target"] == "https://example.org"

    def test_renaming_a_reference_through_an_op_is_allowed(self, store):
        session = store.create()
        store.apply_state_op(session, _safe_reference_op())
        store.apply_state_op(
            session,
            {
                "op": "annotation_updated",
                "annotation": {
                    "id": "r1",
                    "type": REFERENCE_TYPE,
                    "label": "Handbook",
                },
            },
        )
        stored = session.state["annotations"][0]
        assert stored["label"] == "Handbook"
        assert stored["target"] == "https://example.org"

    def test_an_unrecognised_target_kind_is_refused_by_an_op(self, store):
        session = store.create()
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                _create_op(
                    _reference(
                        target_kind="graph_node",
                        target="x",
                        position={"x": 0, "y": 0},
                    )
                ),
            )
        assert session.state["annotations"] == []

    def test_a_preview_smuggling_a_remote_reference_is_refused_by_an_op(self, store):
        session = store.create()
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                _create_op(
                    _reference(
                        target_kind="url",
                        target="https://example.org",
                        preview={"image_url": "https://evil.example/x.png"},
                        position={"x": 0, "y": 0},
                    )
                ),
            )
        assert session.state["annotations"] == []

    def test_the_payload_survives_the_browsers_whole_object_echo(self, store):
        # The browser re-sends the WHOLE annotation on every move/resize/lock
        # (sessionSyncClient.js), so the unconditional validator sees a
        # complete payload on an ordinary move. It must accept it, not refuse
        # the batch.
        session = store.create()
        store.apply_state_op(session, _safe_reference_op())
        echo = dict(session.state["annotations"][0])
        echo["position"] = {"x": 9, "y": 9}
        store.apply_state_op(session, {"op": "annotation_updated", "annotation": echo})
        assert session.state["annotations"][0]["position"] == {"x": 9, "y": 9}
        assert session.state["annotations"][0]["target"] == "https://example.org"


class TestSavedViewMetadata:
    """A saved view is opened by a browser that renders the reference, so a
    view carrying an unsafe target is the same hazard as a live session's."""

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_an_unsafe_reference_in_saved_view_metadata_is_refused(self, target):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_reference(target_kind="url", target=target)],
            }
        }
        assert saved_view_annotation_error(metadata) is not None

    def test_a_safe_reference_in_saved_view_metadata_is_accepted(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    _reference(target_kind="url", target="https://example.org")
                ],
            }
        }
        assert saved_view_annotation_error(metadata) is None

    def test_the_legacy_annotations_list_is_covered_too(self):
        metadata = {
            "annotations": [_reference(target_kind="url", target="javascript:alert(1)")]
        }
        assert saved_view_annotation_error(metadata) is not None

    def test_the_sanitizer_strips_an_unsafe_target_but_keeps_the_tile(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    _reference(
                        target_kind="url",
                        target="javascript:alert(1)",
                        label="Looks innocent",
                    )
                ],
            }
        }
        sanitized = sanitize_saved_view_metadata(metadata)
        annotation = sanitized["annotation_document"]["annotations"][0]
        assert "target" not in annotation
        # The tile itself survives, so an author can find and fix or delete it
        # rather than wondering where their annotation went.
        assert annotation["label"] == "Looks innocent"
        assert annotation["target_kind"] == "url"
        # Never mutates the caller's dict (callers read this straight off live
        # Node.metadata).
        assert (
            metadata["annotation_document"]["annotations"][0]["target"]
            == "javascript:alert(1)"
        )

    def test_the_sanitizer_leaves_a_safe_target_byte_identical(self):
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    _reference(target_kind="url", target="https://example.org")
                ],
            }
        }
        assert sanitize_saved_view_metadata(metadata) == metadata

    def test_the_sanitizer_leaves_non_url_reference_kinds_alone(self):
        # A session id that is not a URL must not be stripped for failing to
        # be one.
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_reference(target_kind="session", target="8244-1742")],
            }
        }
        assert sanitize_saved_view_metadata(metadata) == metadata


class TestRejectionCannotBeTurnedIntoAcceptance:
    """The falsifiability half of the URL guarantee.

    Each test asserts that NO accepting path exists for an unsafe target, by
    sweeping every write entry point rather than trusting one. A mutation that
    loosens any single layer — deleting the `_validate_annotation` call, the
    `_require_safe_reference_target` call, the builders' content check, or the
    saved-view guard — fails here even though the other layers still refuse
    it, because each layer is asserted on its own.
    """

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_no_write_path_accepts_an_unsafe_target(self, target, store):
        content = {"target_kind": "url", "target": target}

        # 1. the pure validators
        assert reference_url_error(target) is not None
        assert reference_content_error(content) is not None
        assert (
            reference_annotation_error(_reference(**content), require_complete=True)
            is not None
        )

        # 2. the object-model builders (what the MCP tools go through)
        with pytest.raises(ValueError):
            build_annotation(type=REFERENCE_TYPE, x=0, y=0, content=content)

        # 3. a raw session op — create and update alike
        session = store.create()
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                _create_op(_reference(position={"x": 0, "y": 0}, **content)),
            )
        store.apply_state_op(session, _safe_reference_op())
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                {
                    "op": "annotation_updated",
                    "annotation": {"id": "r1", "type": REFERENCE_TYPE, **content},
                },
            )

        # 4. SavedView metadata
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [_reference(**content)],
            }
        }
        assert saved_view_annotation_error(metadata) is not None
        assert (
            "target"
            not in sanitize_saved_view_metadata(metadata)["annotation_document"][
                "annotations"
            ][0]
        )

        # 5. and after all of that, nothing unsafe is stored
        assert all(
            a.get("target") == "https://example.org"
            for a in session.state["annotations"]
        )

    def test_the_gate_is_an_allowlist_not_a_denylist_of_known_bad_names(self):
        # A scheme nobody thought to name must be refused too, or the gate
        # only holds until the next one is invented.
        for scheme in ("about", "blob", "chrome", "intent", "jar", "ws", "ftp", "tel"):
            assert reference_url_error(f"{scheme}://example.org/x") is not None, scheme
            assert reference_url_error(f"{scheme}:example.org/x") is not None, scheme
