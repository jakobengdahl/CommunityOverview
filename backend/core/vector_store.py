"""
Vector Store for embeddings.
Handles generating, storing, and searching embeddings for nodes.

This module is part of graph_core - the core graph storage layer.

Semantic *search* over existing embeddings needs only numpy (part of the base
requirements). *Generating* embeddings from text needs sentence-transformers
with CPU-only PyTorch, which are optional ML extras (requirements-ml.txt).
When those extras are absent, embedding generation is skipped gracefully and
callers fall back to name-based similarity.

Imports are deferred (lazy) so the module loads fast and so the absence of the
optional ML stack surfaces only when embedding generation is actually attempted.
"""

from typing import List, Dict, Optional, Tuple, Any

from .models import Node

# Global references for lazy-loaded modules
_np = None
_SentenceTransformer = None


def _ensure_numpy():
    """Lazy load numpy"""
    global _np
    if _np is None:
        import numpy as np

        _np = np
    return _np


def _ensure_sentence_transformers():
    """Lazy load sentence-transformers (optional ML extra)"""
    global _SentenceTransformer
    if _SentenceTransformer is None:
        from sentence_transformers import SentenceTransformer

        _SentenceTransformer = SentenceTransformer
    return _SentenceTransformer


def dominant_dimension(vectors: Dict[str, Any]) -> Optional[int]:
    """The dimension most of these vectors share, or None if there are none.

    A tie resolves to the dimension seen first: max() keeps the first maximal
    key and the dict is insertion-ordered. Deciding a tie by width instead
    would let one stray wide vector outrank an equally-common correct one.
    """
    if not vectors:
        return None

    counts: Dict[int, int] = {}
    for vector in vectors.values():
        counts[len(vector)] = counts.get(len(vector), 0) + 1
    return max(counts, key=counts.get)


def matching_dimension(
    vectors: Dict[str, Any], dimension: Optional[int]
) -> Dict[str, Any]:
    """Keep only the vectors of the given dimension.

    The caller decides which dimension is authoritative rather than letting a
    vote decide it — a majority of stale vectors must never evict the current
    ones.
    """
    if dimension is None:
        return {}
    return {
        node_id: vector
        for node_id, vector in vectors.items()
        if len(vector) == dimension
    }


def _cosine_to_unit_rows(query, unit_matrix):
    """Cosine similarity of a (1, d) query against ALREADY UNIT-LENGTH rows.

    The rows are normalised once, when the index changes, rather than once per
    query - see VectorStore._update_matrix. Doing it here instead allocated a
    full copy of the matrix on every search: 147 MiB per query at 100k nodes of
    width 384, for a result that is the same on every query until the index
    changes.

    Implemented with numpy so semantic search does not depend on scikit-learn.
    """
    np = _ensure_numpy()
    query_norm = query / (np.linalg.norm(query, axis=1, keepdims=True) + 1e-12)
    return (query_norm @ unit_matrix.T)[0]


class VectorStore:
    """
    Manages vector embeddings for graph nodes.
    Uses sentence-transformers for generating embeddings (optional ML extra)
    and numpy for cosine similarity search.

    This class owns the vectors. They are held as float32 numpy rows and
    persisted by GraphStorage into a binary sidecar, not as a field on the
    serialised Node — see backend/core/embedding_sidecar.py. ``Node.embedding``
    remains on the model so a graph written before that split still loads, but
    nothing writes it back.

    ``revision`` increments on every change to the index. GraphStorage compares
    it against the revision it last persisted to decide whether a save needs to
    rewrite the sidecar at all.

    INVARIANT: every vector in the index has the same width. numpy cannot stack
    rows of differing width, so a mixed index breaks the matrix rebuild, and
    from there the sidecar write, semantic search and node deletion — each of
    which then has to guess a recovery. The invariant is enforced here, at the
    only places a vector can enter, so no consumer has to.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = None
        self.embeddings: Dict[str, Any] = {}  # node_id -> embedding (numpy array)
        self.node_ids: List[
            str
        ] = []  # ordered list of node ids corresponding to unit_matrix rows
        # The index as UNIT-LENGTH rows, not as the vectors were given. Search
        # needs them normalised and nothing else reads this; keeping the raw
        # stack as well would double the largest allocation this class makes,
        # on an instance whose ceiling is memory. The vectors as supplied are
        # still in `embeddings`, which is what persistence and export read.
        self.unit_matrix: Optional[Any] = None  # numpy array
        self.revision: int = 0

    @property
    def dimension(self) -> Optional[int]:
        """Width of the vectors in the index, or None when it is empty."""
        for vector in self.embeddings.values():
            return len(vector)
        return None

    def _load_model(self):
        """Lazy load the model"""
        if self.model is None:
            SentenceTransformer = _ensure_sentence_transformers()
            print(f"Loading embedding model: {self.model_name}...")
            self.model = SentenceTransformer(self.model_name)
            print("Model loaded.")

    def preload_model(self):
        """
        Preload the embedding model in a background thread.
        Call at startup to avoid slow first request.
        """
        import threading

        def _load():
            try:
                self._load_model()
                print(f"Embedding model '{self.model_name}' preloaded in background.")
            except Exception as e:
                print(f"Warning: Background model preload failed: {e}")

        t = threading.Thread(target=_load, name="embedding-preload", daemon=True)
        t.start()

    def rebuild_index(self, nodes: List[Node]):
        """Rebuild the search index from vectors carried on the node objects.

        Only pre-split graphs carry them there; GraphStorage loads from the
        sidecar via load_vectors() and takes the vectors off the node objects
        itself, so nothing in the application calls this any more. It stays as
        the public way for an embedder outside this package to build an index
        from nodes it already holds.
        """
        self.load_vectors(
            {node.id: node.embedding for node in nodes if node.embedding is not None}
        )
        print(f"VectorStore index rebuilt with {len(self.embeddings)} embeddings")

    def load_vectors(self, vectors: Dict[str, Any]) -> None:
        """Replace the index with vectors read back from persistence.

        Callers that know which source is authoritative select the dimension
        before calling. This is the last-resort guard that keeps the invariant
        true whatever they pass.
        """
        np = _ensure_numpy()
        kept = matching_dimension(vectors, dominant_dimension(vectors))
        if len(kept) != len(vectors):
            print(
                f"Warning: dropped {len(vectors) - len(kept)} embedding(s) whose "
                f"width did not match the rest of the index"
            )
        self.embeddings = {
            node_id: np.asarray(vector, dtype=np.float32)
            for node_id, vector in kept.items()
        }
        self._update_matrix()

    def _absorb(self, vectors: Dict[str, Any]) -> None:
        """Add freshly generated vectors, resetting the index if the model's
        output width changed.

        A width change means the embedding model changed. The vectors already
        held cannot be compared with the new ones, nor with any query embedded
        by the new model, so they are already dead — keeping them would only
        break the index. Dropping them is what makes the change survivable;
        they come back as each node is next embedded.
        """
        if not vectors:
            return

        np = _ensure_numpy()
        widths = {len(vector) for vector in vectors.values()}
        if len(widths) > 1:
            raise ValueError(
                f"one batch of generated embeddings has mixed widths {sorted(widths)}"
            )

        width = widths.pop()
        current = self.dimension
        if current is not None and current != width:
            print(
                f"Warning: embedding dimension changed from {current} to {width}; "
                f"discarding {len(self.embeddings)} vector(s) that can no longer be "
                f"compared. Re-run scripts/generate_embeddings.py to rebuild them."
            )
            self.embeddings = {}

        for node_id, vector in vectors.items():
            self.embeddings[node_id] = np.asarray(vector, dtype=np.float32)
        self._update_matrix()

    def export_vectors(self) -> Dict[str, Any]:
        """Return the index for persistence.

        A shallow copy is enough: rows are replaced, never mutated in place.
        """
        return dict(self.embeddings)

    def get_vector_list(self, node_id: str) -> Optional[List[float]]:
        """Return a node's vector as a JSON-serialisable list, or None."""
        vector = self.embeddings.get(node_id)
        return None if vector is None else vector.tolist()

    def _update_matrix(self):
        """Update the numpy matrix for vectorized operations.

        Every path that changes the index goes through here, so this is also
        where the persistence revision is bumped - and where the rows are
        normalised, for the same reason: it is the one place the index can
        change, so it is the one place the normalisation can go stale.

        Row order follows `embeddings` insertion order and is what `node_ids`
        indexes. A stacking order that disagreed with `node_ids` would return
        one node's score wearing another node's id.
        """
        self.revision += 1

        if not self.embeddings:
            self.node_ids = []
            self.unit_matrix = None
            return

        np = _ensure_numpy()
        self.node_ids = list(self.embeddings.keys())
        # Stack embeddings into a matrix
        matrix = np.vstack([self.embeddings[nid] for nid in self.node_ids])
        # Normalised IN PLACE: vstack just built an array nobody else holds, so
        # dividing into it keeps the rebuild's peak at one matrix rather than
        # two. The epsilon keeps a zero row at zero instead of dividing by it.
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12
        self.unit_matrix = matrix

    def _get_text_representation(self, node: Node) -> str:
        """Create a text representation of the node for embedding"""
        # Combine name, aliases, description, summary, and tags
        # Tags and aliases are important for similarity search
        tags_text = " ".join(node.tags) if hasattr(node, "tags") and node.tags else ""
        aliases_text = (
            " ".join(node.aliases) if hasattr(node, "aliases") and node.aliases else ""
        )
        text = f"{node.name}. {aliases_text}. {node.description or ''}. {node.summary or ''}. {tags_text}"
        return text.strip()

    def generate_embedding(self, node: Node) -> List[float]:
        """Generate embedding for a single node and return as list"""
        self._load_model()
        text = self._get_text_representation(node)
        embedding = self.model.encode(text)
        return embedding.tolist()

    def update_node_embedding(self, node: Node):
        """Generate and store the embedding for a node."""
        self._absorb({node.id: self.generate_embedding(node)})

    def update_nodes_embeddings(self, nodes: List[Node]):
        """Update embeddings for multiple nodes in batch"""
        if not nodes:
            return

        self._load_model()
        texts = [self._get_text_representation(node) for node in nodes]
        embeddings = self.model.encode(texts)

        self._absorb({node.id: embedding for node, embedding in zip(nodes, embeddings)})

    def remove_node_embedding(self, node_id: str):
        """Remove embedding for a node"""
        if node_id in self.embeddings:
            del self.embeddings[node_id]
            self._update_matrix()

    def remove_nodes_embeddings(self, node_ids: List[str]):
        """Remove embeddings for multiple nodes"""
        changed = False
        for node_id in node_ids:
            if node_id in self.embeddings:
                del self.embeddings[node_id]
                changed = True

        if changed:
            self._update_matrix()

    def search(
        self,
        query_text: str = None,
        query_node: Node = None,
        limit: int = 5,
        threshold: float = 0.0,
    ) -> List[Tuple[str, float]]:
        """
        Search for similar nodes.
        Can search by query text or by existing node.

        `limit` of zero or less returns nothing. The slice this replaced said
        `results[:limit]`, which returned everything-but-one for -1 - slice
        arithmetic rather than an answer - and `limit` reaches here unguarded
        from search_graph over MCP. Note the lexical path still slices, so the
        two halves of one `search_graph(limit=...)` differ for a negative
        limit; both are degenerate, and only this half is defined.

        A score that is not a number is dropped rather than returned, which is
        what the `score >= threshold` filter this replaced did with one.

        Returns:
            List of (node_id, score) tuples, sorted by score descending.
        """
        if not self.embeddings or self.unit_matrix is None:
            return []

        np = _ensure_numpy()

        # Obtaining the query embedding may need the optional ML stack (to embed
        # query text or a not-yet-embedded node). If it is unavailable, degrade
        # to no semantic results rather than failing the whole search.
        try:
            if query_node:
                # If searching by node, check if we already have its embedding
                if query_node.id in self.embeddings:
                    query_embedding = self.embeddings[query_node.id]
                else:
                    query_embedding = self.generate_embedding(query_node)
            elif query_text:
                self._load_model()
                query_embedding = self.model.encode(query_text)
            else:
                return []
        except ImportError as e:
            print(
                f"Warning: semantic search unavailable (embedding model not installed): {e}"
            )
            return []

        # Reshape to (1, embedding_dim); handle both list and array inputs.
        # In the INDEX's dtype, which is what keeps this query-shaped rather
        # than index-shaped: `generate_embedding` returns a Python list, which
        # becomes float64, and a float64 query against a float32 matrix makes
        # numpy promote the whole matrix to compare them - measured at 308 MB
        # for one query at 100k rows of width 384. The query_text path never
        # saw it, because the model hands back float32 already.
        #
        # The scoring therefore happens at the index's width rather than being
        # promoted to float64, so a list-valued query's scores move by about
        # one float32 epsilon (measured: max 1.8e-7 at 100k x 384). Rows that
        # reorder are ones that differ by less than that - effective ties - and
        # the top of the ranking is unaffected; storing the index in float64 to
        # avoid it would double the largest allocation this class makes.
        query_embedding = np.asarray(query_embedding, dtype=self.unit_matrix.dtype)
        query_embedding = query_embedding.reshape(1, -1)

        # Calculate cosine similarity
        similarities = _cosine_to_unit_rows(query_embedding, self.unit_matrix)

        # Ordered in numpy rather than in Python, and walked only as far as the
        # caller asked for. The ordering is still over all n - that is what an
        # exact nearest-neighbour search is - but it happens in numpy instead
        # of over a list of n Python tuples. `stable` keeps equal scores in
        # index order, which is the order the list-and-sort this replaced
        # produced and therefore the order callers have been seeing.
        np_order = np.argsort(-similarities, kind="stable")

        results = []
        for idx in np_order:
            score = float(similarities[idx])
            # Spelled as the negation of the old `score >= threshold` filter
            # rather than as `score < threshold`, because the two differ on a
            # NaN: a NaN is neither above the threshold nor below it, so the
            # second admits it and the filter it replaced did not. A NaN score
            # is reachable from a single corrupt float in the sidecar, and it
            # reaches a caller that formats it as a percentage. argsort puts
            # NaN last, so stopping here is still the same set as filtering.
            if not (score >= threshold):
                break
            node_id = self.node_ids[idx]
            # If query was a node in the database, drop it (similarity 1.0).
            # It costs no slot because the count below is over `len(results)`
            # rather than over iterations - not because of where this sits;
            # swapping the two is behaviour-neutral, since the break can only
            # fire once the list is already full.
            if query_node is not None and node_id == query_node.id:
                continue
            # Counted BEFORE the append, which is what makes `limit=0` return
            # nothing rather than the first candidate - and `limit` is not
            # guarded on every road in here (search_graph over MCP does not
            # constrain it). Asking for none, or for a negative none, gets
            # none; the slice this replaced said `results[:limit]`, which
            # returned everything-but-one for -1, slice arithmetic rather than
            # an answer anyone wanted.
            if len(results) >= limit:
                break
            results.append((node_id, score))

        return results

    def get_embedding_count(self) -> int:
        """Get the number of stored embeddings"""
        return len(self.embeddings)

    def has_embedding(self, node_id: str) -> bool:
        """Check if a node has an embedding"""
        return node_id in self.embeddings
