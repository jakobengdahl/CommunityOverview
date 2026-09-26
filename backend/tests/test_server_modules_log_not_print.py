"""
Server-side modules report through their module logger, not print().

A print() goes to stdout with no level, no logger name and no timestamp, so an
operator cannot filter a failed checkpoint apart from routine chatter. These
tests pin the level each converted report is logged at, and that it does not
also reach stdout.
"""

import ast
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from backend.agents.config import AgentsSettings
from backend.core.embedding_sidecar import FileEmbeddingSidecar
from backend.core.events.dispatcher import EventDispatcher
from backend.core.events.models import (
    EntityData,
    EntityKind,
    Event,
    EventContext,
    EventType,
)
from backend.core.storage_events import emit_event
from backend.core.vector_store import VectorStore
from backend.federation.config import load_federation_config

REPO_ROOT = Path(__file__).resolve().parents[2]

CONVERTED_MODULES = (
    "backend/agents/config.py",
    "backend/core/embedding_sidecar.py",
    "backend/core/events/dispatcher.py",
    "backend/core/postgres_backend.py",
    "backend/core/storage.py",
    "backend/core/storage_backends.py",
    "backend/core/storage_events.py",
    "backend/core/vector_store.py",
    "backend/federation/config.py",
)


def _logged(caplog, capsys, logger_name, level):
    """Messages `logger_name` logged at exactly `level`, after checking that
    none of them also went to stdout."""
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name and record.levelno == level
    ]
    out = capsys.readouterr().out
    leaked = [message for message in messages if message in out]
    assert not leaked, f"a report went to stdout: {leaked}"
    return messages


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


# Every way of writing to stdout that bypasses the logger.
_STDOUT_CALLS = {
    "print",
    "builtins.print",
    "sys.stdout.write",
    "sys.__stdout__.write",
    "sys.stdout.buffer.write",
    "sys.__stdout__.buffer.write",
    "sys.stdout.writelines",
    "sys.__stdout__.writelines",
    "sys.stdout.buffer.writelines",
    "sys.__stdout__.buffer.writelines",
}

# Names an alias can stand for on its way to one of the calls above. Only
# these are followed: an alias to anything else is not recorded, so it can
# never re-route a literal `print` or `sys.stdout.write` away from the guard.
_STDOUT_ROUTES = _STDOUT_CALLS | {
    "builtins",
    "sys",
    "sys.stdout",
    "sys.__stdout__",
    "sys.stdout.buffer",
    "sys.__stdout__.buffer",
    "os",
    "os.write",
    "os.writev",
}

# A module's attributes include the modules it imported, so `os.sys` is `sys`
# and a path through any module to one of these reaches the same object.
_ROUTE_MODULES = {"builtins", "os", "sys"}

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(scope):
    """Every node in `scope`, not descending into the scopes nested in it."""
    nodes, stack = [], list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        nodes.append(node)
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return nodes


def _expansions(dotted, aliases, seen=frozenset()):
    """Every name `dotted` may stand for: itself, and each expansion of its
    first component through every route that name may be bound to."""
    if not dotted:
        return set()
    parts = dotted.split(".")
    names = {dotted}
    for i in range(1, len(parts)):
        if parts[i] in _ROUTE_MODULES:
            # The first such suffix's own expansion reaches every later one;
            # recursing on each as well is exponential in their number.
            names |= _expansions(".".join(parts[i:]), aliases, seen)
            break
    head, _, rest = dotted.partition(".")
    if head not in seen:
        for route in aliases.get(head, ()):
            expanded = route + (f".{rest}" if rest else "")
            names |= _expansions(expanded, aliases, seen | {head})
    return names


def _chains(expr):
    """The dotted name `expr` is, or else every outermost dotted name inside
    it: a name bound to `sys.stdout if c else x`, or unpacked from
    `(sys.stdout, x)`, may stand for any of them."""
    if expr is None or isinstance(expr, _SCOPES):
        return []
    dotted = _dotted(expr)
    if dotted is not None:
        return [dotted]
    return [chain for child in ast.iter_child_nodes(expr) for chain in _chains(child)]


def _targets(target):
    """Every name a binding target binds, through tuples, lists and stars."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _targets(element)]
    if isinstance(target, ast.Starred):
        return _targets(target.value)
    return []


def _bound(targets, value):
    return [
        (name, chain)
        for t in targets
        for name in _targets(t)
        for chain in _chains(value)
    ]


def _bindings(node):
    """(local name, dotted name it is bound to, or None) for each name `node`
    binds: imports, plain, annotated, unpacking and walrus assignments, loop,
    comprehension and `with` targets, and `match` captures. An augmented
    assignment (`out += x`) is not followed: it rebinds a name to the result
    of an operator, not to what the right-hand side names. A name bound to an expression rather than to a plain
    dotted name is paired with every dotted name in it. An `except` target is
    left out: it binds the exception raised, which no stdout route is."""
    if isinstance(node, ast.Import):
        return [(name.asname, name.name) for name in node.names if name.asname]
    if isinstance(node, ast.ImportFrom):
        if not node.module or node.level:
            return [(name.asname or name.name, None) for name in node.names]
        if [name.name for name in node.names] == ["*"]:
            # Binds whatever the module exports, so every attribute of it a
            # stdout route goes through.
            prefix = f"{node.module}."
            return [
                (route[len(prefix) :], route)
                for route in _STDOUT_ROUTES
                if route.startswith(prefix) and "." not in route[len(prefix) :]
            ]
        return [
            (name.asname or name.name, f"{node.module}.{name.name}")
            for name in node.names
        ]
    if isinstance(node, ast.Assign):
        return _bound(node.targets, node.value)
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        return _bound([node.target], node.value)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return _bound([node.target], node.iter)
    if isinstance(node, ast.Match):
        captures = [
            name
            for case in node.cases
            for pattern in ast.walk(case.pattern)
            for name in (
                getattr(pattern, "name", None),
                getattr(pattern, "rest", None),
            )
            if name
        ]
        return [(name, chain) for name in captures for chain in _chains(node.subject)]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [
            binding
            for item in node.items
            for binding in _bound([item.optional_vars], item.context_expr)
            if item.optional_vars is not None
        ]
    return []


def _parameter_bindings(scope):
    """(parameter, dotted name) for each dotted name in a defaulted
    parameter's default: `def f(out=sys.stdout)` binds `out` inside `f`."""
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return []
    args = scope.args
    positional = args.posonlyargs + args.args
    pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults))
    pairs += [
        (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
    ]
    return [(arg.arg, chain) for arg, default in pairs for chain in _chains(default)]


def _scope_aliases(scope, nodes, inherited):
    """Local name -> every stdout route it may stand for in this scope.

    May-alias and fail-closed: a name bound to a stdout route anywhere in a
    scope counts throughout it and in every scope nested in it, in any order
    and even where it is also bound to something else. Routes are only ever
    added - to what the name inherited as well as to each other - never
    replaced, and the bindings are applied until nothing changes, so a chain
    is followed whichever order its links appear in. Undoing an alias on a
    rebinding would have to know which expressions a nested scope evaluates
    in its parent (defaults, decorators, bases) and every binding form Python
    has; getting either wrong hides a real write. Erring the other way costs a
    false flag, which fails loudly and is fixed by renaming.
    """
    aliases = {name: set(routes) for name, routes in inherited.items()}
    bindings = _parameter_bindings(scope)
    for node in nodes:
        bindings.extend(_bindings(node))
    changed = True
    while changed:
        changed = False
        for name, target in bindings:
            for route in _expansions(target, aliases) & _STDOUT_ROUTES:
                if route != name and route not in aliases.setdefault(name, set()):
                    aliases[name].add(route)
                    changed = True
    return aliases


def _writes_to_stdout(call, aliases):
    names = _expansions(_dotted(call.func), aliases)
    if names & _STDOUT_CALLS:
        return True
    # File descriptor 1 is stdout whatever sys.stdout has been swapped for.
    return (
        bool(names & {"os.write", "os.writev"})
        and bool(call.args)
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 1
    )


def _collect(scope, inherited, found, escaped):
    nodes = _own_nodes(scope)
    aliases = _scope_aliases(scope, nodes, inherited)
    # A name declared global or nonlocal binds in an enclosing scope, which
    # has already been walked by now: record its routes for the next pass.
    for node in nodes:
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                escaped.setdefault(name, set()).update(aliases.get(name, ()))
    # Class scopes too, though a method body cannot see a class body's names:
    # its decorators, defaults and bases are evaluated in that body, and
    # telling the two apart is the kind of precision the fail-closed rule
    # above gives up.
    for node in nodes:
        if isinstance(node, ast.Call) and _writes_to_stdout(node, aliases):
            found.append(node.lineno)
        if isinstance(node, _SCOPES):
            _collect(node, aliases, found, escaped)


def _stdout_calls(source):
    """Walked until no global or nonlocal binding adds a route, each pass
    seeding the module scope with what the last one found escaping. A
    nonlocal's routes land at module level too, so they count in every scope
    rather than only the enclosing function's - the fail-closed direction."""
    tree = ast.parse(source)
    escaped = {}
    while True:
        before = {name: set(routes) for name, routes in escaped.items()}
        found = []
        _collect(tree, before, found, escaped)
        if escaped == before:
            return sorted(found)


@pytest.mark.parametrize(
    "source",
    [
        "print('x')",
        "import builtins\nbuiltins.print('x')",
        "import sys\nsys.stdout.write('x')",
        "import sys as s\ns.stdout.write('x')",
        "from sys import stdout\nstdout.write('x')",
        "from sys import stdout as out\nout.write('x')",
        "import sys\nsys.__stdout__.write('x')",
        "from sys import __stdout__\n__stdout__.write('x')",
        "import os\nos.write(1, b'x')",
        "from os import write\nwrite(1, b'x')",
        "say = print\nsay('x')",
        "from builtins import print as say\nsay('x')",
        "import sys\nout = sys.stdout\nout.write('x')",
        # A literal name is caught whatever an import or an assignment
        # elsewhere in the module rebinds it to.
        "from rich import print\nprint('x')",
        "import sys\ndef f():\n    print = sys.stderr.write\nprint('x')",
        "import sys\ndef f():\n    sys = foo.bar\nsys.stdout.write('x')",
        # Chains: an alias built on an alias defined before it.
        "import sys as s\nout = s.stdout\nout.write('x')",
        "from sys import stdout as o\nx = o\nx.write('x')",
        "import sys\na = sys\nb = a.stdout\nb.write('x')",
        "def f():\n    import sys as s\n    out = s.stdout\n    out.write('x')",
        "import sys\ndef f():\n    out = sys.stdout\n    def g():\n        out.write('x')",
        "import sys\nout: object = sys.stdout\nout.write('x')",
        "import sys\nsys.stdout.buffer.write(b'x')",
        # Evaluated in the enclosing scope, so a rebinding inside the
        # function must not hide it - nor may any rebinding at all.
        "import sys\nout = sys.stdout\ndef f(out, x=out.write('x')): pass",
        "import sys\nout = sys.stdout\ndef f():\n    out = open('y')\n"
        "    out.write('x')",
        # A class body's aliases, used where the body evaluates them.
        "import sys\nclass A:\n    out = sys.stdout\n"
        "    def m(self, x=out.write('x')): pass",
        "import sys\nclass A:\n    w = sys.stdout.write\n    class B(w('x')): pass",
        "import sys\ndef f(out=sys.stdout):\n    out.write('x')",
        "import sys\ndef f(*, out=sys.stdout):\n    out.write('x')",
        "import sys\nsys.stdout.writelines(['x'])",
        # Every route a name may stand for counts, whatever else binds it
        # and in whatever order a chain's links appear.
        "import sys\nout = sys.stdout\ndef f(out=sys, x=out.write('x')): pass",
        "import sys\nout = sys\nout = sys.stdout\nout.stdout.write('x')",
        "import os\nw = print\nw = os\nw.write(1, b'x')",
        "import sys\nw = out.write\nout = sys.stdout\ndef f():\n    w('x')",
        "from sys import *\nstdout.write('x')",
        "from sys import *\n__stdout__.buffer.write(b'x')",
        "from os import *\nwrite(1, b'x')",
        "import os\nos.writev(1, [b'x'])",
        "from os import writev\nwritev(1, [b'x'])",
        # Each binding form the guard follows, not only a plain name on the left
        # of `=`.
        "import sys\nout, err = sys.stdout, sys.stderr\nout.write('x')",
        "import sys\n[out, err] = [sys.stdout, sys.stderr]\nout.write('x')",
        "import sys\nerr, *out = sys.stderr, sys.stdout\nout.write('x')",
        "import sys\nif (out := sys.stdout):\n    out.write('x')",
        "import sys\nfor out in (sys.stdout,):\n    out.write('x')",
        "import sys\n[out.write('x') for out in [sys.stdout]]",
        "import sys\nwith sys.stdout as out:\n    out.write('x')",
        "import sys\nasync def f():\n    async for out in g(sys.stdout):\n"
        "        out.write('x')",
        "import sys\nasync def f():\n    async with g(sys.stdout) as out:\n"
        "        out.write('x')",
        "import sys\ndef f(*, out=sys.stdout if c else x):\n    out.write('x')",
        "import sys\nmatch sys.stdout:\n    case out:\n        out.write('x')",
        "import sys\nmatch [sys.stdout]:\n    case [*out]:\n        out.write('x')",
        "import sys\nmatch {1: sys.stdout}:\n    case {**out}:\n        out.write('x')",
        "import sys\nout = sys.stdout if c else x\nout.write('x')",
        "import sys\nout = x or sys.stdout\nout.write('x')",
        "import sys\ndef f(out=sys.stdout if c else x):\n    out.write('x')",
        # Bound in a nested scope, for the scope that declares it.
        "import sys\ndef f():\n    global out\n    out = sys.stdout\nout.write('x')",
        "def f():\n    global out\n    import sys as s\n    out = s.stdout\n"
        "out.write('x')",
        "import sys\ndef f():\n    out = None\n    def g():\n        nonlocal out\n"
        "        out = sys.stdout\n    out.write('x')",
        # Reached through another module's attributes.
        "import os\nos.sys.stdout.write('x')",
        "import logging\nlogging.sys.__stdout__.write('x')",
        "import os\nm = os.sys\nm.stdout.write('x')",
        # Without the time growing exponentially in how many such modules
        # the path runs through.
        "import os\nx" + ".os" * 60 + ".write(1, b'x')",
    ],
)
def test_the_guard_catches_each_way_of_writing_to_stdout(source):
    assert _stdout_calls(source) == [source.count("\n") + 1]


@pytest.mark.parametrize(
    "source",
    [
        # Evaluated where the function is defined, not in its body.
        "import sys\nw = sys.stdout.write\n@deco(w('x'))\ndef f(w): pass",
        "import sys\nout = sys\ndef f(x=out.stdout.write('x')):\n    out = sys.stdout",
    ],
)
def test_the_guard_catches_a_write_where_the_function_is_defined(source):
    assert _stdout_calls(source) == [3]


@pytest.mark.parametrize(
    "source",
    [
        "import logging, sys\nlogging.getLogger().warning('x')\nsys.stderr.write('x')",
        "from sys import stderr\nstderr.write('x')",
        "import os\nos.write(2, b'x')",
        "import os\nos.write(fd, b'x')",
        "import os\nos.writev(2, [b'x'])",
        # A longer dotted name that merely starts with a route does not bind
        # the route.
        "import sys\nf = open(sys.argv[1])\nf.write('x')",
        "import sys\nwith open(sys.argv[1]) as out:\n    out.write('x')",
        "import sys\nfor out in (sys.stderr,):\n    out.write('x')",
        "handle = open('f', 'w')\nhandle.write('x')",
        # An alias to stdout in one function does not reach a same-named
        # handle in another.
        "import sys\ndef b():\n    f = open('x')\n    f.write('y')\n"
        "def a():\n    f = sys.stdout",
    ],
)
def test_the_guard_ignores_logger_calls_and_other_streams(source):
    assert _stdout_calls(source) == []


@pytest.mark.parametrize("module", CONVERTED_MODULES)
def test_converted_module_has_no_print_call(module):
    calls = _stdout_calls((REPO_ROOT / module).read_text(encoding="utf-8"))
    assert not calls, f"{module} writes to stdout at line(s) {calls}; use its logger"


class TestStorageEvents:
    LOGGER = "backend.core.storage_events"

    def _emit(self, **overrides):
        kwargs = dict(
            history_store=None,
            system_listeners=[],
            events_enabled=False,
            event_dispatcher=None,
            event_type=EventType.NODE_CREATE,
            entity_kind=EntityKind.NODE,
            entity_id="n1",
            entity_type="Actor",
        )
        kwargs.update(overrides)
        emit_event(**kwargs)

    def test_a_failed_history_write_is_a_warning(self, caplog, capsys):
        class _BrokenHistory:
            def append_event(self, event):
                raise OSError("history disk full")

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(history_store=_BrokenHistory())

        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("history disk full" in m for m in warnings), warnings

    def test_a_failing_system_listener_is_an_error(self, caplog, capsys):
        def _listener(event):
            raise RuntimeError("listener blew up")

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(system_listeners=[_listener])

        errors = _logged(caplog, capsys, self.LOGGER, logging.ERROR)
        assert any("listener blew up" in m for m in errors), errors

    def test_a_failed_dispatch_is_a_warning(self, caplog, capsys):
        class _BrokenDispatcher:
            def dispatch(self, event):
                raise RuntimeError("dispatcher down")

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(events_enabled=True, event_dispatcher=_BrokenDispatcher())

        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("dispatcher down" in m for m in warnings), warnings

    def test_per_event_chatter_is_debug_only(self, caplog, capsys):
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit()

        debug = _logged(caplog, capsys, self.LOGGER, logging.DEBUG)
        assert any("Skipped" in m for m in debug), debug
        assert not [
            r
            for r in caplog.records
            if r.name == self.LOGGER and r.levelno > logging.DEBUG
        ]

    def test_emitting_to_a_working_dispatcher_is_debug_only(self, caplog, capsys):
        dispatched = []

        class _Dispatcher:
            def dispatch(self, event):
                dispatched.append(event)

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(events_enabled=True, event_dispatcher=_Dispatcher())

        assert len(dispatched) == 1
        debug = _logged(caplog, capsys, self.LOGGER, logging.DEBUG)
        assert any("Emitting" in m and "n1" in m and "Actor" in m for m in debug), debug
        assert not [
            r
            for r in caplog.records
            if r.name == self.LOGGER and r.levelno > logging.DEBUG
        ]


class TestDispatcher:
    LOGGER = "backend.core.events.dispatcher"

    class _Node:
        def __init__(self, id, name, node_type, metadata=None):
            self.id = id
            self.name = name
            self.type = node_type
            self.metadata = metadata or {}

    class _Storage:
        def __init__(self, nodes):
            self.nodes = {n.id: n for n in nodes}

    def _dispatcher(self):
        subscription = self._Node(
            "sub-1",
            "Actor watcher",
            "EventSubscription",
            {
                "filters": {
                    "target": {"entity_kind": "node", "node_types": ["Actor"]},
                    "operations": ["create"],
                },
                "delivery": {"webhook_url": "https://example.com/hook"},
            },
        )
        delivered = []
        dispatcher = EventDispatcher(
            self._Storage([subscription]),
            on_deliver=lambda event, url: delivered.append(url),
        )
        return dispatcher, delivered

    @staticmethod
    def _event():
        return Event(
            event_type=EventType.NODE_CREATE,
            origin=EventContext(),
            entity=EntityData(kind=EntityKind.NODE, id="n1", type="Actor"),
        )

    def test_routine_dispatch_chatter_is_debug_only(self, caplog, capsys):
        dispatcher, delivered = self._dispatcher()
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        assert dispatcher.dispatch(self._event()) == 1

        assert delivered == ["https://example.com/hook"]
        debug = _logged(caplog, capsys, self.LOGGER, logging.DEBUG)
        assert any("Loaded 1 EventSubscription" in m for m in debug), debug
        assert any("Dispatching to 1 subscription" in m for m in debug), debug
        assert any("'Actor watcher' matches=True" in m for m in debug), debug
        chatter = ("Loaded", "Dispatching", "matches=")
        louder = [
            r.getMessage()
            for r in caplog.records
            if r.name == self.LOGGER
            and r.levelno > logging.DEBUG
            and any(word in r.getMessage() for word in chatter)
        ]
        assert not louder, louder


class TestFederationConfig:
    LOGGER = "backend.federation.config"

    @pytest.fixture(autouse=True)
    def _isolated_env(self):
        keys = (
            "FEDERATION_FILE",
            "GRAPH_FEDERATION_CONFIG",
            "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR",
        )
        with patch.dict(os.environ, {}, clear=False):
            for key in keys:
                os.environ.pop(key, None)
            yield

    def test_a_missing_file_is_info(self, tmp_path, caplog, capsys):
        os.environ["FEDERATION_FILE"] = str(tmp_path / "missing.json")
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        load_federation_config()

        info = _logged(caplog, capsys, self.LOGGER, logging.INFO)
        assert any("not found" in m for m in info), info

    def test_an_invalid_file_is_a_warning(self, tmp_path, caplog, capsys):
        path = tmp_path / "federation.json"
        path.write_text("{not json", encoding="utf-8")
        os.environ["FEDERATION_FILE"] = str(path)
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        config = load_federation_config()

        assert config.federation.enabled is False
        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("Invalid federation config" in m for m in warnings), warnings

    def test_a_loaded_file_is_info(self, tmp_path, caplog, capsys):
        path = tmp_path / "federation.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        os.environ["FEDERATION_FILE"] = str(path)
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        load_federation_config()

        info = _logged(caplog, capsys, self.LOGGER, logging.INFO)
        assert any("Loaded federation configuration" in m for m in info), info


def test_unparseable_mcp_integrations_is_a_warning(caplog, capsys):
    logger_name = "backend.agents.config"
    caplog.set_level(logging.DEBUG, logger=logger_name)
    env = {"AGENTS_ENABLED": "true", "MCP_INTEGRATIONS": "{not json"}

    with patch.dict(os.environ, env, clear=True):
        AgentsSettings.from_env()

    warnings = _logged(caplog, capsys, logger_name, logging.WARNING)
    assert any("MCP_INTEGRATIONS" in m for m in warnings), warnings


def test_unloadable_model_profiles_is_a_warning(caplog, capsys):
    logger_name = "backend.agents.config"
    caplog.set_level(logging.DEBUG, logger=logger_name)

    with (
        patch.dict(os.environ, {"AGENTS_ENABLED": "true"}, clear=True),
        patch(
            "backend.config.config_loader.get_model_profiles",
            side_effect=RuntimeError("schema unreadable"),
        ),
    ):
        settings = AgentsSettings.from_env()

    assert settings.model_profiles == []
    warnings = _logged(caplog, capsys, logger_name, logging.WARNING)
    assert any("model profiles" in m and "schema unreadable" in m for m in warnings), (
        warnings
    )


def test_a_replaced_corrupt_sidecar_is_a_warning(tmp_path, caplog, capsys):
    logger_name = "backend.core.embedding_sidecar"
    path = tmp_path / "embeddings.bin"
    path.write_bytes(b"corrupted beyond recognition")
    caplog.set_level(logging.DEBUG, logger=logger_name)

    FileEmbeddingSidecar(path, owns_path=True).save(
        {"n1": np.ones(4, dtype=np.float32)}
    )

    spoiled = tmp_path / "embeddings.bin.corrupt"
    assert spoiled.read_bytes() == b"corrupted beyond recognition"
    warnings = _logged(caplog, capsys, logger_name, logging.WARNING)
    assert any(
        f"{path} was not a readable sidecar" in m and f"moved it to {spoiled}" in m
        for m in warnings
    ), warnings


class TestVectorStore:
    LOGGER = "backend.core.vector_store"

    def test_dropping_mismatched_vectors_is_a_warning(self, caplog, capsys):
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        VectorStore().load_vectors(
            {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [1.0, 0.0, 0.0]}
        )

        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("dropped 1 embedding(s)" in m for m in warnings), warnings

    def test_a_changed_model_width_is_a_warning(self, caplog, capsys):
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0]})
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        store._absorb({"b": [1.0, 0.0, 0.0]})

        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("dimension changed from 2 to 3" in m for m in warnings), warnings

    def test_a_rebuilt_index_is_info(self, caplog, capsys):
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        VectorStore().rebuild_index([])

        info = _logged(caplog, capsys, self.LOGGER, logging.INFO)
        assert any("index rebuilt with 0 embeddings" in m for m in info), info
