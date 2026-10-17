"""
What a merge import commit hands ``GraphStorage.apply_import_merge``.

docs/adr/0013-graph-import-merge-mode.md §4: the commit applies a planned set
of creates and overwrites on top of the live graph all-or-nothing, each write
carrying its expectation. The planner lives in the service layer; these are
the plain shapes it produces, so the storage layer depends on nothing there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .models import Edge, Node


class ImportMergeUnsupported(RuntimeError):
    """The persistence backend cannot take a merge yet (ADR 0013 §6).

    A store that assigns revisions is shared by several instances, and the
    embedding commit a merge needs there is slice 3's; until it exists a
    merge is refused rather than let the embedding job's whole-graph save
    overwrite other instances' writes.
    """

    code = "import_merge_not_supported"


@dataclass(frozen=True)
class NodeOverwrite:
    """Take the document's caller-writable ``fields`` onto a live node."""

    node_id: str
    expected_revision: int
    fields: Dict[str, Any]
    # The embedding text changed, so the live vector no longer describes it.
    drop_vector: bool


@dataclass(frozen=True)
class EdgeOverwrite:
    edge_id: str
    expected_revision: int
    fields: Dict[str, Any]


@dataclass
class MergeWrites:
    """Every write one merge commit makes, ids already final.

    A create expects its id absent and an overwrite expects the revision the
    plan read; every node an edge here names exists live or is created here.
    """

    node_creates: List[Node] = field(default_factory=list)
    node_overwrites: List[NodeOverwrite] = field(default_factory=list)
    edge_creates: List[Edge] = field(default_factory=list)
    edge_overwrites: List[EdgeOverwrite] = field(default_factory=list)


@dataclass
class MergeApplied:
    """What a landed merge commit wrote: each entity's resulting revision,
    keyed by ``("node" | "edge", id)``, and how many events it fired."""

    revisions: Dict[Tuple[str, str], int]
    events: int
