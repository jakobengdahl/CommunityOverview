"""
Acceptance cases: a graph state, a prompt, and a machine-checkable expectation.

A case states its pass condition as a predicate over the recorded run (see
backend/evaluation/transcript.py), never as something a reader has to judge. If
a behaviour cannot be written down that way it does not belong in a case — the
dimension table (backend/evaluation/dimensions.py) says which behaviours those
are and why.

Cases, fixture graphs and fixture skills are data files under
``backend/evaluation/fixtures/`` so the owner can add cases without touching
code.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.evaluation.dimensions import DIMENSIONS

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GRAPHS_DIR = FIXTURES_DIR / "graphs"
SKILLS_DIR = FIXTURES_DIR / "skills"
CASES_FILE = FIXTURES_DIR / "cases.json"

# Read tools a post-write read-back may come through. get_node_details is absent
# because the assistant does not advertise it to the model (it is executable in
# ChatService's tools map but not in ChatProcessor's tool_definitions), so a case
# that demanded it would fail for a reason that has nothing to do with the model.
DEFAULT_VERIFY_READ_TOOLS = ("search_graph", "get_related_nodes", "find_similar_nodes")

# Tools whose arguments carry ids the model must have read before using, and the
# paths those ids sit at. "[]" steps into every element of a list. Writes and
# relationship operations only — the ID-first rule is about those.
ID_BEARING_ARGS: Dict[str, tuple] = {
    "update_node": ("node_id",),
    "delete_nodes": ("node_ids[]",),
    "archive_nodes": ("node_ids[]",),
    "unarchive_nodes": ("node_ids[]",),
    "delete_edges": ("edge_ids[]",),
    "archive_edges": ("edge_ids[]",),
    "unarchive_edges": ("edge_ids[]",),
    "add_nodes": ("edges[].source", "edges[].target"),
    "mark_nodes": ("marks[].node_id",),
    "save_view": (),
}

WRITE_TOOLS = (
    "add_nodes",
    "update_node",
    "delete_nodes",
    "delete_edges",
    "archive_nodes",
    "unarchive_nodes",
    "archive_edges",
    "unarchive_edges",
)


class ExpectedBehaviour(BaseModel):
    """
    The pass conditions for one case. Every field is a predicate over the run.

    A field left unset is not checked, and the dimension it would have scored is
    reported as unscored for that case rather than silently passing.
    """

    tool_calls_valid: Optional[bool] = None
    """Every call names an advertised tool and validates against its schema."""

    required_call_sequence: List[str] = Field(default_factory=list)
    """Tool names that must appear in this relative order (not necessarily adjacent)."""

    forbidden_calls: List[str] = Field(default_factory=list)
    """Tool names that must not appear at all."""

    ids_resolved_from_results: Optional[bool] = None
    """Every id in a write/relationship argument was read back first."""

    verify_after_write: Optional[bool] = None
    """A read after the last write returned the written node."""

    verify_read_tools: List[str] = Field(
        default_factory=lambda: list(DEFAULT_VERIFY_READ_TOOLS)
    )

    final_node_state: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    """node_id -> {field: exact expected value} in the graph after the run."""

    final_node_fields_changed: Dict[str, List[str]] = Field(default_factory=dict)
    """node_id -> fields that must differ from the fixture's value."""

    answer_entities_supported: Optional[bool] = None
    """No node id or quoted node name in the final answer is unsupported by a tool result."""

    discriminating_first_call: Optional[str] = None
    """The first tool call the applicable injected skill mandates."""

    @model_validator(mode="after")
    def _at_least_one_condition(self) -> "ExpectedBehaviour":
        if not self.declared_conditions():
            raise ValueError(
                "expect declares no pass condition — a case whose outcome cannot be "
                "checked is not an acceptance case"
            )
        return self

    def declared_conditions(self) -> List[str]:
        """Names of the expectation fields this case actually declares."""
        declared: List[str] = []
        for name in (
            "tool_calls_valid",
            "ids_resolved_from_results",
            "verify_after_write",
            "answer_entities_supported",
            "discriminating_first_call",
        ):
            if getattr(self, name) is not None:
                declared.append(name)
        for name in (
            "required_call_sequence",
            "forbidden_calls",
            "final_node_state",
            "final_node_fields_changed",
        ):
            if getattr(self, name):
                declared.append(name)
        return declared


class AcceptanceCase(BaseModel):
    """One prompt run against one fixture graph with one expectation."""

    id: str
    dimension: str
    """Primary dimension this case exists to measure; must be a key in DIMENSIONS."""
    prompt: str
    graph: str
    """Fixture graph filename under fixtures/graphs/."""
    skills: List[str] = Field(default_factory=list)
    """Fixture SKILL.md filenames under fixtures/skills/, injected in this order."""
    expect: ExpectedBehaviour
    notes: str = ""

    @field_validator("id", "prompt", "graph")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("dimension")
    @classmethod
    def _known_dimension(cls, v: str) -> str:
        if v not in DIMENSIONS:
            raise ValueError(f"unknown dimension {v!r}; known: {sorted(DIMENSIONS)}")
        return v

    @model_validator(mode="after")
    def _expectation_covers_dimension(self) -> "AcceptanceCase":
        """
        A case must actually declare a condition that scores its own dimension.

        Without this a case can claim to measure ID resolution while checking
        only that the call sequence was valid — the suite then reports coverage
        of a dimension nothing tested.
        """
        dim = DIMENSIONS[self.dimension]
        if not dim.measured_by:
            # Reported-only (latency, token_profile) or deliberately unscored
            # (hallucination): any declared condition is enough to make the run useful.
            return self
        declared = set(self.expect.declared_conditions())
        if not declared.intersection(dim.measured_by):
            raise ValueError(
                f"case {self.id!r} claims dimension {self.dimension!r} but declares "
                f"none of its conditions {dim.measured_by}; declared: {sorted(declared)}"
            )
        return self

    def graph_path(self, graphs_dir: Optional[Path] = None) -> Path:
        return (graphs_dir or GRAPHS_DIR) / self.graph

    def skill_paths(self, skills_dir: Optional[Path] = None) -> List[Path]:
        base = skills_dir or SKILLS_DIR
        return [base / name for name in self.skills]


def load_cases(
    cases_file: Optional[Path] = None,
    graphs_dir: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
) -> List[AcceptanceCase]:
    """
    Load and validate the acceptance cases, checking that their fixtures exist.

    A missing fixture is raised here rather than at run time: a suite that only
    discovers a broken case once a paid provider call has been made is worse
    than one that refuses to start.
    """
    path = cases_file or CASES_FILE
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array of cases")

    cases = [AcceptanceCase.model_validate(item) for item in raw]

    seen: Dict[str, int] = {}
    for case in cases:
        seen[case.id] = seen.get(case.id, 0) + 1
    duplicates = sorted(cid for cid, n in seen.items() if n > 1)
    if duplicates:
        raise ValueError(f"duplicate case id(s): {duplicates}")

    for case in cases:
        graph_path = case.graph_path(graphs_dir)
        if not graph_path.is_file():
            raise ValueError(f"case {case.id!r}: missing fixture graph {graph_path}")
        for skill_path in case.skill_paths(skills_dir):
            if not skill_path.is_file():
                raise ValueError(
                    f"case {case.id!r}: missing fixture skill {skill_path}"
                )

    return cases
