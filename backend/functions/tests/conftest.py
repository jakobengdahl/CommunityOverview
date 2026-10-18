"""Fixtures for user-function tests."""

import os
import tempfile

import pytest

from backend.core import GraphStorage
from backend.service.tests.conftest import MockSentenceTransformer


@pytest.fixture(autouse=True)
def mock_embedding_model():
    """Keep embedding generation offline and deterministic."""
    import backend.core.vector_store as vs

    original = vs._ensure_sentence_transformers
    vs._ensure_sentence_transformers = lambda: MockSentenceTransformer
    vs._SentenceTransformer = None
    yield
    vs._ensure_sentence_transformers = original
    vs._SentenceTransformer = None


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        yield GraphStorage(
            json_path=os.path.join(tmpdir, "graph.json"),
            embeddings_path=os.path.join(tmpdir, "embeddings.pkl"),
        )
