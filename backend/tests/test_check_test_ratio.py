# covers: scripts/check_test_ratio.py
"""A changed test file must stay within 1.5x the lines of what it covers."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.check_test_ratio import main  # noqa: E402


def _lines(count):
    return "x = 1\n" * count


def _git(*args):
    identity = [
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run(["git", *identity, *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo whose `base` branch has a 10-line module and an oversized test file."""
    monkeypatch.chdir(tmp_path)
    _git("init", "-q", "-b", "base")
    (tmp_path / "tests").mkdir()
    (tmp_path / "mod.py").write_text(_lines(10))
    (tmp_path / "tests/test_oversized.py").write_text(_lines(99))
    _git("add", ".")
    _git("commit", "-qm", "base")
    _git("checkout", "-qb", "work")
    return tmp_path


def _commit(repo, files):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    _git("add", ".")
    _git("commit", "-qm", "change")
    return main(["--base", "base"])


@pytest.mark.parametrize("lines, expected", [(15, 0), (16, 1)])
def test_the_whole_changed_file_is_held_to_the_limit(repo, lines, expected):
    assert _commit(repo, {"tests/test_mod.py": _lines(lines)}) == expected


def test_an_unchanged_oversized_file_is_not_reported_and_a_deleted_one_neither(repo):
    assert _commit(repo, {"mod.py": _lines(11)}) == 0
    (repo / "tests/test_oversized.py").unlink()
    _git("add", ".")
    _git("commit", "-qm", "delete")
    assert main(["--base", "base"]) == 0


def test_a_js_test_is_matched_to_the_module_beside_it(repo):
    assert (
        _commit(repo, {"ui/Panel.jsx": _lines(10), "ui/Panel.test.jsx": _lines(15)})
        == 0
    )
    assert _commit(repo, {"ui/Panel.test.jsx": _lines(16)}) == 1


def test_a_file_without_a_match_needs_a_covers_header_naming_real_files(repo):
    assert _commit(repo, {"tests/test_other.py": _lines(1)}) == 1
    assert _commit(repo, {"tests/test_other.py": "# covers: gone.py\n"}) == 1


def test_a_covers_header_sums_its_modules_and_wins_over_a_name_match(repo):
    (repo / "extra.py").write_text(_lines(10))
    header = "# covers: mod.py, extra.py\n"
    assert _commit(repo, {"tests/test_mod.py": header + _lines(29)}) == 0
    assert _commit(repo, {"tests/test_mod.py": header + _lines(30)}) == 1


def test_a_base_ref_git_cannot_resolve_is_an_error_not_a_pass(repo):
    assert main(["--base", "no-such-ref"]) == 2
