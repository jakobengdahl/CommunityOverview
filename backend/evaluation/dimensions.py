"""
The measured dimensions, and how honestly each one can be scored by machine.

This table is the harness's contract with its reader. Every dimension the
evaluation reports comes from here, and each one carries how mechanically it
can be scored:

- ``FULL``     — the pass condition is a predicate over the recorded run. Two
                 runs of the same case on the same output always score the same,
                 and no human reads the answer to decide.
- ``PARTIAL``  — only part of the dimension is a predicate. The scored part is
                 stated in ``caveat``; the rest is not reported as a number.
- ``REPORTED`` — measured objectively but with no pass condition at all: a
                 number the reader compares across providers. Latency and
                 tokens are these. They are deliberately NOT ``FULL``: calling
                 a measurement with no threshold "fully scored" would say the
                 harness passes or fails a provider on it, which it does not.
- ``NONE``     — not scorable without a methodology decision. The harness
                 reports no score for it, deliberately. A metric that looks
                 objective but encodes an unstated judgement is worse than an
                 acknowledged gap, because a reader cannot tell it apart from
                 one that does not.

Keeping this in code rather than only in prose means the docs and the tests can
both pin it (see backend/evaluation/tests/test_cases.py::TestDimensionTable,
which also pins the table in docs/SKILL_EVALUATION.md against this one).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class Mechanical(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    REPORTED = "reported"
    NONE = "none"


@dataclass(frozen=True)
class Dimension:
    key: str
    title: str
    mechanical: Mechanical
    measured_by: List[str] = field(default_factory=list)
    """Expectation fields on AcceptanceCase.expect that score this dimension."""
    caveat: str = ""


DIMENSIONS: Dict[str, Dimension] = {
    d.key: d
    for d in (
        Dimension(
            key="skill_selection",
            title="Correct skill selection",
            mechanical=Mechanical.PARTIAL,
            measured_by=["discriminating_first_call"],
            caveat=(
                "The assistant does not choose between skills itself — skills are "
                "injected into the system prompt by the caller (chat_logic's "
                "skills_override). What is scored is therefore the observable proxy: "
                "when two skills are injected and only one applies to the prompt, the "
                "applicable skill mandates a distinctive first tool call and the other "
                "mandates a different one, so the first call discriminates. This "
                "measures whether the model applies the right injected skill, not "
                "whether a retrieval step picked the right skill to inject. Note the "
                "chat path injects a skill's BODY only — it passes when_to_use solely "
                "as a fallback for a skill that has no body — so a fixture skill must "
                "state when it applies inside its body, and the discrimination the "
                "model gets is the one production would give it."
            ),
        ),
        Dimension(
            key="skill_adherence",
            title="Adherence to SKILL.md",
            mechanical=Mechanical.PARTIAL,
            measured_by=["required_call_sequence", "forbidden_calls"],
            caveat=(
                "Only rules expressible as constraints on the tool-call sequence are "
                "scored: a required ordered subsequence of calls, and calls that must "
                "not appear. A SKILL.md rule about prose — tone, how much to explain, "
                "whether a caveat was stated — is not scored, and a case must not "
                "claim it is. Write such a rule as a sequence constraint or leave it "
                "to the owner's own reading of the transcript."
            ),
        ),
        Dimension(
            key="tool_call_validity",
            title="Tool-call validity",
            mechanical=Mechanical.FULL,
            measured_by=["tool_calls_valid"],
            caveat=(
                "Every requested tool must be one the run advertised, and its "
                "arguments must validate against that tool's own input_schema."
            ),
        ),
        Dimension(
            key="id_resolution",
            title="ID-first execution",
            mechanical=Mechanical.FULL,
            measured_by=["ids_resolved_from_results"],
            caveat=(
                "Every node or edge id passed to a write or relationship tool must "
                "have appeared in an earlier tool result in the same run. An id that "
                "happens to be correct but was never read back is still a failure: "
                "the model guessed and got lucky, which is the behaviour the rule "
                "exists to catch."
            ),
        ),
        Dimension(
            key="post_write_verification",
            title="Post-write verification",
            mechanical=Mechanical.FULL,
            measured_by=["verify_after_write"],
            caveat=(
                "After the last successful write, a read tool's result must contain "
                "the written node's id. Checking the result rather than the arguments "
                "keeps this tool-agnostic — and it has to be, because the assistant "
                "does not advertise get_node_details, so a read-back can only come "
                "through search_graph or get_related_nodes."
            ),
        ),
        Dimension(
            key="completeness",
            title="Completeness of the requested change",
            mechanical=Mechanical.FULL,
            measured_by=["final_node_state", "final_node_fields_changed"],
            caveat=(
                "Scored against the graph state after the run, so a case must be able "
                "to enumerate what 'complete' means: either exact expected field "
                "values, or the set of fields that must differ from the fixture. A "
                "request whose completeness cannot be enumerated is not a case for "
                "this harness."
            ),
        ),
        Dimension(
            key="hallucination",
            title="Hallucination rate",
            mechanical=Mechanical.NONE,
            measured_by=[],
            caveat=(
                "NOT scored as a hallucination rate, and the harness reports none. "
                "What counts as a hallucination in free prose is a methodology "
                "decision the owner has to make before any number here means "
                "anything, and guessing at it would produce a confident figure "
                "resting on an unstated definition. The one check implemented is "
                "narrower and separately named in the report: "
                "unsupported_entity_reference, the share of node references in the "
                "final answer that no tool result returned. That is a strict lower "
                "bound on hallucination — it catches a node the run never read, never "
                "a plausible-but-false claim about a node it did read."
            ),
        ),
        Dimension(
            key="unsupported_entity_reference",
            title="Unsupported entity references in the answer",
            mechanical=Mechanical.FULL,
            measured_by=["answer_entities_supported"],
            caveat=(
                "Deliberately not called a hallucination rate, and not a substitute "
                "for one. Two signals. CLOSED vocabulary: a fixture-graph id the "
                "answer cites that no tool result returned — exact, no false "
                "positives. OPEN vocabulary: a token that survives three "
                "disqualifiers and still matches nothing the model was shown. A "
                "token is disqualified if it has fewer than three hyphenated "
                "segments, if any segment is an English function word (so "
                "'up-to-date' is never read as a node), or if every segment is "
                "numeric (so a date like '2026-10-08' is not); and it must then "
                "share its leading segment with an id the run has actually seen, or "
                "the model's own version strings ('gpt-4o-mini') and compounds "
                "('read-only-mode') would be reported as fabricated nodes. Each "
                "disqualifier buys a false negative, and they are the whole reason "
                "this is a lower bound: an id containing a function word is missed, "
                "and so is a fabrication under a prefix the run never saw — a "
                "larger class than the first. Node *names* are not checked at all: "
                "there is no mechanical way to tell a cited node name from a noun "
                "phrase that repeats one. See the hallucination row for why the "
                "broader dimension is left unscored."
            ),
        ),
        Dimension(
            key="latency",
            title="Latency",
            mechanical=Mechanical.REPORTED,
            measured_by=[],
            caveat=(
                "Wall-clock time around each provider call, summed. Reported, never "
                "pass/fail: a threshold would depend on the endpoint's load and "
                "location, not on the model's skill execution."
            ),
        ),
        Dimension(
            key="token_profile",
            title="Token profile",
            mechanical=Mechanical.REPORTED,
            measured_by=[],
            caveat=(
                "Prompt and completion tokens as the provider reports them, summed "
                "across calls, and marked unreported when it reports none — an "
                "OpenAI-compatible endpoint need not. Reported, never pass/fail. "
                "Tokens only: this harness derives no monetary cost."
            ),
        ),
    )
}


def mechanically_scored_dimensions() -> List[str]:
    """
    Dimension keys the harness scores pass/fail, fully or in part.

    Excludes the REPORTED dimensions: latency and tokens are measured, but
    nothing passes or fails on them, so listing them here would overstate what
    a report says.
    """
    return [
        key
        for key, dim in DIMENSIONS.items()
        if dim.mechanical in (Mechanical.FULL, Mechanical.PARTIAL)
    ]


def reported_only_dimensions() -> List[str]:
    """Dimension keys measured as a number, with no pass condition."""
    return [
        key for key, dim in DIMENSIONS.items() if dim.mechanical is Mechanical.REPORTED
    ]


def unscored_dimensions() -> List[str]:
    """Dimension keys the harness deliberately reports no score for."""
    return [key for key, dim in DIMENSIONS.items() if dim.mechanical is Mechanical.NONE]
