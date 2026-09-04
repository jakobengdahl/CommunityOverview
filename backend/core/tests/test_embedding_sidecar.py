"""
Unit tests for the binary embedding sidecar.

The sidecar is derived data, so the contract has two halves: a faithful
round-trip for well-formed files, and a refusal that callers can catch for
everything else — never a partial or silently wrong read.
"""

import json
import random
import struct
import tempfile
from pathlib import Path

import numpy as np
import pytest

from backend.core.embedding_sidecar import (
    MAGIC,
    EmbeddingSidecarError,
    FileEmbeddingSidecar,
)


@pytest.fixture
def sidecar():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield FileEmbeddingSidecar(Path(tmpdir) / "graph.embeddings.bin")


def test_round_trip_preserves_ids_and_values(sidecar):
    vectors = {
        "node-a": [1.0, 0.0, -0.5],
        "node-b": [0.25, 0.75, 1.5],
    }
    sidecar.save(vectors)
    loaded = sidecar.load()

    assert set(loaded) == {"node-a", "node-b"}
    for node_id, expected in vectors.items():
        assert loaded[node_id].dtype == np.float32
        np.testing.assert_allclose(loaded[node_id], np.float32(expected))


def test_round_trip_keeps_rows_with_their_own_ids(sidecar):
    """A row/id mix-up would still load and still be the wrong vector."""
    vectors = {f"n{i}": [float(i), float(i) + 0.5] for i in range(20)}
    sidecar.save(vectors)
    loaded = sidecar.load()

    for node_id, expected in vectors.items():
        np.testing.assert_allclose(loaded[node_id], np.float32(expected))


def test_loaded_rows_are_writable_copies(sidecar):
    """Rows must not stay views onto the file buffer, which would pin the whole
    file in memory and be read-only."""
    sidecar.save({"n1": [1.0, 2.0]})
    row = sidecar.load()["n1"]

    row += 1.0
    np.testing.assert_allclose(row, np.float32([2.0, 3.0]))


def test_empty_index_round_trips(sidecar):
    sidecar.save({})
    assert sidecar.exists()
    assert sidecar.load() == {}


def test_save_creates_missing_parent_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "nested" / "dir" / "graph.embeddings.bin"
        FileEmbeddingSidecar(target).save({"n1": [1.0]})
        assert target.exists()


def test_save_leaves_no_temp_files_behind(sidecar):
    sidecar.save({"n1": [1.0, 2.0]})
    sidecar.save({"n1": [3.0, 4.0]})

    assert [p.name for p in sidecar.path.parent.iterdir()] == [sidecar.path.name]
    np.testing.assert_allclose(sidecar.load()["n1"], np.float32([3.0, 4.0]))


def test_save_rejects_mixed_dimensions(sidecar):
    with pytest.raises(EmbeddingSidecarError, match="mixed dimensions"):
        sidecar.save({"n1": [1.0, 2.0], "n2": [1.0, 2.0, 3.0]})


def test_load_rejects_a_file_that_is_not_a_sidecar(sidecar):
    sidecar.path.write_bytes(b"just some other file entirely")

    with pytest.raises(EmbeddingSidecarError, match="not an embedding sidecar"):
        sidecar.load()


def test_load_rejects_truncated_payload(sidecar):
    sidecar.save({"n1": [1.0, 2.0], "n2": [3.0, 4.0]})
    raw = sidecar.path.read_bytes()
    sidecar.path.write_bytes(raw[:-4])

    with pytest.raises(EmbeddingSidecarError, match="payload"):
        sidecar.load()


def test_load_rejects_header_longer_than_the_file(sidecar):
    header = json.dumps({"dtype": "float32", "rows": 0, "dim": 0, "ids": []}).encode()
    sidecar.path.write_bytes(MAGIC + struct.pack("<I", len(header) + 500) + header)

    with pytest.raises(EmbeddingSidecarError, match="truncated header"):
        sidecar.load()


def test_load_rejects_header_whose_id_count_disagrees_with_rows(sidecar):
    header = json.dumps(
        {"dtype": "float32", "rows": 2, "dim": 1, "ids": ["only-one"]}
    ).encode()
    payload = np.float32([[1.0], [2.0]]).tobytes()
    sidecar.path.write_bytes(MAGIC + struct.pack("<I", len(header)) + header + payload)

    with pytest.raises(EmbeddingSidecarError, match="inconsistent header"):
        sidecar.load()


def test_load_rejects_unsupported_dtype(sidecar):
    header = json.dumps(
        {"dtype": "float64", "rows": 1, "dim": 1, "ids": ["n1"]}
    ).encode()
    payload = np.float64([[1.0]]).tobytes()
    sidecar.path.write_bytes(MAGIC + struct.pack("<I", len(header)) + header + payload)

    with pytest.raises(EmbeddingSidecarError, match="unsupported dtype"):
        sidecar.load()


def test_load_rejects_unreadable_header(sidecar):
    header = b"{not json"
    sidecar.path.write_bytes(MAGIC + struct.pack("<I", len(header)) + header)

    with pytest.raises(EmbeddingSidecarError, match="unreadable header"):
        sidecar.load()


def test_stored_matrix_is_float32_not_json_text(sidecar):
    """The whole point of the sidecar: 384 floats cost 4 bytes each, not the
    ~30 bytes each they cost as JSON text in an indent=2 graph.json - the 7.5x
    inflation this split exists to remove."""
    vectors = {f"n{i}": np.random.rand(384).astype(np.float32) for i in range(50)}
    sidecar.save(vectors)

    payload_bytes = 50 * 384 * 4
    assert sidecar.path.stat().st_size < payload_bytes * 1.1


def test_load_rejects_a_header_that_is_valid_json_but_not_an_object(sidecar):
    """A bare scalar or array decodes fine but has no .get(). Raising the
    module's own error is what keeps GraphStorage.load() from dying on it."""
    for body in (b"null", b"[]", b'"a string"', b"123"):
        sidecar.path.write_bytes(MAGIC + struct.pack("<I", len(body)) + body)

        with pytest.raises(EmbeddingSidecarError, match="header is not an object"):
            sidecar.load()


def test_load_rejects_non_string_node_ids(sidecar):
    """Ids reach a dict comprehension, so an unhashable one raises TypeError —
    which is not EmbeddingSidecarError, so it escapes the caller's catch and
    fails the whole graph load."""
    header = json.dumps(
        {"dtype": "float32", "rows": 1, "dim": 2, "ids": [[1, 2]]}
    ).encode()
    payload = np.float32([[1.0, 2.0]]).tobytes()
    sidecar.path.write_bytes(MAGIC + struct.pack("<I", len(header)) + header + payload)

    with pytest.raises(EmbeddingSidecarError, match="non-string node id"):
        sidecar.load()


def test_a_payload_longer_than_the_header_promises_is_rejected(tmp_path):
    """Trailing bytes — a partially overwritten or appended-to file — must be
    caught here. Letting them through reaches np.frombuffer/reshape, which
    raises a bare ValueError that GraphStorage does not catch, so a damaged
    sidecar fails the whole graph load instead of degrading to no vectors."""
    path = tmp_path / "graph.embeddings.bin"
    sidecar = FileEmbeddingSidecar(path)
    sidecar.save({"n1": np.ones(4, dtype=np.float32)})

    with open(path, "ab") as f:
        f.write(b"trailing junk")

    with pytest.raises(EmbeddingSidecarError):
        sidecar.load()


def _valid_blob() -> bytes:
    header = json.dumps(
        {"dtype": "float32", "rows": 2, "dim": 3, "ids": ["a", "b"]}
    ).encode("utf-8")
    payload = np.ones(6, dtype="<f4").tobytes()
    return MAGIC + struct.pack("<I", len(header)) + header + payload


def _load_raises_only_its_own_error(path: Path, blob: bytes) -> None:
    """load() may succeed or raise EmbeddingSidecarError. Anything else is the
    defect: GraphStorage catches only EmbeddingSidecarError, so a stray
    ValueError, TypeError or struct.error turns a damaged derived file into a
    failed graph load."""
    path.write_bytes(blob)
    try:
        FileEmbeddingSidecar(path).load()
    except EmbeddingSidecarError:
        pass
    except Exception as exc:  # noqa: BLE001 - the point of the test
        raise AssertionError(
            f"load() raised {type(exc).__name__}({exc}) for {blob[:64]!r}...; "
            f"only EmbeddingSidecarError may escape"
        ) from exc


def test_no_truncation_of_a_valid_sidecar_escapes_as_another_error(tmp_path):
    """Every prefix, not a chosen few. Enumerating shapes one at a time is how
    three separate escapes reached the graph loader across as many reviews."""
    blob = _valid_blob()
    path = tmp_path / "graph.embeddings.bin"

    for cut in range(len(blob) + 1):
        _load_raises_only_its_own_error(path, blob[:cut])


def test_no_extension_of_a_valid_sidecar_escapes_as_another_error(tmp_path):
    blob = _valid_blob()
    path = tmp_path / "graph.embeddings.bin"

    for extra in range(1, 40):
        _load_raises_only_its_own_error(path, blob + b"\xff" * extra)


def test_no_single_byte_corruption_escapes_as_another_error(tmp_path):
    blob = _valid_blob()
    path = tmp_path / "graph.embeddings.bin"

    for offset in range(len(blob)):
        for value in (0x00, 0x01, 0x7F, 0xFF):
            corrupted = bytearray(blob)
            corrupted[offset] = value
            _load_raises_only_its_own_error(path, bytes(corrupted))


def test_no_header_type_confusion_escapes_as_another_error(tmp_path):
    """JSON has types Python's isinstance checks do not separate on their own —
    bool is a subclass of int, and True == 1, so `rows: true` satisfies a
    length check of rows*dim*4 against a 4-byte payload."""
    path = tmp_path / "graph.embeddings.bin"
    # Each payload is sized to what the header CLAIMS, so the length check
    # cannot mask the type confusion by rejecting it first for another reason.
    # rows=True, dim=1 promises True*1*4 == 4 bytes, and gets exactly 4.
    cases = [
        ({"dtype": "float32", "rows": True, "dim": 1, "ids": ["a"]}, 4),
        ({"dtype": "float32", "rows": 1, "dim": True, "ids": ["a"]}, 4),
        ({"dtype": "float32", "rows": True, "dim": True, "ids": ["a"]}, 4),
        ({"dtype": "float32", "rows": False, "dim": False, "ids": []}, 0),
        ({"dtype": "float32", "rows": 1.0, "dim": 4.0, "ids": ["a"]}, 16),
        ({"dtype": "float32", "rows": 1, "dim": 4, "ids": "not-a-list"}, 16),
        ({"dtype": "float32", "rows": None, "dim": None, "ids": []}, 0),
        ({"dtype": "float32", "rows": 1, "dim": 4, "ids": [None]}, 16),
        ({"dtype": "float32", "rows": 1, "dim": 4}, 16),
        ({"rows": 1, "dim": 4, "ids": ["a"]}, 16),
        ([], 0),
        ("a string header", 0),
    ]
    for header, payload_len in cases:
        encoded = json.dumps(header).encode("utf-8")
        blob = MAGIC + struct.pack("<I", len(encoded)) + encoded + b"\x00" * payload_len
        _load_raises_only_its_own_error(path, blob)


def test_random_bytes_behind_a_valid_magic_never_escape(tmp_path):
    """The magic is the cheap gate; everything after it is attacker- or
    corruption-controlled. Seeded, so a failure is reproducible."""
    rng = random.Random(20260904)
    path = tmp_path / "graph.embeddings.bin"

    for _ in range(400):
        length = rng.randrange(0, 96)
        body = bytes(rng.randrange(256) for _ in range(length))
        _load_raises_only_its_own_error(path, MAGIC + body)


def test_a_zero_width_header_reads_back_as_no_vectors(tmp_path):
    """rows without dim is still nothing. Returning three zero-width rows
    instead puts them in the index, and a real query then raises out of numpy —
    the load survives and semantic search does not, which is the failure this
    format's degrade-never-fail contract exists to prevent."""
    header = json.dumps(
        {"dtype": "float32", "rows": 3, "dim": 0, "ids": ["a", "b", "c"]}
    ).encode("utf-8")
    path = tmp_path / "graph.embeddings.bin"
    path.write_bytes(MAGIC + struct.pack("<I", len(header)) + header)

    assert FileEmbeddingSidecar(path).load() == {}


def test_a_deeply_nested_header_degrades_instead_of_escaping(tmp_path):
    """json.loads raises RecursionError, not ValueError, on deep nesting — a
    type the checks never named. About 2 kB of brackets is enough, and the
    callers catch EmbeddingSidecarError and nothing else, so it took the whole
    graph load down."""
    header = (b"[" * 1000) + (b"]" * 1000)
    path = tmp_path / "graph.embeddings.bin"
    path.write_bytes(MAGIC + struct.pack("<I", len(header)) + header)

    with pytest.raises(EmbeddingSidecarError):
        FileEmbeddingSidecar(path).load()


def test_an_unforeseen_failure_inside_load_is_converted_not_propagated(
    tmp_path, monkeypatch
):
    """The guards name what is wrong; this is what makes the contract hold when
    they miss something. Three malformed shapes have each reached a caller as a
    different exception type, so the property is enforced by conversion rather
    than by enumerating parser failures."""
    path = tmp_path / "graph.embeddings.bin"
    FileEmbeddingSidecar(path).save({"n1": np.ones(4, dtype=np.float32)})

    def explode(self):
        raise MemoryError("something no guard anticipated")

    monkeypatch.setattr(FileEmbeddingSidecar, "_read", explode)

    with pytest.raises(EmbeddingSidecarError):
        FileEmbeddingSidecar(path).load()
