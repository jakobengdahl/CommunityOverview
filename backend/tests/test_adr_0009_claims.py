"""Pins the load-bearing claims of ADR 0009 about the tree.

The ADR, CAPACITY.md and PERSISTENCE_BACKENDS.md state facts about code and
about each other: which capabilities the file backend declares, what
`build_persistence_backend` returns for it, which sidecars are gated on the
file backend, and the connection-budget arithmetic. Review of the ADR found a
claim that had drifted or was never true in six of seven rounds, and no link
check or test would have caught any of them. Each test below fails when the
claim stops matching the code or the document it quotes.

Documents are checked for the exact figure or formula the ADR relies on, not
for prose around it, so rewording does not trip them but moving a number does.
"""

import ast
import inspect
import re
from pathlib import Path

from backend.api_host.persistence import build_persistence_backend
from backend.api_host.config import AppConfig
from backend.core import storage as storage_module
from backend.core.storage_backends import FileGraphPersistenceBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR = (REPO_ROOT / "docs/adr/0009-dual-mode-graph-storage-seam.md").read_text()
CAPACITY = (REPO_ROOT / "docs/CAPACITY.md").read_text()
PERSISTENCE = (REPO_ROOT / "docs/PERSISTENCE_BACKENDS.md").read_text()
POSTGRES_BACKEND = REPO_ROOT / "backend/core/postgres_backend.py"


def test_file_backend_declares_incremental_writes_and_transactions_only():
    caps = FileGraphPersistenceBackend(REPO_ROOT / "unused.json").capabilities()
    assert caps.incremental_writes is True
    assert caps.transactions is True
    assert caps.change_notification is False


def test_file_backend_selection_returns_none_so_graphstorage_builds_its_own():
    assert build_persistence_backend(AppConfig(graph_file="graph.json")) is None
    config = AppConfig(graph_file="graph.json", graph_backend="file")
    assert build_persistence_backend(config) is None


def test_vector_and_history_sidecars_are_gated_on_the_file_backend():
    for method in ("_init_embedding_sidecar", "_init_history_store"):
        source = inspect.getsource(getattr(storage_module.GraphStorage, method))
        assert (
            "not isinstance(self._persistence_backend, FileGraphPersistenceBackend)"
            in source
        ), f"{method} no longer returns early for a non-file backend"


def test_graph_name_is_a_constructor_default_that_no_caller_overrides():
    constructors = [
        node
        for node in ast.walk(ast.parse(POSTGRES_BACKEND.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    ]
    defaults = {}
    for init in constructors:
        args = init.args
        names = (
            [a.arg for a in args.args[-len(args.defaults) :]] if args.defaults else []
        )
        defaults.update(
            {n: d for n, d in zip(names, args.defaults) if isinstance(d, ast.Constant)}
        )
        defaults.update(
            {
                a.arg: d
                for a, d in zip(args.kwonlyargs, args.kw_defaults)
                if isinstance(d, ast.Constant)
            }
        )
    assert defaults.get("graph_name") is not None
    assert defaults["graph_name"].value == "graph"

    passed = []
    for root in ("backend", "scripts"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            if "tests" in path.parts or "test_" in path.name:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", ""))
                    == "PostgresGraphPersistenceBackend"
                    and any(k.arg == "graph_name" for k in node.keywords)
                ):
                    passed.append(str(path.relative_to(REPO_ROOT)))
    assert passed == [], f"graph_name is passed by: {passed}"


def test_connection_budget_formula_and_worked_example_agree_with_the_code():
    match = re.search(
        r"^DEFAULT_POOL_SIZE = (\d+)$", POSTGRES_BACKEND.read_text(), re.MULTILINE
    )
    assert match, "DEFAULT_POOL_SIZE is no longer a plain module constant"
    pool_size = int(match.group(1))

    for name, text in (
        ("PERSISTENCE_BACKENDS.md", PERSISTENCE),
        ("CAPACITY.md", CAPACITY),
    ):
        assert "instance_count × (pool_size + 1) × graph_count" in text, name
    assert f"`DEFAULT_POOL_SIZE = {pool_size}`" in CAPACITY

    replicas, graphs = 5, 4
    total = (pool_size + 1) * replicas * graphs
    assert f"(4 + 1) × 5 × 4 = {total}" in PERSISTENCE
    assert f"(4 + 1) × 5 × 4 = {total}" in CAPACITY
    assert total > 97, "the worked example must exceed a stock server's 97 connections"


def test_capacity_figures_quoted_by_the_adr_still_appear_in_capacity_md():
    for figure in ("10,069 B per node", "9,180 B per node", "~50 MB", "~61 MB"):
        assert figure in CAPACITY, f"CAPACITY.md no longer states {figure!r}"
    for figure in ("~10,069 B per node", "~9,180 B", "~50 MB", "~61 MB"):
        assert figure in ADR, f"ADR 0009 no longer quotes {figure!r}"


def test_one_graph_per_core_process_is_stated_in_the_backend_contract():
    contract = PERSISTENCE.split("## The snapshot contract")[0]
    assert "One graph per core process" in contract
    assert "adr/0009-dual-mode-graph-storage-seam.md" in contract
