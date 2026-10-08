"""
Scoring: turn a recorded run into per-dimension verdicts.

Every scorer here is a pure function of the run transcript, the tool schemas the
run advertised, and the fixture the run started from. Nothing reads the model's
prose to form an impression, which is what makes a score reproducible — and what
limits which dimensions can be scored at all (see
backend/evaluation/dimensions.py).

A condition a case does not declare is not scored, and its dimension is reported
as unscored rather than passing by default. An unscored dimension and a passing
one must never look alike in a report.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from jsonschema import Draft7Validator

from backend.evaluation.cases import (
    ID_BEARING_ARGS,
    WRITE_TOOLS,
    AcceptanceCase,
)
from backend.evaluation.dimensions import DIMENSIONS, Mechanical
from backend.evaluation.transcript import RunTranscript, TokenUsage, ToolCall

# An id as this system writes them: a UUID, or a slug of three or more
# hyphen-separated lowercase segments (``task-compare-skills-openai``).
#
# Three segments alone is NOT enough to tell an id from prose: "up-to-date",
# "end-to-end", "state-of-the-art" and "one-size-fits-all" all match the slug
# shape, and reading one of those as a fabricated node id would report a
# hallucination that did not happen — the precise failure the hallucination row
# refuses to risk. So a slug is also rejected when any of its segments is an
# English function word.
#
# That biases the check towards MISSING a real id rather than inventing one: an
# id whose own segments include such a word (``task-fix-edge-auth-on-sspcloud``)
# is not flagged. Deliberate, and consistent with this being a strict lower
# bound — a missed fabrication understates the problem, while a flagged English
# phrase would be a false accusation a reader cannot distinguish from a real
# finding. The closed-vocabulary half of the check (fixture ids cited but never
# read, below) has no such bias and no false positives at all.
#
# Node *names* are deliberately not matched: there is no mechanical way to tell
# a cited node name from a noun phrase that happens to repeat one, so names are
# outside what this check claims to cover.
_UUID_RE = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SLUG_RE = r"[a-z0-9]+(?:-[a-z0-9]+){2,}"
ID_TOKEN_RE = re.compile(rf"\b(?:{_UUID_RE}|{_SLUG_RE})\b")
_UUID_ONLY_RE = re.compile(rf"^{_UUID_RE}$")

# Segments that mark a hyphenated token as English prose rather than an id.
_PROSE_SEGMENTS = frozenset(
    """a an and are as at be been but by can do for from had has have if in into
    is it its may no not of on or per so than that the their then there these
    this to up via vs was were what when which who will with would all one
    both each""".split()
)


def is_id_shaped(token: str) -> bool:
    """
    Whether a token in an answer could be an id this system would write.

    Shape only, and shape is not enough on its own — see
    ``score_answer_entities_supported``, which additionally requires an
    open-vocabulary candidate to belong to the graph's id vocabulary. A date
    ("2026-10-08") and a version string ("gpt-4o-mini") both pass this.
    """
    if _UUID_ONLY_RE.match(token):
        return True
    segments = token.split("-")
    if len(segments) < 3:
        return False
    if any(segment in _PROSE_SEGMENTS for segment in segments):
        return False
    # A date or a numeric sequence: "2026-10-08", "1-2-3".
    if all(segment.isdigit() for segment in segments):
        return False
    return True


# Result keys whose values really are entity ids. The prefix vocabulary is
# built from these, not from every string a result contained: shape cannot tell
# an id from a tag or a timestamp, and `is_id_shaped` says True for both
# "open-data-standard" and "2026-10-08T22:39:02.240070+00:00". The timestamp is
# the one that mattered — every graph result carries created_at/updated_at, so
# every case donated the prefix "2026", and any date-prefixed slug the model
# wrote came back as a fabricated node in the dimension whose own docs list a
# date as a disqualifier.
_ID_BEARING_RESULT_KEYS = frozenset(
    {
        "id",
        "node_id",
        "edge_id",
        "source",
        "target",
        "node_ids",
        "edge_ids",
        "added_node_ids",
        "added_edge_ids",
        "updated_node_ids",
    }
)


def _ids_in_results(obj: Any) -> Set[str]:
    """Values found at id-bearing keys anywhere in a decoded tool result."""
    found: Set[str] = set()
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _ID_BEARING_RESULT_KEYS:
                    if isinstance(value, str):
                        found.add(value)
                    elif isinstance(value, (list, tuple)):
                        found |= {v for v in value if isinstance(v, str)}
                stack.append(value)
        elif isinstance(current, (list, tuple, set)):
            stack.extend(current)
    return found


def _id_vocabulary_prefixes(known_ids: Set[str]) -> Set[str]:
    """
    Leading segments of the ids this run has actually seen.

    Callers pass the fixture's ids plus values found at id-bearing result keys —
    not every string a result contained. Narrowing by SHAPE was not enough:
    `is_id_shaped` admits a three-segment tag and, worse, an ISO timestamp, so
    the vocabulary absorbed "2026" on every single case.

    Ids in a graph share a leading segment by convention (``eval-``, ``task-``,
    ``init-``), and a fabricated id is in practice a near-miss of a real one —
    so it shares that segment too. Requiring it is what separates a made-up
    node from the model's own prose: "2026-10-08", "gpt-4o-mini" and
    "read-only-mode" are all id-SHAPED, and flagging any of them would be a
    false accusation that a reader cannot distinguish from a real finding.

    The cost is a fabrication under a prefix the run never saw, which this
    signal misses. That is the lower bound doing its job; the closed-vocabulary
    signal has no such gap for ids the fixture does contain.
    """
    prefixes = set()
    for value in known_ids:
        # Only values that are themselves ids may donate a prefix. Built from
        # every string a result contained, an ordinary two-segment tag like
        # "open-data" donated "open", and the answer's "open-source-first" was
        # then reported as a fabricated node — the false accusation this signal
        # exists to avoid, moved one step out rather than removed.
        if is_id_shaped(value):
            prefixes.add(value.split("-", 1)[0])
    return prefixes


def cited_id_tokens(text: str) -> Set[str]:
    """Id-shaped tokens cited in a model's answer."""
    return {token for token in ID_TOKEN_RE.findall(text or "") if is_id_shaped(token)}


@dataclass
class ConditionResult:
    """Outcome of one declared pass condition."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class DimensionScore:
    """What the run says about one dimension."""

    dimension: str
    mechanical: Mechanical
    scored: bool
    passed: Optional[bool]
    conditions: List[ConditionResult] = field(default_factory=list)
    note: str = ""


@dataclass
class CaseScore:
    """The full verdict for one case run."""

    case_id: str
    dimension: str
    conditions: List[ConditionResult] = field(default_factory=list)
    dimensions: Dict[str, DimensionScore] = field(default_factory=dict)
    latency_ms: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider_calls: int = 0
    tool_calls: List[str] = field(default_factory=list)
    run_error: Optional[str] = None

    @property
    def passed(self) -> bool:
        """True when the run completed and every declared condition held."""
        if self.run_error:
            return False
        return bool(self.conditions) and all(c.passed for c in self.conditions)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _all_strings(obj: Any) -> Set[str]:
    """
    Every string anywhere in a decoded tool result.

    The ID-first rule asks whether the model had *seen* an id before using it,
    so membership in the text it was shown is exactly the right test — stricter
    key-based extraction would invent false failures whenever a result nests ids
    somewhere this code did not anticipate.
    """
    found: Set[str] = set()
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            found.add(current)
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, (list, tuple, set)):
            stack.extend(current)
    return found


def _resolve_arg_path(args: Any, path: str) -> List[Any]:
    """
    Read the values at a dotted path, where ``[]`` fans out over a list.

    ``edges[].source`` over ``{"edges": [{"source": "a"}, {"source": "b"}]}``
    yields ``["a", "b"]``.
    """
    current: List[Any] = [args]
    for segment in path.split("."):
        fan_out = segment.endswith("[]")
        key = segment[:-2] if fan_out else segment
        nxt: List[Any] = []
        for item in current:
            if not isinstance(item, dict) or key not in item:
                continue
            value = item[key]
            if fan_out:
                if isinstance(value, (list, tuple)):
                    nxt.extend(value)
            else:
                nxt.append(value)
        current = nxt
    return current


def _ids_in_call(call: ToolCall) -> List[str]:
    """Ids the call passes to a write or relationship tool."""
    paths = ID_BEARING_ARGS.get(call.name)
    if not paths:
        return []
    ids: List[str] = []
    for path in paths:
        for value in _resolve_arg_path(call.input, path):
            if isinstance(value, str) and value:
                ids.append(value)
    return ids


def _same_call_node_references(call: ToolCall) -> Set[str]:
    """
    Node references add_nodes declares in the same call its edges may point at.

    An edge may legitimately point at a node being created alongside it, and the
    advertised add_nodes schema gives no way to do that by id: its ``nodes``
    item schema has no ``id`` property at all — ids are server-generated — while
    ``edges[].source``/``target`` are documented as "Source node ID **or
    name**", and storage resolves a name against the graph. So the reference the
    model can legitimately use is the NAME it is creating in this same call.

    Collecting only ids here was a defect: every schema-valid create-and-connect
    call was charged an ID-first failure, and the only way to score a pass was
    to invent a client-side ``nodes[].id`` — an undeclared nested parameter. A
    supplied id is still collected, because storage does adopt one when given.
    """
    if call.name != "add_nodes":
        return set()
    references: Set[str] = set()
    for node in _resolve_arg_path(call.input, "nodes[]"):
        if not isinstance(node, dict):
            continue
        for key in ("id", "name"):
            value = node.get(key)
            if isinstance(value, str) and value:
                references.add(value)
    return references


def _was_shown(value: str, shown: Set[str]) -> bool:
    """
    Whether the model was shown ``value`` among the strings in ``shown``.

    A result may embed an id inside a longer string (a status message, a URL),
    and the model read it there just as surely as it would read a bare field, so
    a substring hit counts. Shared by the ID-first and answer-citation scorers:
    they asked the same question in two different ways before, so an id seen
    only inside a message counted as supported in the answer but not as read
    before a write.
    """
    if value in shown:
        return True
    return any(value in candidate for candidate in shown)


def _ids_known_before(transcript: RunTranscript, turn: int) -> Set[str]:
    """Strings the model had been shown in tool results before ``turn``."""
    known: Set[str] = set()
    for call in transcript.tool_calls:
        if call.turn >= turn:
            continue
        if call.tool_use_id in transcript.tool_results:
            known |= _all_strings(transcript.tool_results[call.tool_use_id])
    return known


def _result_is_error(result: Any) -> bool:
    """Whether a decoded tool result reports failure."""
    if isinstance(result, dict):
        if result.get("error"):
            return True
        if result.get("success") is False:
            return True
    return False


# Writes whose id-bearing arguments name the nodes being written. add_nodes is
# deliberately absent: its entry in ID_BEARING_ARGS is edges[].source/target,
# which are references to OTHER nodes the call did not write — and since those
# may be names, a read-back matching one would be matching a pre-existing node
# by name. The node add_nodes actually creates is only knowable from its result.
_WRITES_NAMING_THEIR_OWN_TARGET = frozenset(ID_BEARING_ARGS) - {"add_nodes"}


def _written_node_ids(transcript: RunTranscript, call: ToolCall) -> Set[str]:
    """
    Node ids a successful write created or changed.

    Not the same question as "which ids did this call reference", which is what
    ID_BEARING_ARGS answers and what this used to reuse. For add_nodes the two
    are actually disjoint: the referenced ids are the pre-existing endpoints of
    the new edges, so a model could create a node, read back only the old node
    its edge pointed at, and pass post-write verification without the written
    node ever being read — the vacuous pass G3 forbids.
    """
    ids = (
        {i for i in _ids_in_call(call)}
        if call.name in _WRITES_NAMING_THEIR_OWN_TARGET
        else set()
    )
    result = transcript.result_for(call.tool_use_id)
    if isinstance(result, dict):
        for key in ("added_node_ids", "updated_node_ids", "node_ids"):
            value = result.get(key)
            if isinstance(value, list):
                ids |= {v for v in value if isinstance(v, str)}
        node = result.get("node")
        if isinstance(node, dict) and isinstance(node.get("id"), str):
            ids.add(node["id"])
    return {i for i in ids if i}


def _abbreviate(value: Any, limit: int = 120) -> str:
    """
    Render a value for a report, bounded.

    A condition's detail reaches build_report, and a field the model wrote can
    run to 2000 characters, so an unbounded repr would put a paragraph of the
    model's own writing into a report documented as carrying none of it. The
    prefix is what diagnosis actually needs.
    """
    text = repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… ({len(text)} chars)"


def _normalise_field(value: Any) -> Any:
    """
    Collapse "absent" and "empty" to one value before comparing two graph states.

    A fixture omits fields it does not set; the serializer fills them in with
    defaults (``subtypes: []``, ``aliases: []``, ``metadata: {}``). Comparing
    raw values then reads ``None != []`` as a change, so a case naming one of
    those fields passed on a run in which the model did nothing at all — a
    scorer passing vacuously, which is the one thing no scorer here may do.
    """
    if value is None or value == [] or value == {} or value == "":
        return None
    return value


def _find_node(graph: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    return None


def _is_ordered_subsequence(required: Sequence[str], actual: Sequence[str]) -> bool:
    it = iter(actual)
    return all(any(name == seen for seen in it) for name in required)


# --------------------------------------------------------------------------
# condition scorers
# --------------------------------------------------------------------------


def _undeclared_arguments(arguments: Any, schema: Dict[str, Any]) -> List[str]:
    """
    Top-level arguments the tool's schema does not declare.

    Checked separately from JSON Schema validation because the tool schemas do
    not set ``additionalProperties: false``, so a validator accepts an invented
    parameter. It is a real model error all the same, and a quiet one:
    chat_logic filters a tool's arguments to the names in its Python signature
    (backend/ui/chat_logic.py), so an invented name is silently *dropped* and
    the tool runs with a default instead. The model is then told the call
    succeeded while the value it chose was discarded, which is precisely the
    class of failure this dimension exists to surface.
    """
    if not isinstance(arguments, dict):
        return []
    declared = schema.get("properties")
    if not isinstance(declared, dict):
        return []
    return sorted(name for name in arguments if name not in declared)


def score_tool_calls_valid(
    transcript: RunTranscript, tool_definitions: Sequence[Dict[str, Any]]
) -> ConditionResult:
    """Every call names an advertised tool and validates against its schema."""
    schemas = {
        t["name"]: t.get("input_schema") or {}
        for t in tool_definitions
        if isinstance(t, dict) and "name" in t
    }
    advertised = set(
        name
        for call in transcript.provider_calls
        for name in call.tools_advertised
        if name
    ) or set(schemas)

    problems: List[str] = []
    for index, call in enumerate(transcript.tool_calls):
        if call.name not in advertised:
            problems.append(
                f"#{index} {_abbreviate(call.name, 80)}: not advertised in this run"
            )
            continue
        schema = schemas.get(call.name)
        if not schema:
            problems.append(
                f"#{index} {call.name!r}: no input_schema to validate against"
            )
            continue
        errors = sorted(
            Draft7Validator(schema).iter_errors(call.input), key=lambda e: list(e.path)
        )
        for error in errors:
            location = "/".join(str(p) for p in error.path) or "(root)"
            # jsonschema's message embeds the offending instance, which is a
            # value the model wrote, so it is bounded like any other quote.
            problems.append(
                f"#{index} {call.name}.{location}: {_abbreviate(error.message, 200)}"
            )
        for name in _undeclared_arguments(call.input, schema):
            problems.append(
                f"#{index} {_abbreviate(call.name, 80)}."
                f"{_abbreviate(name, 80)}: not a parameter of this tool"
            )

    if not transcript.tool_calls:
        return ConditionResult(
            "tool_calls_valid", False, "the model made no tool calls at all"
        )
    if problems:
        return ConditionResult("tool_calls_valid", False, "; ".join(problems))
    return ConditionResult(
        "tool_calls_valid", True, f"{len(transcript.tool_calls)} call(s) valid"
    )


def score_required_call_sequence(
    transcript: RunTranscript, required: Sequence[str]
) -> ConditionResult:
    actual = transcript.tool_call_names
    if _is_ordered_subsequence(required, actual):
        return ConditionResult(
            "required_call_sequence", True, f"{list(required)} in order within {actual}"
        )
    return ConditionResult(
        "required_call_sequence",
        False,
        f"expected {list(required)} as an ordered subsequence of "
        f"{_abbreviate(actual, 300)}",
    )


def score_forbidden_calls(
    transcript: RunTranscript, forbidden: Sequence[str]
) -> ConditionResult:
    hit = [name for name in transcript.tool_call_names if name in set(forbidden)]
    if hit:
        return ConditionResult("forbidden_calls", False, f"called {sorted(set(hit))}")
    if not transcript.tool_calls:
        # Its three siblings all refuse this; this one did not. A model that
        # called nothing at all has not demonstrated restraint, and crediting
        # it would be a scorer passing vacuously.
        return ConditionResult(
            "forbidden_calls",
            False,
            "the model made no tool calls at all, so it avoided nothing",
        )
    return ConditionResult("forbidden_calls", True, f"none of {list(forbidden)} called")


def score_ids_resolved_from_results(transcript: RunTranscript) -> ConditionResult:
    """Every id passed to a write or relationship tool was read back first."""
    unresolved: List[str] = []
    checked = 0
    for call in transcript.tool_calls:
        ids = _ids_in_call(call)
        if not ids:
            continue
        known = _ids_known_before(transcript, call.turn) | _same_call_node_references(
            call
        )
        for value in ids:
            checked += 1
            if not _was_shown(value, known):
                unresolved.append(
                    f"{call.name}({_abbreviate(value, 80)}) at turn {call.turn}"
                )

    if unresolved:
        return ConditionResult(
            "ids_resolved_from_results",
            False,
            f"id(s) used without being read first: {unresolved}",
        )
    if checked == 0:
        return ConditionResult(
            "ids_resolved_from_results",
            False,
            "no write or relationship call carried an id, so the rule was never exercised",
        )
    return ConditionResult(
        "ids_resolved_from_results", True, f"{checked} id reference(s) all read first"
    )


# Writes that leave the node readable afterwards, so "a read returned it" is
# the verification. A removal (delete_*, archive_*) leaves it absent or hidden,
# and this scorer is a PRESENCE check — pointing it at one asks the model to
# read back a node that is supposed to be gone, and scores a correct
# verification as a failure. Those cases are rejected with a detail that names
# the case as mis-specified rather than blaming the model.
_PRESENCE_VERIFIABLE_WRITES = frozenset(
    {"add_nodes", "update_node", "unarchive_nodes", "unarchive_edges"}
)


def score_verify_after_write(
    transcript: RunTranscript, read_tools: Sequence[str]
) -> ConditionResult:
    """A read after the last successful write returned the written node."""
    successful = [
        (index, call)
        for index, call in enumerate(transcript.tool_calls)
        if call.name in WRITE_TOOLS
        and not _result_is_error(transcript.result_for(call.tool_use_id))
    ]
    if not successful:
        return ConditionResult(
            "verify_after_write", False, "no successful write occurred in the run"
        )

    writes = [
        (index, call)
        for index, call in successful
        if call.name in _PRESENCE_VERIFIABLE_WRITES
    ]
    if not writes:
        # Two different situations, and conflating them misattributes one of
        # them. If the model ATTEMPTED a presence-verifiable write and it
        # failed, that is the model's doing; only when every write this run
        # could make is a removal is the case at fault.
        attempted = sorted(
            {
                call.name
                for call in transcript.tool_calls
                if call.name in _PRESENCE_VERIFIABLE_WRITES
            }
        )
        if attempted:
            return ConditionResult(
                "verify_after_write",
                False,
                f"the model's {attempted} write did not succeed, so there was "
                "nothing to read back",
            )
        removals = sorted({call.name for _, call in successful})
        return ConditionResult(
            "verify_after_write",
            False,
            f"this run's only writes were {removals}, which remove or hide a node; "
            "post_write_verification is a presence check and is not defined for "
            "them, so this case is mis-specified rather than the model at fault "
            "(see docs/SKILL_EVALUATION.md, 'Adding a case')",
        )

    last_index, last_write = writes[-1]
    written = _written_node_ids(transcript, last_write)
    if not written:
        return ConditionResult(
            "verify_after_write",
            False,
            f"could not determine which node {last_write.name} wrote",
        )

    allowed = set(read_tools)
    for call in transcript.tool_calls[last_index + 1 :]:
        if call.name not in allowed:
            continue
        seen = _all_strings(transcript.result_for(call.tool_use_id))
        overlap = written & seen
        if overlap:
            return ConditionResult(
                "verify_after_write",
                True,
                f"{call.name} after {last_write.name} returned {sorted(overlap)}",
            )
    return ConditionResult(
        "verify_after_write",
        False,
        f"no {sorted(allowed)} call after {last_write.name} returned any of "
        f"{_abbreviate(sorted(written), 200)}",
    )


def score_final_node_state(
    transcript: RunTranscript, expected: Dict[str, Dict[str, Any]]
) -> ConditionResult:
    problems: List[str] = []
    for node_id, fields in expected.items():
        node = _find_node(transcript.final_graph, node_id)
        if node is None:
            problems.append(f"{node_id}: absent from the final graph")
            continue
        for key, want in fields.items():
            got = node.get(key)
            if got != want:
                problems.append(
                    f"{node_id}.{key}: expected {want!r}, got {_abbreviate(got)}"
                )
    if problems:
        return ConditionResult("final_node_state", False, "; ".join(problems))
    return ConditionResult(
        "final_node_state", True, f"{len(expected)} node state(s) as expected"
    )


def score_final_node_fields_changed(
    transcript: RunTranscript,
    fixture_graph: Dict[str, Any],
    expected: Dict[str, List[str]],
) -> ConditionResult:
    problems: List[str] = []
    for node_id, fields in expected.items():
        before = _find_node(fixture_graph, node_id)
        after = _find_node(transcript.final_graph, node_id)
        if after is None:
            problems.append(f"{node_id}: absent from the final graph")
            continue
        for key in fields:
            was = _normalise_field((before or {}).get(key))
            now = _normalise_field(after.get(key))
            if now == was:
                problems.append(f"{node_id}.{key}: unchanged ({was!r})")
    if problems:
        return ConditionResult("final_node_fields_changed", False, "; ".join(problems))
    changed = sum(len(f) for f in expected.values())
    return ConditionResult(
        "final_node_fields_changed", True, f"{changed} field(s) changed as required"
    )


def score_answer_entities_supported(
    transcript: RunTranscript, fixture_graph: Optional[Dict[str, Any]] = None
) -> ConditionResult:
    """
    No node the final answer cites is one the run never read.

    Two signals, deliberately of different character:

    1. **Closed vocabulary, no false positives.** Any id from the case's own
       fixture graph that the answer cites but no tool result returned. The
       answer named a real node the model never looked at.
    2. **Open vocabulary, biased towards misses.** An id-shaped token (see
       ``is_id_shaped``) that belongs to this graph's id vocabulary — it shares
       a leading segment with an id the run has seen — and matches nothing the
       model was shown. Catches a fabricated id, which in practice is a
       near-miss of a real one, and is tuned to miss rather than to misfire:
       a false accusation here is indistinguishable from a real finding.

    This is the narrow, separately-named check behind the
    ``unsupported_entity_reference`` dimension — NOT a hallucination rate. See
    that dimension's caveat for what it does and does not catch.
    """
    shown: Set[str] = set()
    for result in transcript.tool_results.values():
        shown |= _all_strings(result)

    answer = transcript.final_text or ""

    fixture_ids = {
        node["id"]
        for node in (fixture_graph or {}).get("nodes") or []
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    # Signal 1, closed vocabulary: a real node of this fixture, cited but unread.
    cited_fixture_ids = {node_id for node_id in fixture_ids if node_id in answer}

    # Signal 2, open vocabulary: an id-shaped token belonging to this graph's id
    # vocabulary. A UUID is unambiguous on shape alone; a slug must share its
    # leading segment with an id the run has seen, or the model's own dates and
    # version strings get reported as fabricated nodes.
    result_ids: Set[str] = set()
    for result in transcript.tool_results.values():
        result_ids |= _ids_in_results(result)
    prefixes = _id_vocabulary_prefixes(fixture_ids | result_ids)
    cited_tokens = {
        token
        for token in cited_id_tokens(answer)
        if _UUID_ONLY_RE.match(token) or token.split("-", 1)[0] in prefixes
    }

    unsupported = sorted(
        {
            token
            for token in cited_tokens | cited_fixture_ids
            if not _was_shown(token, shown)
        }
    )
    if unsupported:
        return ConditionResult(
            "answer_entities_supported",
            False,
            f"answer cites node(s) no tool result returned: "
            f"{_abbreviate(unsupported, 300)}",
        )
    checked = cited_tokens | cited_fixture_ids
    if not checked:
        return ConditionResult(
            "answer_entities_supported",
            True,
            "the answer cites no node id (vacuously supported)",
        )
    return ConditionResult(
        "answer_entities_supported",
        True,
        f"{len(checked)} cited id(s) all returned by a tool result",
    )


def score_discriminating_first_call(
    transcript: RunTranscript, expected: str
) -> ConditionResult:
    actual = transcript.tool_call_names
    if not actual:
        return ConditionResult(
            "discriminating_first_call", False, "the model made no tool calls at all"
        )
    if actual[0] == expected:
        return ConditionResult(
            "discriminating_first_call", True, f"first call was {expected!r}"
        )
    return ConditionResult(
        "discriminating_first_call",
        False,
        f"expected first call {expected!r}, got {_abbreviate(actual[0], 80)} "
        f"(sequence {_abbreviate(actual, 300)})",
    )


# --------------------------------------------------------------------------
# case-level scoring
# --------------------------------------------------------------------------


def _evaluate_conditions(
    case: AcceptanceCase,
    transcript: RunTranscript,
    tool_definitions: Sequence[Dict[str, Any]],
    fixture_graph: Dict[str, Any],
) -> List[ConditionResult]:
    expect = case.expect
    results: List[ConditionResult] = []

    if expect.tool_calls_valid is not None:
        actual = score_tool_calls_valid(transcript, tool_definitions)
        results.append(_align(actual, expect.tool_calls_valid))
    if expect.required_call_sequence:
        results.append(
            score_required_call_sequence(transcript, expect.required_call_sequence)
        )
    if expect.forbidden_calls:
        results.append(score_forbidden_calls(transcript, expect.forbidden_calls))
    if expect.ids_resolved_from_results is not None:
        actual = score_ids_resolved_from_results(transcript)
        results.append(_align(actual, expect.ids_resolved_from_results))
    if expect.verify_after_write is not None:
        actual = score_verify_after_write(transcript, expect.verify_read_tools)
        results.append(_align(actual, expect.verify_after_write))
    if expect.final_node_state:
        results.append(score_final_node_state(transcript, expect.final_node_state))
    if expect.final_node_fields_changed:
        results.append(
            score_final_node_fields_changed(
                transcript, fixture_graph, expect.final_node_fields_changed
            )
        )
    if expect.answer_entities_supported is not None:
        actual = score_answer_entities_supported(transcript, fixture_graph)
        results.append(_align(actual, expect.answer_entities_supported))
    if expect.discriminating_first_call is not None:
        results.append(
            score_discriminating_first_call(
                transcript, expect.discriminating_first_call
            )
        )
    return results


def _align(result: ConditionResult, expected_outcome: bool) -> ConditionResult:
    """
    Reconcile a boolean condition with the polarity the case declared.

    A case may assert that a behaviour must NOT hold — a negative case pinning
    that the harness detects a violation rather than only that a good model
    passes. Without this, ``false`` in a fixture would silently read as "do not
    check", and a suite of only positive cases cannot tell a working scorer from
    one that always returns True.
    """
    if result.passed == expected_outcome:
        return ConditionResult(result.name, True, result.detail)
    return ConditionResult(
        result.name,
        False,
        f"expected this condition to be {expected_outcome}; {result.detail}",
    )


def score_case(
    case: AcceptanceCase,
    transcript: RunTranscript,
    tool_definitions: Sequence[Dict[str, Any]],
    fixture_graph: Optional[Dict[str, Any]] = None,
) -> CaseScore:
    """Score one recorded run against its case."""
    conditions = (
        []
        if transcript.run_error
        else _evaluate_conditions(
            case, transcript, tool_definitions, fixture_graph or {}
        )
    )
    by_name = {c.name: c for c in conditions}

    dimension_scores: Dict[str, DimensionScore] = {}
    for key, dim in DIMENSIONS.items():
        relevant = [by_name[n] for n in dim.measured_by if n in by_name]
        if dim.mechanical is Mechanical.NONE:
            dimension_scores[key] = DimensionScore(
                dimension=key,
                mechanical=dim.mechanical,
                scored=False,
                passed=None,
                note=dim.caveat,
            )
            continue
        if dim.mechanical is Mechanical.REPORTED:
            # Latency and tokens: the numbers live on the CaseScore and there is
            # no pass condition, so `scored` stays False — a reader must not see
            # a measured-but-unjudged dimension the same way as a passed one.
            dimension_scores[key] = DimensionScore(
                dimension=key,
                mechanical=dim.mechanical,
                scored=False,
                passed=None,
                note=dim.caveat,
            )
            continue
        if not relevant:
            dimension_scores[key] = DimensionScore(
                dimension=key,
                mechanical=dim.mechanical,
                scored=False,
                passed=None,
                note="this case declares no condition for this dimension",
            )
            continue
        dimension_scores[key] = DimensionScore(
            dimension=key,
            mechanical=dim.mechanical,
            scored=True,
            passed=all(c.passed for c in relevant),
            conditions=relevant,
            note=dim.caveat,
        )

    return CaseScore(
        case_id=case.id,
        dimension=case.dimension,
        conditions=conditions,
        dimensions=dimension_scores,
        latency_ms=transcript.total_latency_ms,
        usage=transcript.total_usage,
        provider_calls=len(transcript.provider_calls),
        tool_calls=transcript.tool_call_names,
        run_error=transcript.run_error,
    )
