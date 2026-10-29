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
    keyed by ``("node" | "edge", id)``, how many events it fired, and the
    graph's generation it landed at, read under the same lock."""

    revisions: Dict[Tuple[str, str], int]
    events: int
    generation: int


# --- session merges (docs/SESSION_OVERLAY_CONTRACT.md §11) --------------------


@dataclass(frozen=True)
class SessionMergeWrite:
    """One graph write of a session merge, in the order the unit applies it.

    ``payload`` is the whole entity for an upsert (the storage stamps its
    revision); None for a delete. A create expects its id absent; an update
    or a delete expects the revision the plan read. ``staged_by`` is who
    staged the change, for the event the write fires.
    """

    kind: str
    action: str
    entity_id: str
    payload: Any = None
    expected_revision: Any = None
    expect_absent: bool = False
    staged_by: Any = None


@dataclass
class SessionMergePlan:
    """Everything one session merge hands the layer store's merge unit: the
    writes, the checks the decision depended on, the entries it removes and
    the record it stores (§7, §11.3)."""

    writes: List[SessionMergeWrite] = field(default_factory=list)
    checks: List[Any] = field(default_factory=list)
    removals: List[Tuple[str, str]] = field(default_factory=list)
    record: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMergeLanded:
    """A merge's stored record, and whether this call only replayed it (I9).
    ``revisions`` maps ("node" | "edge", id) to the revision each write
    produced, None for a delete; empty for a replay."""

    record: Dict[str, Any]
    replayed: bool
    revisions: Dict[Tuple[str, str], Any] = field(default_factory=dict)
