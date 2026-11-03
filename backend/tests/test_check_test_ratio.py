# covers: scripts/check_test_ratio.py
"""Changed test files must stay, with their siblings, within 1.5x what they cover."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.check_test_ratio import check, main  # noqa: E402


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


def _check(root, files, changed):
    for name, text in files.items():
        (root / name).parent.mkdir(exist_ok=True)
        (root / name).write_text(text)
    return check(root, list(files), set(changed))


def _printed_paths(capsys):
    return {line.split(":")[0] for line in capsys.readouterr().out.splitlines()}


def test_changed_files_covering_one_module_are_held_to_the_limit_together(repo, capsys):
    a, b = "tests/test_mod.py", "tests/test_mod_guards.py"
    assert _commit(repo, {a: _lines(15), b: _lines(15)}) == 1
    assert _printed_paths(capsys) == {a, b}


def test_an_unchanged_sibling_counts_towards_the_total_but_is_not_reported(tmp_path):
    changed, sibling = "tests/test_mod.py", "tests/test_mod_guards.py"
    report = _check(
        tmp_path,
        {"mod.py": _lines(10), changed: _lines(1), sibling: _lines(15)},
        [changed],
    )
    assert [item.path for item in report.over] == [changed]
    item = report.over[0]
    assert (item.lines, item.total_lines, item.test_files) == (1, 16, 2)
    assert (item.module, item.module_lines) == ("mod.py", 10)


def test_naming_a_large_module_adds_no_room_to_a_small_one(tmp_path):
    wide, narrow = "tests/test_wide.py", "tests/test_mod.py"
    report = _check(
        tmp_path,
        {
            "mod.py": _lines(10),
            "big.py": _lines(1000),
            wide: "# covers: mod.py, big.py\n",
            narrow: _lines(501),
        },
        [wide, narrow],
    )
    assert {(item.path, item.module) for item in report.over} == {
        (wide, "mod.py"),
        (narrow, "mod.py"),
    }


def test_a_file_covering_several_modules_counts_in_proportion_to_their_lines(tmp_path):
    wide, narrow = "tests/test_wide.py", "tests/test_mod.py"
    files = {
        "mod.py": _lines(10),
        "big.py": _lines(30),
        wide: "# covers: mod.py, big.py\n" + _lines(39),
    }
    assert _check(tmp_path, {**files, narrow: _lines(5)}, [wide]).over == []
    assert len(_check(tmp_path, {**files, narrow: _lines(6)}, [wide]).over) == 1


@pytest.mark.parametrize(
    "files",
    [
        {"tests/test_a.py": "# covers: mod.py, ./mod.py\n" + _lines(15)},
        {
            "tests/test_a.py": "# covers: ./mod.py\n" + _lines(7),
            "tests/test_b.py": "# covers: mod.py\n" + _lines(7),
        },
    ],
)
def test_spellings_of_one_covers_path_count_as_one_module(tmp_path, files):
    report = _check(tmp_path, {"mod.py": _lines(10), **files}, files)
    assert [item.module for item in report.over] == ["mod.py"] * len(files)
    assert report.over[0].total_lines == 16


def test_a_test_file_covering_only_empty_modules_is_over_the_limit(tmp_path):
    test = "tests/test_a.py"
    files = {"a.py": "", "b.py": "", test: "# covers: a.py, b.py\n"}
    assert {item.module for item in _check(tmp_path, files, [test]).over} == {
        "a.py",
        "b.py",
    }


def test_the_longest_name_prefix_wins(tmp_path):
    test = "tests/test_mod_extra_guards.py"
    files = {"mod.py": _lines(100), "mod_extra.py": _lines(10), test: _lines(16)}
    report = _check(tmp_path, files, [test])
    assert report.over[0].module == "mod_extra.py"


def test_a_test_file_outside_tests_matches_the_module_beside_it(repo):
    (repo / "svc").mkdir()
    (repo / "svc/flow.py").write_text(_lines(10))
    assert _commit(repo, {"svc/test_flow.py": _lines(15)}) == 0
    assert _commit(repo, {"svc/test_flow.py": _lines(16)}) == 1
    assert _commit(repo, {"svc/test_other.py": _lines(1)}) == 1


def test_a_ratio_exempt_file_is_skipped_printed_and_left_out_of_totals(repo, capsys):
    exempt = "tests/test_mod_big.py"
    header = "# ratio-exempt: generated matrix\n"
    assert _commit(repo, {exempt: header + _lines(99)}) == 0
    assert _printed_paths(capsys) == {exempt}
    assert _commit(repo, {"tests/test_mod.py": _lines(15)}) == 0


def test_a_ratio_exempt_header_without_a_reason_does_not_exempt(repo):
    assert _commit(repo, {"tests/test_mod.py": "# ratio-exempt:\n" + _lines(99)}) == 1


def test_an_unchanged_exempt_file_is_not_printed(repo, capsys):
    exempt = "tests/test_mod_big.py"
    _commit(repo, {exempt: "# ratio-exempt: generated matrix\n"})
    capsys.readouterr()
    assert main(["--base", "work"]) == 0
    assert _printed_paths(capsys) == set()


def test_a_base_ref_git_cannot_resolve_is_an_error_not_a_pass(repo):
    assert main(["--base", "no-such-ref"]) == 2
