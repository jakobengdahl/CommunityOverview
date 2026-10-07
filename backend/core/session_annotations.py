"""Annotation-shape helpers over the generic session annotation store.

``session_store``/``session_manager`` treat an annotation as an opaque dict —
they validate only the boundary fields (``type``/``kind``, ``id``, ``position``)
and apply the ``annotation_created``/``annotation_updated``/``annotation_deleted``
ops without knowing what a "note" or a "line" is. The v1 annotation shape
itself (geometry/position/size projections, per-type payload fields) is
defined by ``packages/ui-graph-canvas/src/utils/annotationModel.js`` and
consumed as-is by the canvas. This module builds and reads that same shape
from Python, once, so MCP tools do not each hand-roll it.

Three helper sets live here:

* note-shape helpers (``is_note``, ``build_note_annotation``,
  ``build_note_patch``, ``project_note``) — used by the dedicated
  ``list_sticky_notes``/``create_sticky_note``/``update_sticky_note``/
  ``delete_sticky_note`` MCP tools (``backend/service/mcp_tools.py``).
* generic-type helpers (``build_annotation``, ``build_annotation_patch``,
  ``project_annotation``, and the ``*_type`` functions) — used by the
  generic ``list_annotations``/``create_annotation``/``update_annotation``/
  ``delete_annotation``/``reorder_annotation``/``set_annotation_lock``/
  ``duplicate_annotation`` tools, which cover every v1 type except ``note``
  (kept on its own dedicated tool set above) and ``group`` (node-membership
  boxes, kept on its own tool set below).
* group-shape helpers (``is_group``, ``build_group_annotation``) — used by
  the dedicated ``create_group_annotation``/``update_group_members`` MCP
  tools. Membership itself is edited through the ``group_membership_changed``
  op (``update_group_members``, not this module), never by re-supplying
  ``member_node_ids`` through a generic patch — see
  ``build_group_annotation``'s docstring for why.

``image_annotation_error`` also lives here: the contract rule that an
``image`` annotation's pixel content is always an embedded, server-ingested
data URI is a property of the annotation *shape*, so ``SessionStore`` applies
it in one place rather than per entry point. Two writes are exempt by design
— re-sending the URL already stored under that id, and an undo replaying its
own inverse op — see the function's docstring and ``SessionStore.apply_state_op``.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional
from urllib.parse import urlsplit

NOTE_TYPE = "note"
GROUP_TYPE = "group"
IMAGE_TYPE = "image"
REFERENCE_TYPE = "reference"
DEFAULT_NOTE_SIZE = {"w": 160, "h": 96}
# A reference tile shows a badge, a label and (when present) a preview line,
# so unlike a note it is wider than tall by default.
DEFAULT_REFERENCE_SIZE = {"w": 220, "h": 72}
# A group box has no natural single-member size the way a note does; this is
# just a usable default footprint for a freshly created, still-empty group —
# callers passing member ids up front should size it themselves.
DEFAULT_GROUP_SIZE = {"w": 320, "h": 200}

# The only `content.image.url` form an `image` annotation may be persisted
# with: an embedded base64 data URI of the content type server-side ingest
# *emits* (``image_ingest.OPTIMIZED_CONTENT_TYPE``). Deliberately not the
# wider set of formats ingest *accepts* as input — those are all re-encoded,
# so accepting their prefixes here would widen what a forged data URI may
# claim to be without any path ever producing one. Kept as a literal rather
# than imported so this module (which ``session_store`` imports on every
# annotation op) does not pull Pillow/httpx into the store's import path;
# ``test_session_annotations_image_guard.py`` pins it to the optimizer's
# output so the two cannot drift apart.
EMBEDDED_IMAGE_URL_PREFIXES = ("data:image/webp;base64,",)

# Every v1 type except `note` and `group` — see module docstring for why
# those two are excluded from the generic tool set. `frame` (a plain box with
# no fill) was a member of this set until task-annotation-merge-frame-into-
# shape-rectangle folded it into `shape`: a `shape` with a transparent fill
# and a coloured border now covers what a standalone `frame` used to be, and
# `frame` is no longer a recognised annotation type at all. No migration was
# written for annotations already stored with type `frame` (nobody used the
# annotation features yet — see task-annotation-tolerate-unexpected-data);
# they are simply no longer resolved by `normalize_generic_type` below, the
# same as any other unrecognised type.
GENERIC_ANNOTATION_TYPES: FrozenSet[str] = frozenset(
    {
        "text",
        "label",
        "line",
        "shape",
        "icon",
        "vote_dot",
        "image",
        "freehand",
        "heatmap",
        REFERENCE_TYPE,
    }
)
ALL_ANNOTATION_TYPES: FrozenSet[str] = GENERIC_ANNOTATION_TYPES | {
    NOTE_TYPE,
    GROUP_TYPE,
}
# Mirrors session_store's `_LEGACY_ANNOTATION_ALIASES`: `arrow` is still an
# accepted input alias for `line` (docs/ANNOTATION_CONTRACT.md).
LEGACY_ANNOTATION_ALIASES: Dict[str, str] = {"arrow": "line"}

# Envelope fields the generic builders/projector manage themselves; a caller
# supplying one of these inside `content` would silently overwrite bookkeeping
# the caller does not otherwise control (e.g. smuggling a `type` change
# through a patch), so it is rejected instead of merged.
_RESERVED_ANNOTATION_KEYS = {
    "id",
    "type",
    "kind",
    "geometry",
    "position",
    "size",
    "style",
    "z",
    "locked",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
    # Server-owned versioning bookkeeping (dec-annotation-field-patches-and-
    # conflicts) — see session_store.py's _ANNOTATION_META_FIELDS. Never
    # caller-settable; "version" is surfaced read-only by project_note/
    # project_annotation so a caller can supply it back as base_version, but
    # neither belongs inside a content/patch payload.
    "version",
    "field_versions",
}

# `heatmap` (docs/ANNOTATION_CONTRACT.md's "Heat-map circles"): a soft red
# circle whose `content.intensity` is an integer 0-10, 0 invisible and 10 the
# strongest red. Mirrors HEATMAP_* in
# packages/ui-graph-canvas/src/utils/annotationModel.js.
HEATMAP_TYPE = "heatmap"
HEATMAP_MIN_INTENSITY = 0
HEATMAP_MAX_INTENSITY = 10
HEATMAP_DEFAULT_INTENSITY = 5
HEATMAP_DEFAULT_DIAMETER = 160

# The `content.shape` variants a `shape` annotation accepts
# (docs/ANNOTATION_CONTRACT.md), mirroring
# `packages/ui-graph-canvas/src/utils/annotationModel.js`'s `ANNOTATION_SHAPES`.
# A string outside this set is not rejected — `backend/DEVELOPMENT.md`
# documents that a name outside the set is "stored verbatim and drawn as a
# rectangle" by the canvas, matching `normalizeShapeName`'s behaviour of
# keeping an unrecognised name rather than discarding it, so this constant is
# used for documentation/tests, not as a rejection list. Only the *type* of
# `content.shape` is validated below (see `_validate_generic_content`).
ANNOTATION_SHAPES: FrozenSet[str] = frozenset(
    {"rectangle", "circle", "triangle", "rhombus", "hexagon", "process_arrow"}
)

# The generic types whose `content.attachment` may bind them to a node
# (docs/ANNOTATION_CONTRACT.md's "Attachment and detach behavior"). `line`
# attaches per-endpoint (`start`/`end`) instead, validated separately.
#
# `vote_dot` was a member of this set until task-annotation-vote-dot-simplify
# retired its attachment behaviour: a vote dot is now a plain coloured dot
# that always lives on its own. An `attachment` a caller still sends in a
# vote_dot's `content` is no longer structurally validated as one (it is
# simply free-form, unvalidated content, like any field this module does not
# specifically type-constrain) — the canvas never reads it as an attachment
# either way (see ATTACHABLE_OVERLAY_KINDS in
# packages/ui-graph-canvas/src/utils/annotations.js). No migration was
# written for a vote_dot already stored with one.
ATTACHABLE_ANNOTATION_TYPES: FrozenSet[str] = frozenset({"text", "label", "icon"})

# ==================== Reference (navigational link) constants ====================

# The three things a `reference` annotation may point at
# (docs/ANNOTATION_CONTRACT.md's "Reference tiles"). Mirrors
# REFERENCE_TARGET_KINDS in
# packages/ui-graph-canvas/src/utils/annotationModel.js.
#
# * `session` — another visualization session, by its session id.
# * `url` — an external web page, by an http/https URL.
# * `resource` — supporting material already in the graph, by node id.
#
# A reference is *annotation* state: it lives in the session's annotation
# document and is never written to the main graph, so none of these three
# introduces a node type, a relationship type or a graph write. A `resource`
# reference names a node it does not create and does not own.
REFERENCE_TARGET_KINDS: FrozenSet[str] = frozenset({"session", "url", "resource"})

# The only URL schemes a `url` reference may be persisted with. An allowlist,
# not a denylist of the schemes that are known to be dangerous today: a
# reference's target is rendered into an activatable link, so a scheme nobody
# thought to list must be refused rather than shipped. This is what rejects
# `javascript:`, `data:`, `file:` and `vbscript:` — and equally the next
# scheme of that family that gets invented.
REFERENCE_SAFE_URL_SCHEMES: FrozenSet[str] = frozenset({"http", "https"})

# Field caps. A session's annotation document is session state, not a blob
# store: without these a reference could carry an arbitrarily large payload
# that every client then has to download on every open.
REFERENCE_MAX_TARGET_LENGTH = 2048
REFERENCE_MAX_LABEL_LENGTH = 200
REFERENCE_MAX_ICON_LENGTH = 64
REFERENCE_MAX_PREVIEW_FIELD_LENGTH = 500

# The complete set of `preview` keys a reference may carry, each a string.
# Deliberately narrow and text-only. A wider free-form preview object would
# re-open exactly the hole `image_annotation_error` closes for `image`: a
# key holding a remote URL that every viewer's browser then fetches on open,
# going around image ingest's format validation, budgets and SSRF checks.
# Widening this set later is additive and costs nothing; shipping a preview
# that can hold a remote reference and taking it back is not. Pixel content
# for a reference is therefore out of scope for v1 — see
# docs/ANNOTATION_CONTRACT.md's "Reference tiles".
REFERENCE_PREVIEW_FIELDS: FrozenSet[str] = frozenset({"title", "description", "site"})


# The whitespace a reference target may not contain, and the set trimmed from
# its ends. Spelled out character by character rather than deferring to
# ``str.isspace()`` because the canvas runs the same gate in JavaScript
# (``isSafeReferenceUrl`` in
# packages/ui-graph-canvas/src/utils/annotationModel.js) and the two languages
# do not mean the same thing by "whitespace": ``str.isspace()`` is true for
# U+0085 (NEL) where JavaScript's ``\s`` is not, and ``\s`` matches U+FEFF
# where ``str.isspace()`` does not — and ``str.strip()`` and
# ``String.prototype.trim()`` split exactly the same way. Round 3 of the review
# loop measured both: U+0085 mid-target was refused here and accepted there, so
# a paste minted a tile the server then dropped; U+FEFF mid-target was accepted
# here and refused there, so a stored tile drew permanently as "Unsafe link —
# not opened". Both are the failure modes ``reference_url_error``'s own comment
# says the rule exists to prevent, and two earlier rounds had each fixed one
# instance of the same class by hand.
#
# The set is the UNION of the two languages' notions, so each side refuses
# everything either language would call whitespace — strictly stricter than
# either alone, which is the safe direction for a gate. It is enumerated in
# docs/fixtures/reference_url_gate.json, which both sides drive, so a character
# that stops agreeing fails on the side that moved.
REFERENCE_WHITESPACE_CHARS: FrozenSet[str] = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)

_REFERENCE_WHITESPACE_TRIM = "".join(sorted(REFERENCE_WHITESPACE_CHARS))


def _has_control_characters(value: str) -> bool:
    """Whether *value* contains a C0 control character or DEL.

    A tab or a newline inside a scheme is the standard way an unsafe URL is
    smuggled past a scheme check: a browser strips them before resolving
    ``java\tscript:alert(1)``, so a checker that does not see them refuses
    nothing while the browser happily runs it. Rejecting the whole string
    rather than stripping it keeps the value this module *validated*
    byte-identical to the value it stores — a normalising check validates one
    string and persists another, which is its own class of bug.
    """
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def reference_url_error(url: Any) -> Optional[str]:
    """Why *url* may not be a ``url`` reference's target, or ``None``.

    The single scheme gate for the whole stack: every write path that can
    persist a reference routes through it (the generic MCP
    create/update tools, a raw session ``annotation_created``/
    ``annotation_updated`` op via ``session_store._validate_annotation``, and
    SavedView node metadata via ``saved_view_annotation_error``), so there is
    one place that decides what a reference may link to rather than one check
    per entry point.

    Requires an explicit allowlisted scheme *and* a host. A scheme-relative
    ``//evil.example`` or a bare ``/path`` parses with no scheme at all and is
    refused for that reason: a reference target is resolved by a browser with
    the app's own origin as its base, so a relative target is both ambiguous
    and a way to point an innocuous-looking tile at the app itself.

    Unlike ``image_annotation_error`` there is no "the same value is already
    stored" exemption. That exemption exists for images because annotations
    persisted before the ingest rule had to stay movable; nothing unsafe can
    ever have been persisted as a reference, so an exemption would only ever
    admit a value that got in by some path this gate does not cover.
    """
    if not isinstance(url, str):
        return "content.target must be a string for a url reference"
    if _has_control_characters(url):
        return (
            "content.target must not contain control characters; a tab or "
            "newline inside a URL scheme is stripped by the browser and is "
            "not accepted here"
        )
    candidate = url.strip(_REFERENCE_WHITESPACE_TRIM)
    if not candidate:
        return "content.target must not be empty for a url reference"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "content.target is not a parseable URL"
    scheme = parts.scheme.lower()
    if scheme not in REFERENCE_SAFE_URL_SCHEMES:
        return (
            "content.target must be an "
            f"{'/'.join(sorted(REFERENCE_SAFE_URL_SCHEMES))} URL; "
            f"the scheme {scheme or '(none)'!r} is not accepted for a url "
            "reference"
        )
    if not parts.hostname:
        return "content.target must include a host"
    # The two checks below exist to keep this gate and the renderer's
    # (``isSafeReferenceUrl`` in
    # packages/ui-graph-canvas/src/utils/annotationModel.js) deciding the same
    # thing. ``urlsplit`` is deliberately lenient where the WHATWG ``URL``
    # constructor is strict, and a disagreement hurts in both directions: a
    # target this side accepts and the canvas refuses is stored and then drawn
    # permanently as "Unsafe link — not opened" (a false statement about a
    # typo, on a tile with no GUI way to repoint it), while a target this side
    # refuses and the canvas accepts passes the paste gate, lands on the canvas
    # as a local node, and is then dropped by the server — leaving an unsaved
    # tile with no explanation.
    #
    # Whitespace is refused ANYWHERE in the trimmed target, not just a space
    # and not just in the authority. Round 2 of the review loop caught the
    # narrower "no space" rule failing both ways at once: it refused a space in
    # a path or query (which the canvas happily accepts, so the only human
    # creation path could produce a tile the server then rejected) while
    # letting a NON-BREAKING space through in the HOST, which IDNA rejects — so
    # the canvas drew it permanently broken. Round 3 then found that "every
    # whitespace character" was still two different rules, because each side
    # was asking its own language; both now ask
    # ``REFERENCE_WHITESPACE_CHARS``, whose comment has the measurements. A
    # real URL carries its spaces percent-encoded, and ``%20`` is accepted.
    if any(ch in REFERENCE_WHITESPACE_CHARS for ch in candidate):
        return "content.target must not contain whitespace"
    try:
        port = parts.port
    except ValueError:
        return "content.target has an invalid port"
    if port is not None and not 1 <= port <= 65535:
        return "content.target has an invalid port"
    return None


def _reference_preview_error(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, dict):
        return "content.preview must be an object"
    unknown = sorted(set(value) - REFERENCE_PREVIEW_FIELDS)
    if unknown:
        return (
            f"content.preview may only carry {sorted(REFERENCE_PREVIEW_FIELDS)}; "
            f"unknown field(s) {unknown}"
        )
    for key, field in value.items():
        if field is None:
            continue
        if not isinstance(field, str):
            return f"content.preview.{key} must be a string"
        if _has_control_characters(field):
            return f"content.preview.{key} must not contain control characters"
        if len(field) > REFERENCE_MAX_PREVIEW_FIELD_LENGTH:
            return (
                f"content.preview.{key} must be at most "
                f"{REFERENCE_MAX_PREVIEW_FIELD_LENGTH} characters"
            )
    return None


def reference_content_error(
    source: Dict[str, Any], *, require_complete: bool = False
) -> Optional[str]:
    """Why *source* is not a valid ``reference`` payload, or ``None``.

    *source* is either the ``content`` dict a builder is about to merge or an
    already-merged annotation dict — both put payload fields at the top level,
    the same convention ``_validate_generic_content`` follows.

    Validation is per *present* field, so a partial patch (moving a reference,
    renaming its label) is judged on what it actually changes. The one
    cross-field rule is the scheme gate: a payload that sets ``target`` while
    the resolved target kind is ``url`` must carry a safe URL. "Resolved"
    matters — a patch that sets only ``target`` on a stored url reference
    arrives here without a ``target_kind``, so callers pass the stored kind in
    as a default (see ``reference_annotation_error``).

    *require_complete* additionally demands that both ``target_kind`` and
    ``target`` are present. Set for a write that stands on its own — a fresh
    create, a whole stored annotation — and left off for a patch, which is
    allowed to touch one field. Without it a create carrying only ``target``
    would skip the scheme gate entirely, because the gate keys off the target
    kind and there would be none to read: an unsafe URL would get in through
    the gap between "no kind given" and "kind is not url".
    """
    if require_complete:
        for key in ("target_kind", "target"):
            if source.get(key) is None:
                return f"content.{key} is required for a reference annotation"
    target_kind = source.get("target_kind")
    if "target_kind" in source:
        if target_kind not in REFERENCE_TARGET_KINDS:
            return (
                f"content.target_kind must be one of {sorted(REFERENCE_TARGET_KINDS)}"
            )
    if "target" in source:
        target = source["target"]
        if not isinstance(target, str) or not target.strip():
            return "content.target must be a non-empty string"
        if len(target) > REFERENCE_MAX_TARGET_LENGTH:
            return (
                "content.target must be at most "
                f"{REFERENCE_MAX_TARGET_LENGTH} characters"
            )
        if _has_control_characters(target):
            return "content.target must not contain control characters"
        if target_kind == "url":
            error = reference_url_error(target)
            if error:
                return error
    for key, cap in (
        ("label", REFERENCE_MAX_LABEL_LENGTH),
        ("icon", REFERENCE_MAX_ICON_LENGTH),
    ):
        if key in source and source[key] is not None:
            value = source[key]
            if not isinstance(value, str):
                return f"content.{key} must be a string"
            if len(value) > cap:
                return f"content.{key} must be at most {cap} characters"
            if _has_control_characters(value):
                return f"content.{key} must not contain control characters"
    if "preview" in source:
        error = _reference_preview_error(source["preview"])
        if error:
            return error
    return None


def reference_annotation_error(
    annotation: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
    *,
    require_complete: bool = False,
) -> Optional[str]:
    """Why *annotation* may not be persisted as a ``reference``, or ``None``.

    The enforcement entry point ``session_store`` calls for every annotation
    op, so a browser's raw op batch is held to the identical rule the MCP
    tools apply — the generic tools bypassing a hardened path is exactly how
    the image guard came to be needed.

    *existing* supplies the stored annotation under this id, when there is
    one. **The whole stored annotation is merged under the patch**, and the
    result — the annotation as it would be *after* this write — is what gets
    validated. Not the patch alone, and not one hand-picked field of the
    stored state.

    That matters because a reference's rules are cross-field: the scheme gate
    reads ``target`` *and* ``target_kind``, and ``SessionStore`` applies a
    patch with a shallow ``dict.update``, so either field can arrive while
    the other stays stored. Resolving only one of them leaves the other
    unchecked, and a two-step write then slips past a gate that refuses both
    steps individually:

        created  {target_kind: "resource", target: "javascript:alert(1)"}
            -> accepted, correctly: a resource target is a node id, not a
               URL, so it is not held to the scheme rule
        updated  {target_kind: "url"}
            -> must be REFUSED, because the annotation this produces is a
               url reference whose target is "javascript:alert(1)"

    Validating the merged result is what makes that refusal fall out rather
    than needing to be enumerated, and it is the same shape
    ``_validate_generic_content`` already used on the MCP path — which is why
    the MCP tools refused this flip while the raw-op path accepted it. Two
    gates that disagree are one gate.

    Merging is **not** an exemption. A stored value is never a reason to
    accept anything: it is only ever re-checked, so a stored annotation that
    is already bad fails here too (see ``reference_url_error``).

    The type is resolved from *annotation* **or** from *existing*: a patch is
    allowed to omit ``type``, and reading the type from the patch alone would
    make every such patch non-reference and therefore unchecked. (The two
    store call sites canonicalise ``type`` onto the patch before calling, so
    they were never exposed to that; this keeps the function correct on its
    own terms for any other caller.)
    """
    annotation_kind = annotation_type_of(annotation)
    if annotation_kind is None and isinstance(existing, dict):
        annotation_kind = annotation_type_of(existing)
    if annotation_kind != REFERENCE_TYPE:
        return None
    source = (
        {**existing, **annotation} if isinstance(existing, dict) else dict(annotation)
    )
    return reference_content_error(source, require_complete=require_complete)


# Semantic default layer at creation (task-annotation-render-direct-
# manipulation's remaining scope: "semantic default layers - a per-kind
# default z at creation", docs/ANNOTATION_CONTRACT.md's "Layer order").
# Mirrors `DEFAULT_ANNOTATION_Z_BY_TYPE`/`defaultAnnotationZ` in
# packages/ui-graph-canvas/src/utils/annotationModel.js exactly — see that
# file's comment for the full reasoning (only `shape` and `heatmap` move,
# everything else including `note`/`group`/`image` stays at 0) — so an MCP/REST-created
# annotation and a GUI-created one of the same kind start on the same layer.
# `heatmap` shares shape's backdrop layer (see annotationModel.js).
SHAPE_DEFAULT_Z = -1
DEFAULT_ANNOTATION_Z_BY_TYPE: Dict[str, float] = {
    "shape": SHAPE_DEFAULT_Z,
    HEATMAP_TYPE: SHAPE_DEFAULT_Z,
}


def default_annotation_z(annotation_type: Optional[str]) -> float:
    return DEFAULT_ANNOTATION_Z_BY_TYPE.get(annotation_type, 0)


def _attachment_error(value: Any, *, field: str) -> Optional[str]:
    """Structural validation for an `attachment = {target_id, target_type,
    anchor, offset}` payload (docs/ANNOTATION_CONTRACT.md's "Attachment and
    detach behavior"). `None` clears/omits the attachment and is always
    valid; anything else must be a well-formed object. Unlike the shape/icon
    checks, a malformed attachment is rejected rather than merely
    type-checked, because a value that isn't a resolvable target reference
    doesn't mean anything (there is no "verbatim but unrecognised" case for
    it the way there is for a shape or icon name).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{field} must be an object"
    target_id = value.get("target_id")
    if target_id is None or (isinstance(target_id, str) and not target_id.strip()):
        return f"{field}.target_id is required and must not be empty"
    if not isinstance(target_id, (str, int, float)):
        return f"{field}.target_id must be a string"
    target_type = value.get("target_type")
    if target_type is not None and not isinstance(target_type, str):
        return f"{field}.target_type must be a string"
    anchor = value.get("anchor")
    if anchor is not None and not isinstance(anchor, str):
        return f"{field}.anchor must be a string"
    offset = value.get("offset")
    if offset is not None:
        if (
            not isinstance(offset, dict)
            or not isinstance(offset.get("x"), (int, float))
            or not isinstance(offset.get("y"), (int, float))
        ):
            return f"{field}.offset must be an object with numeric x and y"
    return None


def _line_endpoint_error(value: Any, *, field: str) -> Optional[str]:
    """Structural validation for a `line`'s `start`/`end` endpoint
    (docs/ANNOTATION_CONTRACT.md: "Line endpoints may attach to a node or to
    another annotation, or stay free-floating at a fixed model-space
    point."). `None` is valid (an endpoint carried only via the legacy
    `from`/`to` point fields, with no explicit `start`/`end`).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{field} must be an object"
    point = value.get("point")
    if point is not None:
        if (
            not isinstance(point, dict)
            or not isinstance(point.get("x"), (int, float))
            or not isinstance(point.get("y"), (int, float))
        ):
            return f"{field}.point must be an object with numeric x and y"
    return _attachment_error(value.get("attachment"), field=f"{field}.attachment")


def _validate_generic_content(
    ann_type: Optional[str],
    source: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Type-specific structural validation for a generic annotation's
    payload fields, given either the `content` dict a builder is about to
    merge or the already-merged annotation dict (both put payload fields at
    the top level — see `_apply_content`). Returns an error message, or
    `None` when *source* is valid for *ann_type*.

    Deliberately narrow: only the fields the v1 contract actually
    type-constrains are checked (`shape`, `icon`, `attachment`, a `line`'s
    `start`/`end`, a `heatmap`'s `intensity`) — everything else in `content` stays the free-form,
    verbatim payload `build_annotation`'s docstring describes. A `shape` or
    `icon` name outside its documented set is *not* an error (see
    `ANNOTATION_SHAPES`'s docstring); only its type is checked, so a caller
    gets a clear `invalid_content` for an obviously wrong payload (a number,
    a list) instead of silently corrupting the stored document with a value
    no renderer expects a string field to hold.
    """
    if not source:
        return None
    if ann_type == REFERENCE_TYPE:
        # A reference is the one generic type whose payload is fully
        # constrained rather than free-form: its `target` is rendered into an
        # activatable link, so an unvalidated field here is a navigation
        # surface, not a decoration that merely draws oddly. *existing*
        # resolves the target kind of a patch that changes only `target` —
        # see reference_annotation_error for why that is not an exemption.
        return reference_annotation_error(
            {**(existing or {}), **source, "type": REFERENCE_TYPE},
            existing,
            require_complete=existing is None,
        )
    if ann_type == "shape" and "shape" in source:
        shape = source["shape"]
        if not isinstance(shape, str) or not shape.strip():
            return "content.shape must be a non-empty string"
    if ann_type == "icon" and "icon" in source:
        icon = source["icon"]
        if not isinstance(icon, str) or not icon.strip():
            return "content.icon must be a non-empty string"
    if ann_type == HEATMAP_TYPE and "intensity" in source:
        intensity = source["intensity"]
        # A float or a bool is refused rather than rounded: the canvas steps
        # in whole levels, and an agent sending 7.5 or True has misread the
        # contract, which a silent rounding would hide from it.
        if (
            not isinstance(intensity, int)
            or isinstance(intensity, bool)
            or not HEATMAP_MIN_INTENSITY <= intensity <= HEATMAP_MAX_INTENSITY
        ):
            return (
                "content.intensity must be an integer from "
                f"{HEATMAP_MIN_INTENSITY} to {HEATMAP_MAX_INTENSITY}"
            )
    if ann_type in ATTACHABLE_ANNOTATION_TYPES and "attachment" in source:
        error = _attachment_error(source["attachment"], field="content.attachment")
        if error:
            return error
    if ann_type == "line":
        for key in ("start", "end"):
            if key in source:
                error = _line_endpoint_error(source[key], field=f"content.{key}")
                if error:
                    return error
    return None


def is_note(annotation: Dict[str, Any]) -> bool:
    """Whether *annotation* is a v1 ``note`` annotation (checks type or its
    ``kind`` compatibility alias, matching how the store itself resolves type).
    """
    ann_type = annotation.get("type") or annotation.get("kind")
    return ann_type == NOTE_TYPE


def build_note_annotation(
    *,
    x: float,
    y: float,
    text: str = "",
    color: Optional[str] = None,
    font_size: Optional[float] = None,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    z: Optional[float] = None,
    locked: bool = False,
    annotation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a v1 ``note`` annotation dict for the ``annotation_created`` op.

    Mirrors ``createAnnotation({type: 'note', ...})``: geometry, position and
    size all carry the same x/y/w/h so any code reading either projection
    stays consistent. ``annotation_id`` is left out when not given, so the
    store assigns one (``SessionStore.apply_state_op`` mints a
    ``secrets.token_hex(8)`` id for a create with no id) — the caller reads the
    assigned id off the op result instead of inventing one.

    ``rotation``/``z``/``locked`` default the same way ``build_annotation``
    does for the generic types: an omitted ``rotation``/``z`` becomes ``0``,
    an omitted ``locked`` becomes ``False``.
    """
    size = {
        "w": w if w is not None else DEFAULT_NOTE_SIZE["w"],
        "h": h if h is not None else DEFAULT_NOTE_SIZE["h"],
    }
    annotation: Dict[str, Any] = {
        "type": NOTE_TYPE,
        "kind": NOTE_TYPE,
        "position": {"x": x, "y": y},
        "geometry": {
            "x": x,
            "y": y,
            "w": size["w"],
            "h": size["h"],
            "rotation": rotation if rotation is not None else 0,
        },
        "size": size,
        "text": text or "",
        "z": z if z is not None else 0,
        "locked": bool(locked),
    }
    if annotation_id is not None:
        annotation["id"] = annotation_id
    if color is not None:
        annotation["color"] = color
        annotation["style"] = {"color": color}
    if font_size is not None:
        annotation["fontSize"] = font_size
    return annotation


def build_note_patch(
    existing: Dict[str, Any],
    *,
    text: Optional[str] = None,
    color: Optional[str] = None,
    font_size: Optional[float] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    z: Optional[float] = None,
    locked: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a partial ``annotation_updated`` patch for an existing note.

    ``SessionStore.apply_state_op`` merges a patch onto the stored annotation
    with a shallow ``dict.update`` — a key that is *present* in the patch wholly
    replaces the stored value, it does not deep-merge. So a position-only move
    still has to carry the note's current w/h inside ``geometry`` (and a
    size-only resize its current x/y), or the untouched half would be dropped
    rather than preserved. Only fields present here as non-``None`` arguments
    are touched; the rest keep the value already in ``existing``.

    ``rotation``/``z``/``locked`` follow ``build_annotation_patch``'s
    convention for the same fields: ``rotation`` is folded into ``geometry``
    alongside any position/size change (so it survives the same shallow
    merge), ``z``/``locked`` are set directly on the patch when given.
    """
    patch: Dict[str, Any] = {
        "id": existing["id"],
        "type": NOTE_TYPE,
        "kind": NOTE_TYPE,
    }
    if text is not None:
        patch["text"] = text
    if color is not None:
        patch["color"] = color
        patch["style"] = {**(existing.get("style") or {}), "color": color}
    if font_size is not None:
        patch["fontSize"] = font_size

    geometry = dict(existing.get("geometry") or {})
    size = dict(existing.get("size") or DEFAULT_NOTE_SIZE)
    position = dict(
        existing.get("position")
        or {"x": geometry.get("x", 0), "y": geometry.get("y", 0)}
    )

    moved = x is not None or y is not None
    resized = w is not None or h is not None
    rotated = rotation is not None
    if moved:
        position["x"] = x if x is not None else position.get("x", 0)
        position["y"] = y if y is not None else position.get("y", 0)
        geometry["x"] = position["x"]
        geometry["y"] = position["y"]
        patch["position"] = position
    if resized:
        size["w"] = w if w is not None else size.get("w", DEFAULT_NOTE_SIZE["w"])
        size["h"] = h if h is not None else size.get("h", DEFAULT_NOTE_SIZE["h"])
        geometry["w"] = size["w"]
        geometry["h"] = size["h"]
        patch["size"] = size
    if rotated:
        geometry["rotation"] = rotation
    if moved or resized or rotated:
        patch["geometry"] = geometry
    if z is not None:
        patch["z"] = z
    if locked is not None:
        patch["locked"] = bool(locked)
    return patch


def project_note(annotation: Dict[str, Any]) -> Dict[str, Any]:
    """Project a stored note annotation into the MCP-facing read shape."""
    geometry = annotation.get("geometry") or {}
    position = annotation.get("position") or {
        "x": geometry.get("x", 0),
        "y": geometry.get("y", 0),
    }
    size = annotation.get("size") or {
        "w": geometry.get("w", DEFAULT_NOTE_SIZE["w"]),
        "h": geometry.get("h", DEFAULT_NOTE_SIZE["h"]),
    }
    return {
        "id": annotation.get("id"),
        "text": annotation.get("text") or "",
        "x": position.get("x", 0),
        "y": position.get("y", 0),
        "w": size.get("w", DEFAULT_NOTE_SIZE["w"]),
        "h": size.get("h", DEFAULT_NOTE_SIZE["h"]),
        "color": annotation.get("color"),
        "font_size": annotation.get("fontSize"),
        "rotation": geometry.get("rotation", 0),
        "z": annotation.get("z", 0),
        "locked": bool(annotation.get("locked", False)),
        # Read-only bookkeeping (dec-annotation-field-patches-and-conflicts):
        # bumped on every applied write. Pass straight back as
        # `update_sticky_note`'s `base_version` to opt into field-level
        # conflict checking on a later write, same as `project_annotation`'s
        # equivalent field. Defaults to 1 for an annotation stored before
        # this field existed.
        "version": annotation.get("version", 1),
        "created_at": annotation.get("created_at"),
        "updated_at": annotation.get("updated_at"),
        "created_by": annotation.get("created_by"),
        "updated_by": annotation.get("updated_by"),
    }


# ==================== Group (node-membership box) helpers ====================


def is_group(annotation: Dict[str, Any]) -> bool:
    """Whether *annotation* is a v1 ``group`` annotation (checks type or its
    ``kind`` compatibility alias, matching how the store itself resolves type).
    """
    ann_type = annotation.get("type") or annotation.get("kind")
    return ann_type == GROUP_TYPE


def build_group_annotation(
    *,
    x: float,
    y: float,
    w: Optional[float] = None,
    h: Optional[float] = None,
    label: str = "",
    description: str = "",
    color: Optional[str] = None,
    member_node_ids: Optional[List[str]] = None,
    z: Optional[float] = None,
    locked: bool = False,
    annotation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a v1 ``group`` annotation dict for the ``annotation_created`` op.

    Mirrors ``build_note_annotation``'s shape and upsert behaviour (passing
    an existing ``annotation_id`` replaces that group's fields), with one
    deliberate difference: ``member_node_ids`` is included in the built dict
    only when the caller passes it explicitly, instead of always defaulting
    to ``[]`` the way ``build_note_annotation`` always sets ``text``.
    ``SessionStore`` applies an upsert with a shallow ``dict.update``, so a
    key this function omits is left untouched on the stored annotation
    rather than reset. Membership is meant to be managed through
    ``update_group_members`` (the ``group_membership_changed`` op) once a
    group exists; if re-creating a group by id to change its label or color
    also silently wiped out membership set through that other tool whenever
    the caller did not resend the current list, the two tools would fight
    each other. A brand-new group with no ``member_node_ids`` given is
    simply created empty — the canvas and ``project_annotation`` both treat
    an absent list the same as an empty one.

    ``ValueError`` is raised for a non-list-of-strings ``member_node_ids``,
    matching how the generic builders report a malformed payload as
    ``invalid_content`` at the MCP tool layer.
    """
    if member_node_ids is not None and (
        not isinstance(member_node_ids, list)
        or not all(isinstance(m, str) for m in member_node_ids)
    ):
        raise ValueError("member_node_ids must be a list of strings")
    size = {
        "w": w if w is not None else DEFAULT_GROUP_SIZE["w"],
        "h": h if h is not None else DEFAULT_GROUP_SIZE["h"],
    }
    annotation: Dict[str, Any] = {
        "type": GROUP_TYPE,
        "kind": GROUP_TYPE,
        "position": {"x": x, "y": y},
        "geometry": {"x": x, "y": y, "w": size["w"], "h": size["h"], "rotation": 0},
        "size": size,
        "label": label or "",
        "description": description or "",
        "z": z if z is not None else 0,
        "locked": bool(locked),
    }
    if annotation_id is not None:
        annotation["id"] = annotation_id
    if color is not None:
        annotation["color"] = color
        annotation["style"] = {"color": color}
    if member_node_ids is not None:
        annotation["member_node_ids"] = list(member_node_ids)
    return annotation


# ==================== Generic (non-note, non-group) type helpers ====================


def resolve_annotation_type_alias(raw_type: Any) -> Optional[str]:
    """Resolve the legacy ``arrow`` alias to its canonical type, if applicable.

    Returns ``None`` for anything that is not a string, leaving membership
    checks to the caller.
    """
    if not isinstance(raw_type, str):
        return None
    return LEGACY_ANNOTATION_ALIASES.get(raw_type, raw_type)


def normalize_generic_type(raw_type: Any) -> Optional[str]:
    """Resolve *raw_type* and return it only if it is one of the v1 types the
    generic annotation tool set manages (excludes ``note`` and ``group``,
    which are out of scope here — see module docstring). ``None`` otherwise.
    """
    resolved = resolve_annotation_type_alias(raw_type)
    return resolved if resolved in GENERIC_ANNOTATION_TYPES else None


def annotation_type_of(annotation: Dict[str, Any]) -> Optional[str]:
    """The canonical type of a stored annotation dict (``type`` or its
    ``kind`` fallback, with the legacy ``arrow`` alias resolved).
    """
    raw = annotation.get("type") or annotation.get("kind")
    return resolve_annotation_type_alias(raw)


def is_generic_annotation(annotation: Dict[str, Any]) -> bool:
    """Whether *annotation* is one of the types the generic tool set manages
    (i.e. not a ``note`` and not a ``group``)."""
    return annotation_type_of(annotation) in GENERIC_ANNOTATION_TYPES


def is_embedded_image_url(url: Any) -> bool:
    """Whether *url* is an embedded image data URI ingest is allowed to store."""
    return isinstance(url, str) and url.startswith(EMBEDDED_IMAGE_URL_PREFIXES)


def image_annotation_error(
    annotation: Dict[str, Any], existing: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Why *annotation* may not be persisted as an ``image``, or ``None``.

    Enforces docs/ANNOTATION_CONTRACT.md's "Image ingest enforcement" rule
    for the writes ``SessionStore.apply_state_op`` submits to it:
    an ``image`` annotation's pixel content must be the embedded result of
    server-side ingest (``image_ingest.py``), never a remote URL that the
    annotation would then depend on staying reachable — and never a
    ``file:``/``javascript:`` style URL either. Without this, the generic
    ``create_annotation``/``update_annotation`` tools and any client posting a
    raw ``annotation_created`` op could store an arbitrary unvalidated remote
    URL, going around the validation, budgets and SSRF checks
    ``create_image_annotation`` performs.

    *existing* is the annotation already stored under this id, when there is
    one. A write whose ``image.url`` is byte-identical to the stored one is
    allowed even if that URL is not embedded: it introduces no new reference,
    and refusing it would strand annotations persisted before this rule
    existed — the browser echoes the *whole* annotation, image payload
    included, on every move/resize/lock (``sessionSyncClient.js``), so a
    blanket refusal would make such an annotation permanently unmovable. Only
    a *new* non-embedded URL is refused — and a duplicate, which lands on a
    fresh id with no *existing* to match, counts as new.

    The second exemption is not here at all: ``apply_state_op`` skips this
    check entirely for an undo replaying its stored inverse op
    (``trusted_replay``), which restores a copy of the session's own earlier
    state rather than accepting caller input. Without it, deleting an
    annotation persisted before this rule existed would be irreversible,
    since after the delete there is no *existing* left to match against.

    An annotation of another type, or an ``image`` patch that omits the pixel
    payload entirely, is unaffected.
    """
    if annotation_type_of(annotation) != IMAGE_TYPE:
        return None
    if "image" not in annotation:
        return None
    image = annotation.get("image")
    if image is None:
        return None
    if not isinstance(image, dict):
        return "image annotation 'image' payload must be an object"
    url = image.get("url")
    if url is None:
        # No pixel content to validate. A browser echoing an annotation back
        # on a move serialises `image` as `{}` when the payload is missing
        # (`sessionAnnotations.js`), and rejecting that would wedge the whole
        # op batch — including every unrelated op in it — over an annotation
        # that references nothing.
        return None
    if is_embedded_image_url(url):
        return None
    if isinstance(existing, dict):
        stored = existing.get("image")
        if isinstance(stored, dict) and stored.get("url") == url:
            return None
    return (
        "image annotation content must be an embedded image produced by "
        "server-side ingest (a data:image/webp;base64 URI); a remote or "
        "unvalidated URL is not accepted — create or replace the image with "
        "the image ingest path instead"
    )


def iter_saved_view_annotations(metadata: Dict[str, Any]):
    """Yield every annotation-shaped dict embedded in SavedView/VisualizationView
    node metadata: the v1 annotation document's ``annotations`` list and the
    legacy ``annotations`` list the frontend keeps in sync alongside it
    (``frontend/web/src/utils/sessionAnnotations.js``, design 3.1).

    A SavedView node's annotation content is ordinary node metadata, written
    through the generic ``add_nodes``/``update_node`` tools rather than
    through ``SessionStore.apply_state_op`` — so unlike a live session op it
    is not opaque to the caller here, but it uses the identical v1 annotation
    shape and must be checked against the identical rule
    (``saved_view_annotation_error`` below).
    """
    if not isinstance(metadata, dict):
        return
    document = metadata.get("annotation_document")
    if isinstance(document, dict):
        for annotation in document.get("annotations", []) or []:
            if isinstance(annotation, dict):
                yield annotation
    legacy = metadata.get("annotations")
    if isinstance(legacy, list):
        for annotation in legacy:
            if isinstance(annotation, dict):
                yield annotation


def saved_view_annotation_error(metadata: Dict[str, Any]) -> Optional[str]:
    """Why *metadata* may not be persisted on a SavedView/VisualizationView
    node, or ``None`` if every embedded annotation is fine.

    Applies ``image_annotation_error`` — the same rule
    ``SessionStore.apply_state_op`` enforces for live annotation ops — to
    every annotation reachable from saved-view metadata (see
    ``iter_saved_view_annotations``). Unlike a live op, a saved-view write is
    not an incremental patch onto previously-validated state, so there is no
    legitimate *existing* annotation to exempt a re-sent URL against here:
    every image annotation must already be an embedded data URI, with no
    byte-identical-URL exemption. Callers gate this call to nodes of the
    right type themselves — this module has no notion of node types.

    ``reference_annotation_error`` is applied over the same annotations for
    the same reason: a saved view is opened by a browser that renders a
    reference's target into an activatable link, so a view carrying an
    unsafe-scheme reference would hand a viewer a `javascript:` link merely
    by being opened. A saved-view annotation is a whole stored object rather
    than a patch, so it is held to the complete-payload rule.
    """
    for annotation in iter_saved_view_annotations(metadata):
        error = image_annotation_error(annotation)
        if error:
            return error
        error = reference_annotation_error(annotation, require_complete=True)
        if error:
            return error
    return None


def _sanitize_saved_view_annotation(annotation: Dict[str, Any]) -> Dict[str, Any]:
    if annotation_type_of(annotation) == REFERENCE_TYPE:
        # Same defense-in-depth role as the image branch below, for the same
        # renderer reason: a reference's `target` becomes an activatable link,
        # so a view that reached storage before the gate existed (or by some
        # path it does not cover) must not hand a viewer an unsafe link just
        # by being opened. The target is dropped rather than the whole
        # annotation: the tile still renders, in its broken-target state,
        # which is the honest thing to show for a link that cannot be
        # followed.
        if annotation.get("target_kind") != "url":
            return annotation
        if reference_url_error(annotation.get("target")) is None:
            return annotation
        sanitized = dict(annotation)
        sanitized.pop("target", None)
        return sanitized
    if annotation_type_of(annotation) != IMAGE_TYPE:
        return annotation
    image = annotation.get("image")
    if not isinstance(image, dict):
        return annotation
    url = image.get("url")
    if url is None or is_embedded_image_url(url):
        return annotation
    sanitized_image = dict(image)
    sanitized_image.pop("url", None)
    return {**annotation, "image": sanitized_image}


def sanitize_saved_view_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of SavedView/VisualizationView *metadata* with every
    non-embedded image annotation URL, and every unsafe-scheme reference
    target, stripped.

    Defense in depth alongside ``saved_view_annotation_error``: a view saved
    before that check existed (or whose metadata reached storage by some
    other path) must still not make a viewer fetch a remote host merely by
    opening it — ``GenericAnnotationNode`` renders ``content.image.url``
    straight into an ``<img src>``. Never mutates *metadata* itself (callers
    read this directly off live ``Node.metadata``); returns a shallow copy
    with only the ``annotation_document``/``annotations`` keys replaced when
    something was actually stripped, leaving every other field — and every
    non-image annotation — untouched (and byte-for-byte identical, so callers
    that need to detect "did this change" can compare by equality).
    """
    if not isinstance(metadata, dict):
        return metadata
    sanitized = dict(metadata)
    document = metadata.get("annotation_document")
    if isinstance(document, dict) and isinstance(document.get("annotations"), list):
        new_annotations = [
            _sanitize_saved_view_annotation(a) if isinstance(a, dict) else a
            for a in document["annotations"]
        ]
        if new_annotations != document["annotations"]:
            sanitized["annotation_document"] = {
                **document,
                "annotations": new_annotations,
            }
    legacy = metadata.get("annotations")
    if isinstance(legacy, list):
        new_legacy = [
            _sanitize_saved_view_annotation(a) if isinstance(a, dict) else a
            for a in legacy
        ]
        if new_legacy != legacy:
            sanitized["annotations"] = new_legacy
    return sanitized


def _apply_content(
    target: Dict[str, Any],
    content: Optional[Dict[str, Any]],
    *,
    ann_type: Optional[str] = None,
    existing: Optional[Dict[str, Any]] = None,
) -> None:
    if not content:
        return
    reserved = _RESERVED_ANNOTATION_KEYS & content.keys()
    if reserved:
        raise ValueError(
            f"content must not set reserved field(s) {sorted(reserved)}; "
            "those are managed by their own arguments"
        )
    content_error = _validate_generic_content(ann_type, content, existing)
    if content_error:
        raise ValueError(content_error)
    target.update(content)


def build_annotation(
    *,
    type: str,
    x: float,
    y: float,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    content: Optional[Dict[str, Any]] = None,
    style: Optional[Dict[str, Any]] = None,
    z: Optional[float] = None,
    locked: bool = False,
    annotation_id: Optional[str] = None,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a v1 annotation dict of *type* for the ``annotation_created`` op.

    Builds the common envelope (``geometry``/``position``/``style``/``z``/
    ``locked``) shared by every v1 type, mirroring ``createAnnotation()``.
    Unlike ``build_note_annotation`` this does not model each type's payload
    shape — that differs too much across line/label/shape/icon/
    vote_dot/image/freehand for one generic builder to hand-build — so *content*
    carries it verbatim and is merged onto the annotation as-is. The
    frontend's ``createAnnotation()`` re-normalizes defensively on load
    either way, so a caller-supplied payload that is merely incomplete (e.g.
    a ``line`` missing ``to``) does not corrupt the document.

    *type* must already be resolved to one of ``GENERIC_ANNOTATION_TYPES``
    (see ``normalize_generic_type``); this function does not itself validate
    it, matching ``build_note_annotation``'s division of labor with its
    callers.

    A ``heatmap`` given no size gets a ``HEATMAP_DEFAULT_DIAMETER`` circle
    (and one given only w or h a circle of that diameter), since a 0x0 box
    draws nothing. Its intensity is not defaulted here: an upsert-replace
    that omits ``content`` keeps the stored intensity under the store's
    shallow merge, and a default written here would silently reset it. The
    MCP tool fills the default for a fresh create instead.

    Pass *existing* when this build replaces an annotation already stored
    under the same id (an upsert). Without it a `reference` upsert that
    re-sends only part of its payload is refused as incomplete, even though
    the store's shallow merge would have kept the rest — the defect round 2 of
    the review loop turned up while testing round 1's own fix.
    """
    if type == HEATMAP_TYPE:
        if w is None and h is None:
            w = h = HEATMAP_DEFAULT_DIAMETER
        elif w is None:
            w = h
        elif h is None:
            h = w
    elif type == REFERENCE_TYPE:
        # A tile with no box draws nothing, the same reason heatmap defaults
        # its diameter above. Each side defaults independently so a caller
        # giving only a width keeps the default height.
        if w is None:
            w = DEFAULT_REFERENCE_SIZE["w"]
        if h is None:
            h = DEFAULT_REFERENCE_SIZE["h"]
    geometry = {
        "x": x,
        "y": y,
        "w": w if w is not None else 0,
        "h": h if h is not None else 0,
        "rotation": rotation if rotation is not None else 0,
    }
    annotation: Dict[str, Any] = {
        "type": type,
        "kind": type,
        "position": {"x": x, "y": y},
        "geometry": geometry,
        "z": z if z is not None else default_annotation_z(type),
        "locked": bool(locked),
    }
    if w is not None or h is not None:
        annotation["size"] = {"w": geometry["w"], "h": geometry["h"]}
    if style is not None:
        annotation["style"] = dict(style)
    # *existing* is the annotation already stored under this id, when this
    # build is an UPSERT rather than a fresh create. It matters only for a
    # cross-field type like `reference`: the store applies an upsert with a
    # shallow merge, so re-sending a subset of the payload is a legitimate
    # edit, and validating that subset as if it stood alone refuses it for
    # fields the stored annotation already carries. Omitted — a genuine fresh
    # create — the payload is required to be complete.
    _apply_content(annotation, content, ann_type=type, existing=existing)
    if annotation_id is not None:
        annotation["id"] = annotation_id
    return annotation


def translate_line_endpoints(
    existing: Dict[str, Any], dx: float, dy: float
) -> Dict[str, Any]:
    """Translate a line annotation's explicit endpoint coordinates by (dx, dy).

    A line's shape lives in its ``from``/``to`` content fields, outside the
    common ``geometry``/``position`` envelope those fields shadow (the
    envelope's x/y only tracks the anchor, `from` in practice). Moving or
    duplicating a line must translate both ends by the same delta or the
    line stretches/reshapes instead of sliding. Returns the fields to merge
    onto the target patch/copy; empty for annotations without explicit
    endpoint coordinates (every non-``line`` type).
    """
    translated: Dict[str, Any] = {}
    for key in ("from", "to"):
        point = existing.get(key)
        if (
            isinstance(point, dict)
            and isinstance(point.get("x"), (int, float))
            and isinstance(point.get("y"), (int, float))
        ):
            translated[key] = {**point, "x": point["x"] + dx, "y": point["y"] + dy}
    return translated


def translate_freehand_points(
    existing: Dict[str, Any], dx: float, dy: float
) -> Dict[str, Any]:
    """Translate a freehand annotation's sampled points by (dx, dy).

    A freehand stroke's shape lives in its ``points`` content field as
    absolute model-space coordinates, outside the common ``geometry``/
    ``position`` envelope — same reason as ``translate_line_endpoints``:
    moving the annotation must slide every sampled point by the same delta,
    or the stroke reshapes instead of sliding. Returns the fields to merge
    onto the target patch/copy; empty for annotations without a ``points``
    list (every non-``freehand`` type).
    """
    points = existing.get("points")
    if not isinstance(points, list) or not points:
        return {}
    translated = []
    changed = False
    for point in points:
        if (
            isinstance(point, dict)
            and isinstance(point.get("x"), (int, float))
            and isinstance(point.get("y"), (int, float))
        ):
            translated.append({**point, "x": point["x"] + dx, "y": point["y"] + dy})
            changed = True
        else:
            translated.append(point)
    return {"points": translated} if changed else {}


def build_annotation_patch(
    existing: Dict[str, Any],
    *,
    x: Optional[float] = None,
    y: Optional[float] = None,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    content: Optional[Dict[str, Any]] = None,
    style: Optional[Dict[str, Any]] = None,
    z: Optional[float] = None,
    locked: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a partial ``annotation_updated`` patch for an existing annotation.

    Same shallow-merge caveat as ``build_note_patch``: only fields present
    here as non-``None`` arguments are touched; a position-only move still
    carries the existing w/h inside ``geometry`` (and vice versa) so the
    untouched half is not dropped by the store's ``dict.update`` merge.
    """
    ann_type = annotation_type_of(existing) or existing.get("type")
    patch: Dict[str, Any] = {"id": existing["id"], "type": ann_type, "kind": ann_type}

    geometry = dict(existing.get("geometry") or {})
    size = dict(
        existing.get("size") or {"w": geometry.get("w", 0), "h": geometry.get("h", 0)}
    )
    position = dict(
        existing.get("position")
        or {"x": geometry.get("x", 0), "y": geometry.get("y", 0)}
    )

    moved = x is not None or y is not None
    resized = w is not None or h is not None
    rotated = rotation is not None
    if moved:
        original_x = position.get("x", 0)
        original_y = position.get("y", 0)
        position["x"] = x if x is not None else original_x
        position["y"] = y if y is not None else original_y
        geometry["x"] = position["x"]
        geometry["y"] = position["y"]
        patch["position"] = position
        dx = position["x"] - original_x
        dy = position["y"] - original_y
        if dx or dy:
            patch.update(translate_line_endpoints(existing, dx, dy))
            patch.update(translate_freehand_points(existing, dx, dy))
    if resized:
        size["w"] = w if w is not None else size.get("w", 0)
        size["h"] = h if h is not None else size.get("h", 0)
        geometry["w"] = size["w"]
        geometry["h"] = size["h"]
        patch["size"] = size
    if rotated:
        geometry["rotation"] = rotation
    if moved or resized or rotated:
        patch["geometry"] = geometry
    if style is not None:
        patch["style"] = dict(style)
    if z is not None:
        patch["z"] = z
    if locked is not None:
        patch["locked"] = bool(locked)
    _apply_content(patch, content, ann_type=ann_type, existing=existing)
    return patch


def project_annotation(annotation: Dict[str, Any]) -> Dict[str, Any]:
    """Project a stored annotation of any type into the MCP-facing read shape.

    ``content`` holds every field outside the common envelope — the
    type-specific payload (a line's ``from``/``to``, a label's ``text``,
    ...) — verbatim, mirroring how ``build_annotation``'s *content* argument
    writes it.
    """
    geometry = annotation.get("geometry") or {}
    position = annotation.get("position") or {
        "x": geometry.get("x", 0),
        "y": geometry.get("y", 0),
    }
    size = annotation.get("size") or {}
    content = {
        key: value
        for key, value in annotation.items()
        if key not in _RESERVED_ANNOTATION_KEYS
    }
    return {
        "id": annotation.get("id"),
        "type": annotation_type_of(annotation),
        "x": position.get("x", 0),
        "y": position.get("y", 0),
        "w": geometry.get("w", size.get("w", 0)),
        "h": geometry.get("h", size.get("h", 0)),
        "rotation": geometry.get("rotation", 0),
        "style": annotation.get("style") or {},
        "z": annotation.get("z", 0),
        "locked": bool(annotation.get("locked", False)),
        "content": content,
        # Read-only: pass straight back as update_annotation's base_version
        # to opt into field-level conflict checking on a later write
        # (dec-annotation-field-patches-and-conflicts). Defaults to 1 for an
        # annotation stored before this field existed.
        "version": annotation.get("version", 1),
        "created_at": annotation.get("created_at"),
        "updated_at": annotation.get("updated_at"),
        "created_by": annotation.get("created_by"),
        "updated_by": annotation.get("updated_by"),
    }
