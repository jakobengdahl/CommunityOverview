"""Query-time passage retrieval over text extracted from node attachments.

Pure functions only: no I/O, no persistent index, no dependency beyond the
base install. Callers hand in the segments an extractor produced (one per
page, sheet, slide or similar) and get back ranked passages, each still
carrying the locator of the segment it came from so an answer can cite it.

Ranking is BM25 over Unicode word tokens. When the caller passes a
``VectorStore`` whose embedding model is *already loaded*, the top BM25
candidates are re-ranked with that model and the two orders are fused with
reciprocal-rank fusion. The model is never loaded here: loading it is slow
and needs the optional ML extra, and a query must not pay for either.

Only chunks that contain at least one query term are returned. The
embedding pass reorders lexical matches; it never introduces a chunk with no
lexical match, so a chunk containing a query term always outranks one that
contains none, in either ranking mode.
"""

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 1200
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_TOP_K = 8
DEFAULT_RERANK_CANDIDATES = 32

# Okapi BM25 parameters, the usual defaults.
BM25_K1 = 1.2
BM25_B = 0.75
# Reciprocal-rank fusion constant from Cormack et al. (2009).
RRF_K = 60

RANKING_BM25 = "bm25"
RANKING_HYBRID = "hybrid"

_WORD_RE = re.compile(r"\w+")


@dataclass(frozen=True)
class Segment:
    """A run of extracted text that shares one locator.

    ``locator`` identifies where in the file the text sits (page, sheet,
    slide, ...). ``source`` carries opaque ids (node, attachment, ...) that
    this module copies onto every passage and never interprets.
    """

    text: str
    locator: str
    source: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    text: str
    locator: str
    source: Mapping[str, Any]
    # Position among all chunks of the input, in input order. Ties in score
    # are broken on it, which is what makes the ranking deterministic.
    ordinal: int
    # Character offset of the chunk within its segment's text.
    start: int


@dataclass(frozen=True)
class Passage:
    text: str
    locator: str
    source: Mapping[str, Any]
    ordinal: int
    start: int
    score: float
    bm25_score: float


@dataclass(frozen=True)
class RetrievalResult:
    passages: List[Passage]
    ranking: str


def tokenize(text: str) -> List[str]:
    """Split text into case-folded Unicode word tokens.

    NFKC folds compatibility forms (ligatures, full-width letters) and
    ``\\w`` is Unicode-aware, so Swedish letters stay inside their words.
    """
    return _WORD_RE.findall(unicodedata.normalize("NFKC", text).casefold())


def _window_end(text: str, start: int, chunk_size: int) -> int:
    end = start + chunk_size
    if end >= len(text):
        return len(text)
    # Prefer to end on whitespace so a word is not cut, but never shorten the
    # window below half its size looking for it.
    cut = max(
        text.rfind(" ", start + chunk_size // 2, end),
        text.rfind("\n", start + chunk_size // 2, end),
    )
    return cut if cut > start else end


def _next_start(text: str, start: int, end: int, overlap: int) -> int:
    nxt = max(end - overlap, start + 1)
    # Begin the overlap on a word boundary when one is inside it.
    if 0 < nxt < end and not text[nxt - 1].isspace():
        for i in range(nxt, end):
            if text[i].isspace():
                return i + 1 if i + 1 < end else nxt
    return nxt


def chunk_segments(
    segments: Sequence[Segment],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Chunk]:
    """Cut each segment into overlapping windows.

    A chunk never spans two segments, so its locator is always exact.
    Segments with no word characters produce no chunk.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must be at least 0 and below chunk_size")

    chunks: List[Chunk] = []
    for segment in segments:
        text = segment.text or ""
        if not _WORD_RE.search(text):
            continue
        source = dict(segment.source)
        start = 0
        while start < len(text):
            end = _window_end(text, start, chunk_size)
            piece = text[start:end]
            if _WORD_RE.search(piece):
                chunks.append(
                    Chunk(
                        text=piece,
                        locator=segment.locator,
                        source=source,
                        ordinal=len(chunks),
                        start=start,
                    )
                )
            if end >= len(text):
                break
            start = _next_start(text, start, end, overlap)
    return chunks


def bm25_scores(
    query_terms: Sequence[str], documents: Sequence[Sequence[str]]
) -> List[float]:
    """Okapi BM25 score of every tokenized document for the query terms.

    IDF uses the ``log(1 + ...)`` form, which stays positive for a term that
    occurs in most documents; the classic form goes negative there and would
    rank a chunk containing the term below one without it.
    """
    n_docs = len(documents)
    if n_docs == 0:
        return []
    terms = sorted(set(query_terms))
    if not terms:
        return [0.0] * n_docs

    lengths = [len(doc) for doc in documents]
    avg_len = sum(lengths) / n_docs or 1.0
    freqs: List[Dict[str, int]] = []
    doc_freq = dict.fromkeys(terms, 0)
    for doc in documents:
        counts: Dict[str, int] = {}
        for token in doc:
            if token in doc_freq:
                counts[token] = counts.get(token, 0) + 1
        for term in counts:
            doc_freq[term] += 1
        freqs.append(counts)

    idf = {
        term: math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for term, df in doc_freq.items()
    }
    scores = []
    for counts, length in zip(freqs, lengths):
        score = 0.0
        norm = BM25_K1 * (1 - BM25_B + BM25_B * length / avg_len)
        # Sorted term order keeps the float sum identical across runs.
        for term in terms:
            tf = counts.get(term)
            if tf:
                score += idf[term] * tf * (BM25_K1 + 1) / (tf + norm)
        scores.append(score)
    return scores


def _loaded_model(vector_store: Any) -> Any:
    return getattr(vector_store, "model", None) if vector_store is not None else None


def _embedding_order(model: Any, query: str, texts: List[str]) -> List[int]:
    """Indices of ``texts`` ordered by cosine similarity to the query."""
    import numpy as np

    vectors = np.asarray(model.encode([query] + texts), dtype=np.float64)
    if vectors.ndim != 2 or vectors.shape[0] != len(texts) + 1:
        raise ValueError("embedding model returned an unexpected shape")
    norms = np.linalg.norm(vectors, axis=1)
    norms[norms == 0] = 1.0
    unit = vectors / norms[:, None]
    sims = unit[1:] @ unit[0]
    if not np.all(np.isfinite(sims)):
        raise ValueError("embedding model returned non-finite values")
    # Stable sort on -similarity: equal similarities keep BM25 order.
    return sorted(range(len(texts)), key=lambda i: -float(sims[i]))


def rank_passages(
    query: str,
    segments: Sequence[Segment],
    *,
    top_k: int = DEFAULT_TOP_K,
    rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    vector_store: Optional[Any] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> RetrievalResult:
    """Return the ``top_k`` passages best matching ``query``.

    ``ranking`` is ``"hybrid"`` only when an embedding re-rank actually ran;
    otherwise (no store, model not loaded, or the model failed) it is
    ``"bm25"``. At most ``rerank_candidates`` chunks are sent to the model.
    """
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if rerank_candidates < 1:
        raise ValueError("rerank_candidates must be at least 1")

    chunks = chunk_segments(segments, chunk_size=chunk_size, overlap=overlap)
    scores = bm25_scores(tokenize(query), [tokenize(c.text) for c in chunks])
    matched = sorted(
        (i for i, score in enumerate(scores) if score > 0),
        key=lambda i: (-scores[i], chunks[i].ordinal),
    )

    order = matched
    final_score = {i: scores[i] for i in matched}
    ranking = RANKING_BM25
    model = _loaded_model(vector_store)
    if model is not None and len(matched) > 1:
        candidates = matched[:rerank_candidates]
        try:
            by_embedding = _embedding_order(
                model, query, [chunks[i].text for i in candidates]
            )
        except Exception as exc:  # noqa: BLE001 - any model failure means BM25
            logger.warning("attachment re-rank failed, using bm25 only: %s", exc)
        else:
            fused = {i: 1.0 / (RRF_K + rank + 1) for rank, i in enumerate(candidates)}
            for rank, pos in enumerate(by_embedding):
                fused[candidates[pos]] += 1.0 / (RRF_K + rank + 1)
            reranked = sorted(
                candidates,
                key=lambda i: (-fused[i], -scores[i], chunks[i].ordinal),
            )
            tail = matched[len(candidates) :]
            order = reranked + tail
            final_score = dict(fused)
            # Chunks past the candidate cap keep their BM25 rank and a
            # one-sided RRF score, which is below every fused candidate score,
            # so ``score`` stays monotonic down the list.
            for rank, i in enumerate(tail, start=len(candidates)):
                final_score[i] = 1.0 / (RRF_K + rank + 1)
            ranking = RANKING_HYBRID

    passages = [
        Passage(
            text=chunks[i].text,
            locator=chunks[i].locator,
            source=chunks[i].source,
            ordinal=chunks[i].ordinal,
            start=chunks[i].start,
            score=final_score[i],
            bm25_score=scores[i],
        )
        for i in order[:top_k]
    ]
    return RetrievalResult(passages=passages, ranking=ranking)
