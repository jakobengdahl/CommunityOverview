"""Vectors for staged entries (docs/SESSION_OVERLAY_CONTRACT.md §8.3).

A staged node create or update has content the main index never saw, so a
composing semantic read needs a vector for it that the main index does not
hold. These are derived data: kept per instance, in memory, never persisted
and never written into the main index (I1, I12).

Each vector is held under the exact text it was embedded from, so an entry
whose content changed since is embedded again rather than answered from its
old vector, and one whose entry left the layer is dropped the next time the
session's vectors are read. Embedding happens in the background when an entry
is staged on this instance, when the optional ML stack is installed; a
composing read embeds on demand whatever is still missing - so its results
never omit a staged entry - and yields no layer vectors at all on a base
install, where the main index answers nothing either.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Tuple

if TYPE_CHECKING:
    from .models import Node
    from .vector_store import VectorStore

logger = logging.getLogger(__name__)

# Sessions whose vectors are kept at once. A layer is capped (§6.4), so this
# bounds the memory, and a session dropped here only costs a re-embed.
MAX_SESSIONS = 64


def embeddings_installed() -> bool:
    """Whether the optional ML stack that embeds text is installed."""
    return importlib.util.find_spec("sentence_transformers") is not None


class LayerVectors:
    """The staged entries' vectors of every session read on this instance."""

    def __init__(self, vector_store: "VectorStore"):
        self._vector_store = vector_store
        self._lock = threading.Lock()
        # session id -> node id -> (embedded text, vector)
        self._sessions: "OrderedDict[str, Dict[str, Tuple[str, Any]]]" = OrderedDict()
        self._executor: "ThreadPoolExecutor | None" = None

    def _text(self, node: "Node") -> str:
        return self._vector_store._get_text_representation(node)

    def vectors(
        self,
        session_id: str,
        nodes: Iterable["Node"],
        live_ids: Iterable[str],
    ) -> Dict[str, Any]:
        """The vector of each of ``nodes``, embedding any not held yet.

        ``live_ids`` names every node entry the layer holds now; vectors held
        for any other id are dropped. A node whose vector cannot be made - no
        ML stack, a model failure - is left out.
        """
        nodes = list(nodes)
        with self._lock:
            held = dict(self._sessions.get(session_id) or {})
        result: Dict[str, Any] = {}
        missing: List["Node"] = []
        texts: Dict[str, str] = {}
        for node in nodes:
            text = texts[node.id] = self._text(node)
            known = held.get(node.id)
            if known is not None and known[0] == text:
                result[node.id] = known[1]
            else:
                missing.append(node)
        computed: Dict[str, Any] = {}
        if missing:
            try:
                computed = self._vector_store.compute_node_embeddings(missing)
            except ImportError:
                computed = {}
            except Exception as exc:
                logger.warning(f"could not embed staged entries: {exc}")
                computed = {}
        result.update(computed)
        live = set(live_ids)
        with self._lock:
            current = dict(self._sessions.get(session_id) or {})
            current.update({nid: (texts[nid], vec) for nid, vec in computed.items()})
            kept = {nid: v for nid, v in current.items() if nid in live}
            if kept:
                self._sessions[session_id] = kept
                self._sessions.move_to_end(session_id)
                while len(self._sessions) > MAX_SESSIONS:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.pop(session_id, None)
        return result

    def schedule(
        self,
        session_id: str,
        load: Callable[[], Tuple[List["Node"], List[str]]],
    ) -> None:
        """Embed a session's staged nodes in the background, when this
        instance can embed at all. ``load`` returns ``(nodes, live_ids)`` as
        :meth:`vectors` takes them, read when the work runs."""
        if not embeddings_installed():
            return
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="layer-vectors"
                )
            executor = self._executor

        def run() -> None:
            try:
                nodes, live_ids = load()
                self.vectors(session_id, nodes, live_ids)
            except Exception as exc:
                logger.warning(f"background embedding of staged entries failed: {exc}")

        executor.submit(run)

    def held(self, session_id: str) -> Dict[str, Tuple[str, Any]]:
        with self._lock:
            return dict(self._sessions.get(session_id) or {})
