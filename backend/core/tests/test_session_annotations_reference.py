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

import json
import os

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
    REFERENCE_WHITESPACE_CHARS,
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
    # Forms that make urlsplit RAISE rather than return a bad split. The gate
    # refuses on the exception, and a mutation replacing that refusal with
    # `return None` left the entire backend green — nothing in this list made
    # the parser raise, so an unsafe scheme behind an unparseable authority was
    # storable on every write path with the suite green (round 1 mutation
    # review, S1). A valid IPv6 host is asserted acceptable in
    # TestCrossLanguageUrlGateAgreement, so this cannot be satisfied by
    # refusing every bracket.
    "javascript://[/alert(1)",
    "http://[::1",
    "https://[::1",
]

# The subset of UNSAFE_TARGETS that a NON-url reference may legitimately hold,
# and therefore the only ones a two-step kind flip could ever promote. The
# others — empty, whitespace-only, and the control-character ones — are refused
# under every target kind, so they are unstorable at step one and there is no
# sequence to test. (That they are refused for a `resource` target too is
# asserted in TestPayloadRules and TestUrlGate.)
FLIPPABLE_UNSAFE_TARGETS = [
    t
    for t in UNSAFE_TARGETS
    if t.strip() and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in t)
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


_URL_GATE_FIXTURE = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "docs",
    "fixtures",
    "reference_url_gate.json",
)
with open(_URL_GATE_FIXTURE, encoding="utf-8") as _handle:
    _URL_GATE = json.load(_handle)


class TestTargetEmptinessUsesTheGatesOwnWhitespaceSet:
    """A target of nothing but whitespace is empty, by the gate's definition.

    Round 4 of the review loop: the non-empty check used bare ``str.strip()``,
    whose notion of whitespace excludes U+FEFF. So a ``session`` or
    ``resource`` target of a single U+FEFF was stored as non-empty here, while
    the canvas trimmed it to nothing with the shared set and reported
    ``'missing'`` — a tile that could only ever draw broken, created through a
    path that reported success. ``url`` was already safe because
    ``reference_url_error`` re-checks it with the right set; these two kinds
    are not re-checked anywhere, so this was their only gate.
    """

    @pytest.mark.parametrize("kind", ["session", "resource"])
    @pytest.mark.parametrize(
        "target",
        ["\ufeff", "\u0085", "\u00a0", "\u3000", "\u2028", " \ufeff \u0085 "],
    )
    def test_a_whitespace_only_target_is_not_a_target(self, kind, target):
        error = reference_content_error(
            {"target_kind": kind, "target": target}, require_complete=True
        )
        assert error is not None
        assert "non-empty" in error

    @pytest.mark.parametrize("kind", ["session", "resource"])
    def test_a_target_padded_with_that_whitespace_is_still_accepted(self, kind):
        assert (
            reference_content_error(
                {"target_kind": kind, "target": "\ufeff8244-1742\u0085"},
                require_complete=True,
            )
            is None
        )


class TestTheWhitespaceSetItselfIsPinned:
    """The ENUMERATION is the contract, not a sample of it.

    Round 4's mutation pass narrowed this set to only the characters some
    ``refuse`` case below happens to use — 10 of 26 — and the whole backend
    stayed green. A sampled refuse list can only ever pin the characters
    someone thought to write down. Both sides therefore assert equality with
    ``docs/fixtures/reference_url_gate.json``'s ``whitespace`` array, so a
    character added or dropped on one side fails on the side that moved.
    """

    def test_the_set_is_exactly_the_shared_enumeration(self):
        expected = {chr(int(code, 16)) for code in _URL_GATE["whitespace"]}
        assert REFERENCE_WHITESPACE_CHARS == expected

    @pytest.mark.parametrize("code", _URL_GATE["whitespace"])
    def test_every_enumerated_character_is_refused_mid_path(self, code):
        char = chr(int(code, 16))
        assert reference_url_error(f"https://example.org/a{char}b") is not None

    @pytest.mark.parametrize("code", _URL_GATE["whitespace"])
    def test_every_enumerated_character_is_handled_at_the_ends(self, code):
        """Padding is trimmed — unless the character is a C0 control.

        Five members of the set (U+0009-U+000D) are also control characters,
        and ``_has_control_characters`` refuses those OUTRIGHT rather than
        stripping them, before the trim is reached. That is deliberate and
        documented there: a browser strips a tab inside a scheme before
        resolving it, so normalising first and asking afterwards validates one
        string and runs another. Both outcomes are correct; asserting a single
        one for all 26 would be asserting something false about five of them.
        """
        char = chr(int(code, 16))
        error = reference_url_error(f"{char}https://example.org/x{char}")
        if ord(char) < 0x20:
            assert error is not None
            assert "control characters" in error
        else:
            assert error is None


class TestSavedViewPathDemandsACompletePayload:
    """A SavedView annotation with no ``target_kind`` must not be stored.

    Round 4's mutation pass flipped this call site's ``require_complete`` to
    ``False`` and the whole backend stayed green — while the mutation makes a
    ``javascript:`` target STORABLE through node metadata, because
    ``reference_content_error`` only consults the scheme gate under
    ``if target_kind == "url"`` and an absent kind is ``None``. That is the
    exact gap ``reference_content_error``'s own docstring warns about, and the
    sibling raw-op call site already had this test; this one did not.
    """

    @pytest.mark.parametrize(
        "target",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "vbscript:msgbox(1)",
        ],
    )
    def test_a_kindless_saved_view_reference_is_refused(self, target):
        metadata = {
            "annotations": [{"id": "a1", "type": "reference", "target": target}]
        }
        assert saved_view_annotation_error(metadata) is not None

    def test_a_complete_saved_view_reference_still_passes(self):
        metadata = {
            "annotations": [
                {
                    "id": "a1",
                    "type": "reference",
                    "target_kind": "url",
                    "target": "https://example.org/x",
                }
            ]
        }
        assert saved_view_annotation_error(metadata) is None


class TestCrossLanguageUrlGateAgreement:
    """The backend gate and the renderer's own gate must agree.

    They are separate implementations on purpose — the backend decides what may
    be stored, the canvas decides what it may draw as clickable, and the canvas
    must not trust what it is handed. Separate implementations drift, and the
    drift found in round 1 of the review loop was in the dangerous direction:
    ``urlsplit`` is lenient where the WHATWG ``URL`` parser is strict, so the
    backend accepted ``https://exa mple.org/x``, ``https://example.org:99999/x``
    and ``http://1.2.3.4:x/`` while the canvas refused all three — storing a
    target that then rendered permanently broken and labelled "Unsafe link —
    not opened", which is a false statement about a typo, on a tile with no GUI
    way to repoint it.

    Writing the fixture then found drift the other way too: the canvas's
    WHATWG parser *repaired* ``http:///path`` into ``http://path/``, re-reading
    the path as the hostname, so a tile would have opened somewhere its author
    never wrote. Round 2 found a third and a fourth, from the "no space" rule
    that had been added for the first: it refused a space in a path or query,
    which the canvas accepted, while letting a NON-BREAKING space through in
    the host, which the canvas refuses. Both sides now refuse whitespace
    anywhere in the trimmed target.

    What this pins is every case in the fixture, on both sides — not a general
    claim that two different parsers agree everywhere. That is the point of
    enumerating them.

    The fixture is shared with
    ``packages/ui-graph-canvas/tests/ReferenceAnnotation.test.jsx``, so moving a
    case on either side fails on that side.
    """

    @pytest.mark.parametrize("target", _URL_GATE["accept"])
    def test_accepts_every_shared_accept_case(self, target):
        assert reference_url_error(target) is None, target

    @pytest.mark.parametrize("target", _URL_GATE["refuse"])
    def test_refuses_every_shared_refuse_case(self, target):
        assert reference_url_error(target) is not None, target

    def test_the_fixture_covers_the_schemes_the_task_names(self):
        refused = " ".join(_URL_GATE["refuse"]).lower()
        for scheme in ("javascript:", "data:", "file:", "vbscript:"):
            assert scheme in refused, scheme


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

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_even_a_trusted_replay_cannot_store_an_unsafe_target(self, store, target):
        # `_require_safe_reference_target` is deliberately skipped for an undo's
        # trusted replay, which is exactly why `_validate_annotation` carries an
        # unconditional check as well. Deleting that check left the entire
        # backend green while making a trusted replay store javascript:
        # (round 1 mutation review, S3) — the one assertion that tells the
        # unconditional floor apart from the two call-site guards.
        session = store.create()
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                _create_op(
                    _reference(
                        target_kind="url", target=target, position={"x": 0, "y": 0}
                    )
                ),
                trusted_replay=True,
            )
        assert session.state["annotations"] == []

    def test_a_trusted_replay_of_a_safe_reference_still_works(self, store):
        # The unconditional check must not break undo for a legitimate
        # annotation, which is the reason the image guard exempts replays.
        session = store.create()
        store.apply_state_op(session, _safe_reference_op(), trusted_replay=True)
        assert session.state["annotations"][0]["target"] == "https://example.org"

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


class TestTwoStepTargetKindFlip:
    """The hole round 1 of the review loop found, pinned on both op paths.

    A reference's rules are cross-field and the store applies a patch with a
    shallow merge, so either half can arrive while the other stays stored.
    Every single-write case was already covered; the two-step was not, and the
    gate resolved only ``target_kind`` from the stored annotation — so a
    ``resource`` reference holding an unsafe string (legitimate: a resource
    target is a node id, not a URL) could be promoted to ``url`` by a patch
    that carried no target at all, and the gate had nothing to check.

    These assert the composite state, not just the call's return: the point is
    that no SEQUENCE of individually-acceptable writes reaches a url reference
    with an unsafe target.
    """

    @pytest.mark.parametrize("target", FLIPPABLE_UNSAFE_TARGETS)
    @pytest.mark.parametrize("stored_kind", ["resource", "session"])
    def test_flipping_the_kind_to_url_over_an_update_is_refused(
        self, store, stored_kind, target
    ):
        session = store.create()
        store.apply_state_op(
            session,
            _create_op(
                _reference(
                    target_kind=stored_kind,
                    target=target,
                    position={"x": 0, "y": 0},
                )
            ),
        )
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "r1",
                        "type": REFERENCE_TYPE,
                        "target_kind": "url",
                    },
                },
            )
        stored = session.state["annotations"][0]
        assert stored["target_kind"] == stored_kind
        assert stored["target"] == target

    @pytest.mark.parametrize("target", FLIPPABLE_UNSAFE_TARGETS)
    def test_flipping_the_kind_to_url_over_a_same_id_upsert_is_refused(
        self, store, target
    ):
        # The create branch drops require_complete when an annotation already
        # exists under the id, so the upsert is its own path to the same flip.
        session = store.create()
        store.apply_state_op(
            session,
            _create_op(
                _reference(
                    target_kind="resource", target=target, position={"x": 0, "y": 0}
                )
            ),
        )
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                _create_op(_reference(target_kind="url", position={"x": 0, "y": 0})),
            )
        stored = session.state["annotations"][0]
        assert stored["target_kind"] == "resource"
        assert stored["target"] == target

    def test_the_same_flip_is_refused_by_the_pure_validator(self, store):
        stored = _reference(target_kind="resource", target="javascript:alert(1)")
        assert reference_annotation_error({"target_kind": "url"}, stored) is not None

    def test_a_flip_to_url_with_a_safe_target_in_the_same_write_is_allowed(self, store):
        # The refusal must be about the resulting annotation being unsafe, not
        # about flipping a kind — repointing a reference is legitimate.
        session = store.create()
        store.apply_state_op(
            session,
            _create_op(
                _reference(
                    target_kind="resource",
                    target="resource-1",
                    position={"x": 0, "y": 0},
                )
            ),
        )
        store.apply_state_op(
            session,
            {
                "op": "annotation_updated",
                "annotation": {
                    "id": "r1",
                    "type": REFERENCE_TYPE,
                    "target_kind": "url",
                    "target": "https://example.org",
                },
            },
        )
        stored = session.state["annotations"][0]
        assert stored["target_kind"] == "url"
        assert stored["target"] == "https://example.org"

    def test_flipping_a_url_reference_away_from_url_is_allowed(self, store):
        # The other direction is fine: a session/resource target is not held
        # to the scheme rule, so demoting a safe url reference is not a flip
        # into danger.
        session = store.create()
        store.apply_state_op(session, _safe_reference_op())
        store.apply_state_op(
            session,
            {
                "op": "annotation_updated",
                "annotation": {
                    "id": "r1",
                    "type": REFERENCE_TYPE,
                    "target_kind": "resource",
                    "target": "resource-1",
                },
            },
        )
        assert session.state["annotations"][0]["target_kind"] == "resource"

    def test_a_label_only_patch_still_re_checks_the_stored_pair(self, store):
        # Merging the stored annotation means every patch re-validates the
        # whole resulting object. A stored annotation that is already bad is
        # not grandfathered by an unrelated edit.
        session = store.create()
        store.apply_state_op(session, _safe_reference_op())
        # Reach past the op path to plant a bad stored state, the way a gap in
        # some other path would.
        session.state["annotations"][0]["target"] = "javascript:alert(1)"
        with pytest.raises(OpError):
            store.apply_state_op(
                session,
                {
                    "op": "annotation_updated",
                    "annotation": {
                        "id": "r1",
                        "type": REFERENCE_TYPE,
                        "label": "Harmless rename",
                    },
                },
            )


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

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_a_reference_declared_only_by_kind_is_refused(self, target):
        # `saved_view_annotation_error` reads RAW stored annotations out of node
        # metadata, so unlike the two session-op call sites nothing has
        # canonicalised `type` onto them first. An annotation carrying only the
        # legacy `kind` alias must still be recognised as a reference — a
        # mutation dropping that fallback left the whole backend green while
        # making saved-view metadata accept `{"kind": "reference", ...}` with a
        # javascript: target (round 1 mutation review, S2).
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    {
                        "id": "r1",
                        "kind": REFERENCE_TYPE,
                        "target_kind": "url",
                        "target": target,
                    }
                ],
            }
        }
        assert saved_view_annotation_error(metadata) is not None

    def test_the_sanitizer_also_recognises_a_kind_only_reference(self):
        # The guard and the sanitizer must agree about what a reference is, or
        # a view refused by one is left intact by the other.
        metadata = {
            "annotation_document": {
                "schema_version": 1,
                "annotations": [
                    {
                        "id": "r1",
                        "kind": REFERENCE_TYPE,
                        "target_kind": "url",
                        "target": "javascript:alert(1)",
                    }
                ],
            }
        }
        sanitized = sanitize_saved_view_metadata(metadata)
        assert "target" not in sanitized["annotation_document"]["annotations"][0]

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
