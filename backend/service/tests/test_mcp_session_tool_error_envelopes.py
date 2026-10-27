"""The error envelopes the session and annotation MCP tools return.

An agent branches on ``error`` (and retries ``busy``/``rate_limited``), so the
envelope each tool returns for each failure the session manager can raise is
the tools' contract: these tests pin it exactly. Every manager failure is
injected by replacing the one ``SessionManager`` method the tool writes
through, so each case reaches the tool's own ``except`` clause; the
manager's own behaviour is covered in ``backend/core/tests/``.

Exceptions whose message is not part of the envelope carry a sentinel in it:
an envelope that does not echo the exception by design (``OpError``, the
lease conflict and the image budget do) must not leak it.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage, layer_store
from backend.core.session_manager import (
    AnnotationNotFound,
    AnnotationRecentlyDeleted,
    ImageBudgetExceeded,
    LayoutBusy,
    LeaseConflict,
    OpBatchTooLarge,
    RateLimited,
    RevisionConflict,
    SessionLimitReached,
    SessionManager,
    SessionNotFound,
)
from backend.core.session_annotations import project_annotation, project_note
from backend.core.session_store import (
    AnnotationFieldConflict,
    InMemorySessionPersistenceBackend,
    OpError,
    SessionStore,
)
from backend.core.write_modes import WRITE_MODE_IRREVERSIBLE, WriteModeRefused
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, register_mcp_tools

SENTINEL = "internal-detail-7f3a"
UNKNOWN_SESSION = "9999-9999-9999-9999"

BUSY = {
    "success": False,
    "error": "busy",
    "message": "Another change is being applied to this session; retry.",
}
RATE_LIMITED = {
    "success": False,
    "error": "rate_limited",
    "message": "Too many session writes; slow down and retry.",
}


def _session_gone(session_id):
    return {
        "success": False,
        "error": (
            f"Session '{session_id}' not found. "
            "This tool acts on a session's stored state, which exists "
            "once create_visualization_session created it or a browser "
            "made its first change to it."
        ),
    }


def _too_large(what):
    return {
        "success": False,
        "error": "too_large",
        "message": f"{what} payload too large for one write.",
    }


def _not_found(message):
    return {"success": False, "error": "not_found", "message": message}


@pytest.fixture
def seeded(tmp_path):
    """Registered tools over an in-memory manager, plus one session holding a
    sticky note, a generic (label) annotation and a group."""
    service = GraphService(GraphStorage(json_path=os.path.join(tmp_path, "g.json")))
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mock_mcp, service, session_manager=manager)

    sid = manager.create_session().id
    note = tools["create_sticky_note"](session_id=sid, x=0, y=0, text="n")
    label = tools["create_annotation"](session_id=sid, type="label", x=1, y=1)
    group = tools["create_group_annotation"](session_id=sid, x=2, y=2, label="g")
    ids = {
        "sid": sid,
        "note": note["note"]["id"],
        "label": label["annotation"]["id"],
        "group": group["group"]["id"],
    }
    return tools, manager, ids


# tool -> (manager method it writes through, kwargs beyond session_id, the
# expected envelope per injectable failure). A failure a tool does not catch
# is absent from its row. Keys of the kwargs/messages refer to ``ids``.
ANNOTATION_TOOLS = {
    "create_sticky_note": (
        "upsert_annotation",
        lambda ids: {"x": 0, "y": 0, "text": "t"},
        {"too_large": _too_large("Note")},
    ),
    "update_sticky_note": (
        "update_annotation",
        lambda ids: {"annotation_id": ids["note"], "text": "t"},
        {
            "too_large": _too_large("Note"),
            "not_found": lambda ids: _not_found(
                f"No sticky note with id {ids['note']!r} in this session."
            ),
        },
    ),
    "delete_sticky_note": (
        "delete_annotation",
        lambda ids: {"annotation_id": ids["note"]},
        {
            "not_found": lambda ids: _not_found(
                f"No sticky note with id {ids['note']!r} in this session."
            ),
        },
    ),
    "create_annotation": (
        "upsert_annotation",
        lambda ids: {"type": "label", "x": 0, "y": 0},
        {"too_large": _too_large("Annotation"), "image_budget": True},
    ),
    "update_annotation": (
        "update_annotation",
        lambda ids: {"annotation_id": ids["label"], "x": 5},
        {
            "too_large": _too_large("Annotation"),
            "not_found": lambda ids: _not_found(
                f"No annotation with id {ids['label']!r} in this session."
            ),
        },
    ),
    "reorder_annotation": (
        "update_annotation",
        lambda ids: {"annotation_id": ids["label"], "z": 3},
        {
            "too_large": _too_large("Annotation"),
            "not_found": lambda ids: _not_found(
                f"No annotation with id {ids['label']!r} in this session."
            ),
        },
    ),
    "set_annotation_lock": (
        "update_annotation",
        lambda ids: {"annotation_id": ids["label"], "locked": True},
        {
            "too_large": _too_large("Annotation"),
            "not_found": lambda ids: _not_found(
                f"No annotation with id {ids['label']!r} in this session."
            ),
        },
    ),
    "duplicate_annotation": (
        "upsert_annotation",
        lambda ids: {"annotation_id": ids["label"], "dx": 1},
        {"too_large": _too_large("Annotation"), "image_budget": True},
    ),
    "delete_annotation": (
        "delete_annotation",
        lambda ids: {"annotation_id": ids["label"]},
        {
            "not_found": lambda ids: _not_found(
                f"No annotation with id {ids['label']!r} in this session."
            ),
        },
    ),
    "create_group_annotation": (
        "upsert_annotation",
        lambda ids: {"x": 0, "y": 0},
        {"too_large": _too_large("Group")},
    ),
    "update_group_members": (
        "set_group_members",
        lambda ids: {"group_id": ids["group"], "add_member_node_ids": ["n1"]},
        {"too_large": _too_large("Membership")},
    ),
    "delete_group_annotation": (
        "delete_annotation",
        lambda ids: {"group_id": ids["group"]},
        {
            "not_found": lambda ids: _not_found(
                f"No group annotation with id {ids['group']!r} in this session."
            ),
        },
    ),
}

# The tools that create, and the kwarg each takes the caller's id for the new
# annotation in: only a caller-chosen id can collide with a recent delete.
CREATING_TOOLS = {
    "create_sticky_note": "annotation_id",
    "create_annotation": "annotation_id",
    "duplicate_annotation": "new_annotation_id",
    "create_group_annotation": "annotation_id",
}


# The tools that only act on an existing annotation, so they look the session
# up before writing (the creators leave a missing session to the write).
TARGETED_TOOL_NAMES = sorted(
    set(ANNOTATION_TOOLS)
    - {"create_sticky_note", "create_annotation"}
    - {"create_group_annotation"}
)


def _revision_conflict(tool):
    list_tool = "list_sticky_notes" if "sticky" in tool else "list_annotations"
    return {
        "success": False,
        "error": "revision_conflict",
        "message": (
            "The session changed since you read it; re-read "
            f"{list_tool} and retry with the current revision."
        ),
        "expected_revision": 3,
        "current_revision": 4,
    }


_LEASE = LeaseConflict("ann-held", "browser-1")


def _annotation_cases():
    """(tool, failure, exception, expected-envelope builder) per caught failure."""
    for tool, (_method, _kwargs, row) in ANNOTATION_TOOLS.items():
        yield (
            tool,
            "revision_conflict",
            RevisionConflict(3, 4),
            lambda ids, t=tool: _revision_conflict(t),
        )
        yield (
            tool,
            "lease_conflict",
            _LEASE,
            lambda ids: {
                "success": False,
                "error": "lease_conflict",
                "message": str(_LEASE),
                "annotation_id": "ann-held",
                "held_by": "browser-1",
            },
        )
        yield tool, "busy", LayoutBusy(SENTINEL), lambda ids: BUSY
        yield tool, "rate_limited", RateLimited(SENTINEL), lambda ids: RATE_LIMITED
        yield (
            tool,
            "session_gone",
            SessionNotFound(SENTINEL),
            lambda ids: _session_gone(ids["sid"]),
        )
        yield (
            tool,
            "op_error",
            OpError(f"bad op {SENTINEL}"),
            lambda ids: {"success": False, "error": f"bad op {SENTINEL}"},
        )
        if "too_large" in row:
            expected = row["too_large"]
            yield (
                tool,
                "too_large",
                OpBatchTooLarge(SENTINEL),
                lambda ids, e=expected: e,
            )
        if "not_found" in row:
            yield tool, "not_found", AnnotationNotFound(SENTINEL), row["not_found"]
        if row.get("image_budget"):
            yield (
                tool,
                "image_budget",
                ImageBudgetExceeded(f"over budget {SENTINEL}"),
                lambda ids: {
                    "success": False,
                    "error": "too_large",
                    "message": f"over budget {SENTINEL}",
                },
            )


@pytest.mark.parametrize(
    "tool,failure,exc,expected",
    [pytest.param(*case, id=f"{case[0]}-{case[1]}") for case in _annotation_cases()],
)
def test_annotation_tool_maps_each_manager_failure_to_its_envelope(
    seeded, monkeypatch, tool, failure, exc, expected
):
    tools, manager, ids = seeded
    method, kwargs, _row = ANNOTATION_TOOLS[tool]

    def fail(*args, **kw):
        raise exc

    monkeypatch.setattr(manager, method, fail)

    result = tools[tool](session_id=ids["sid"], **kwargs(ids))

    assert result == expected(ids)
    if failure not in ("op_error", "image_budget"):
        assert SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "tool,target,key,project",
    [
        ("update_sticky_note", "note", "note", project_note),
        ("update_annotation", "label", "annotation", project_annotation),
    ],
)
def test_field_conflict_returns_the_server_copy_to_rebase_on(
    seeded, monkeypatch, tool, target, key, project
):
    tools, manager, ids = seeded
    server_copy = next(
        a
        for a in manager.get_session(ids["sid"]).state["annotations"]
        if a["id"] == ids[target]
    )
    conflict = AnnotationFieldConflict(ids[target], {"geometry": 2}, server_copy)
    monkeypatch.setattr(manager, "update_annotation", Mock(side_effect=conflict))

    result = tools[tool](session_id=ids["sid"], **ANNOTATION_TOOLS[tool][1](ids))

    assert result == {
        "success": False,
        "error": "field_conflict",
        "message": str(conflict),
        "conflicting_fields": {"geometry": 2},
        "server_version": conflict.server_version,
        key: project(server_copy),
    }


@pytest.mark.parametrize("tool", sorted(CREATING_TOOLS))
def test_creating_tool_refuses_a_recently_deleted_id(seeded, monkeypatch, tool):
    tools, manager, ids = seeded
    method, kwargs, _row = ANNOTATION_TOOLS[tool]

    def fail(*args, **kw):
        raise AnnotationRecentlyDeleted(SENTINEL)

    monkeypatch.setattr(manager, method, fail)

    result = tools[tool](
        session_id=ids["sid"], **{**kwargs(ids), CREATING_TOOLS[tool]: "ann-gone"}
    )

    assert result == {
        "success": False,
        "error": "annotation_recently_deleted",
        "message": (
            "Annotation id 'ann-gone' was just deleted by another "
            "collaborator; retry with a different id."
        ),
    }


@pytest.mark.parametrize("tool", TARGETED_TOOL_NAMES)
def test_tool_on_an_unknown_session_reports_it_gone_without_writing(
    seeded, monkeypatch, tool
):
    tools, manager, ids = seeded
    method, kwargs, _row = ANNOTATION_TOOLS[tool]
    monkeypatch.setattr(manager, method, Mock(side_effect=AssertionError("wrote")))

    result = tools[tool](session_id=UNKNOWN_SESSION, **kwargs(ids))

    assert result == _session_gone(UNKNOWN_SESSION)


# tool -> (kwarg naming the target, ids of the wrong kind it must refuse,
# expected pre-check envelope message for a given id).
TARGETED_TOOLS = {
    "update_sticky_note": (
        "annotation_id",
        ("label", "group"),
        lambda aid: f"No sticky note with id {aid!r} in this session.",
    ),
    "delete_sticky_note": (
        "annotation_id",
        ("label", "group"),
        lambda aid: f"No sticky note with id {aid!r} in this session.",
    ),
    "update_annotation": (
        "annotation_id",
        ("note", "group"),
        lambda aid: (
            f"No annotation with id {aid!r} in this session's "
            "generic annotation set (note/group ids are managed by their "
            "own tools)."
        ),
    ),
    **{
        tool: (
            "annotation_id",
            ("note", "group"),
            lambda aid: (
                f"No annotation with id {aid!r} in this session's "
                "generic annotation set."
            ),
        )
        for tool in (
            "reorder_annotation",
            "set_annotation_lock",
            "duplicate_annotation",
            "delete_annotation",
        )
    },
    **{
        tool: (
            "group_id",
            ("note", "label"),
            lambda aid: f"No group annotation with id {aid!r} in this session.",
        )
        for tool in ("update_group_members", "delete_group_annotation")
    },
}


def _target_cases():
    for tool, (_kwarg, wrong_kinds, _message) in TARGETED_TOOLS.items():
        for kind in (None, *wrong_kinds):
            yield pytest.param(tool, kind, id=f"{tool}-{kind or 'missing'}")


@pytest.mark.parametrize("tool,kind", list(_target_cases()))
def test_targeted_tool_refuses_a_missing_or_other_kind_id_as_not_found(
    seeded, monkeypatch, tool, kind
):
    tools, manager, ids = seeded
    method, kwargs, _row = ANNOTATION_TOOLS[tool]
    target_kwarg, _wrong, message = TARGETED_TOOLS[tool]
    target = ids[kind] if kind else "no-such-annotation"
    monkeypatch.setattr(manager, method, Mock(side_effect=AssertionError("wrote")))

    result = tools[tool](session_id=ids["sid"], **{**kwargs(ids), target_kwarg: target})

    assert result == _not_found(message(target))


SESSION_WRITE_METHODS = {
    "rename_visualization_session": ("rename_session_sync", {"name": "x"}),
    "set_session_write_mode": ("set_write_mode_sync", {"write_mode": "staged"}),
}


@pytest.mark.parametrize("tool", sorted({*ANNOTATION_TOOLS, *SESSION_WRITE_METHODS}))
def test_read_only_access_denies_the_write_before_it_reaches_the_manager(
    seeded, monkeypatch, tool
):
    tools, manager, ids = seeded
    if tool in SESSION_WRITE_METHODS:
        method, kwargs = SESSION_WRITE_METHODS[tool]
    else:
        method, build, _row = ANNOTATION_TOOLS[tool]
        kwargs = build(ids)
    monkeypatch.setattr(manager, method, Mock(side_effect=AssertionError("wrote")))
    monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

    result = tools[tool](session_id=ids["sid"], **kwargs)

    assert result["success"] is False
    assert result["error"] == "Graph access denied"
    assert result["error_code"] == "access_denied"
    assert result["authorization"]["action"] == "mutate"
    assert result["authorization"]["target"] == tool


@pytest.mark.parametrize("tool", sorted(ANNOTATION_TOOLS))
def test_tool_without_a_session_manager_says_so(tmp_path, tool):
    service = GraphService(GraphStorage(json_path=os.path.join(tmp_path, "g.json")))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mock_mcp, service, session_manager=None)
    ids = {"sid": UNKNOWN_SESSION, "note": "a", "label": "a", "group": "a"}

    result = tools[tool](session_id=UNKNOWN_SESSION, **ANNOTATION_TOOLS[tool][1](ids))

    assert result == {"success": False, "error": "Session manager not available"}


@pytest.mark.parametrize(
    "exc,expected",
    [
        pytest.param(LayoutBusy(SENTINEL), BUSY, id="busy"),
        pytest.param(RateLimited(SENTINEL), RATE_LIMITED, id="rate_limited"),
        pytest.param(
            SessionLimitReached(SENTINEL),
            {
                "success": False,
                "error": "too_many_sessions",
                "message": (
                    "The session limit has been reached; delete unused "
                    "sessions and retry."
                ),
            },
            id="too_many_sessions",
        ),
        pytest.param(
            SessionNotFound(SENTINEL),
            {"success": False, "error": f"Session '{UNKNOWN_SESSION}' not found."},
            id="session_gone",
        ),
        pytest.param(
            OpError("name too long"),
            {"success": False, "error": "name too long"},
            id="op_error",
        ),
    ],
)
def test_rename_session_maps_each_manager_failure_to_its_envelope(
    seeded, monkeypatch, exc, expected
):
    tools, manager, _ids = seeded

    def fail(*args, **kw):
        raise exc

    monkeypatch.setattr(manager, "rename_session_sync", fail)

    result = tools["rename_visualization_session"](session_id=UNKNOWN_SESSION, name="x")

    assert result == expected
    assert SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "exc,expected",
    [
        pytest.param(
            WriteModeRefused(WRITE_MODE_IRREVERSIBLE, "staged stays staged"),
            {
                "success": False,
                "error": WRITE_MODE_IRREVERSIBLE,
                "message": "staged stays staged",
            },
            id="refused",
        ),
        pytest.param(RateLimited(SENTINEL), RATE_LIMITED, id="rate_limited"),
        pytest.param(LayoutBusy(SENTINEL), BUSY, id="busy"),
        pytest.param(
            SessionNotFound(SENTINEL),
            {
                "success": False,
                "error": "session_not_found",
                "message": f"session {UNKNOWN_SESSION!r} not found",
            },
            id="session_gone",
        ),
        pytest.param(
            layer_store.StagedSessionRetired(SENTINEL),
            {
                "success": False,
                "error": "session_not_found",
                "message": f"session {UNKNOWN_SESSION!r} not found",
            },
            id="session_retired",
        ),
    ],
)
def test_set_session_write_mode_maps_each_manager_failure_to_its_envelope(
    seeded, monkeypatch, exc, expected
):
    tools, manager, _ids = seeded

    def fail(*args, **kw):
        raise exc

    monkeypatch.setattr(manager, "set_write_mode_sync", fail)

    result = tools["set_session_write_mode"](
        session_id=UNKNOWN_SESSION, write_mode="staged"
    )

    assert result == expected
    assert SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("rename_visualization_session", {"name": "x"}),
        ("set_session_write_mode", {"write_mode": "staged"}),
    ],
)
def test_session_tool_without_a_session_manager_says_so(tmp_path, tool, kwargs):
    service = GraphService(GraphStorage(json_path=os.path.join(tmp_path, "g.json")))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mock_mcp, service, session_manager=None)

    result = tools[tool](session_id=UNKNOWN_SESSION, **kwargs)

    assert result == {"success": False, "error": "Session manager not available"}
