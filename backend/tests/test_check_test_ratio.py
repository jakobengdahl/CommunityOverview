# covers: scripts/check_test_ratio.py
"""Changed test files must stay, with their siblings, within 1.5x what they cover."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.check_test_ratio import _read_base, check, main  # noqa: E402


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


def test_a_large_file_covering_a_large_module_charges_a_small_one_only_its_share(
    tmp_path,
):
    test = "tests/test_wide.py"
    files = {"mod.py": _lines(10), "big.py": _lines(1000)}
    both = {**files, test: "# covers: mod.py, big.py\n" + _lines(999)}
    only_mod = {**files, test: "# covers: mod.py\n" + _lines(999)}
    assert _check(tmp_path, both, [test]).over == []
    assert [item.module for item in _check(tmp_path, only_mod, [test]).over] == [
        "mod.py"
    ]


def test_a_small_modules_limit_is_not_pooled_with_a_large_one_a_sibling_names(tmp_path):
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


@pytest.mark.parametrize(
    "target",
    [
        "tests/test_a.py",
        "tests/helpers.py",
        "notes.md",
        "../outside.py",
        "untracked.py",
    ],
)
def test_a_covers_header_naming_anything_but_a_module_is_unresolved(tmp_path, target):
    root = tmp_path / "repo"
    root.mkdir()
    test = "tests/test_a.py"
    (tmp_path / "outside.py").write_text(_lines(1000))
    (root / "untracked.py").write_text(_lines(1000))
    files = {
        "tests/helpers.py": _lines(1000),
        "notes.md": _lines(1000),
        test: f"# covers: {target}\n",
    }
    report = _check(root, files, [test])
    assert list(report.unresolved) == [test]
    assert report.over == []


@pytest.mark.parametrize(
    "target", ["hooks/check.sh", "workflows/ci.yml", "Dockerfile", "app/Dockerfile"]
)
def test_a_covers_header_may_name_a_shell_workflow_or_docker_source(tmp_path, target):
    test = "tests/test_a.py"
    files = {target: _lines(10), test: f"# covers: {target}\n" + _lines(15)}
    report = _check(tmp_path, files, [test])
    assert report.unresolved == {}
    assert [item.module for item in report.over] == [target]


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


def _commit_on_base(repo, files):
    _git("checkout", "-q", "base")
    _commit(repo, files)
    _git("checkout", "-qB", "work")


def test_growth_within_a_module_already_over_on_base_is_printed_and_does_not_fail(
    repo, capsys
):
    test = "tests/test_mod.py"
    _commit_on_base(repo, {test: _lines(16)})
    capsys.readouterr()
    assert _commit(repo, {test: _lines(40)}) == 0
    assert _printed_paths(capsys) == {test}


def test_a_module_within_the_limit_on_base_is_reported_once_it_grows_past_it(repo):
    test = "tests/test_mod.py"
    _commit_on_base(repo, {test: _lines(15)})
    assert _commit(repo, {test: _lines(16)}) == 1


@pytest.mark.parametrize("base_size, expected", [(16, 0), (15, 1)])
def test_a_header_added_on_the_branch_judges_the_file_on_its_size_before(
    repo, base_size, expected
):
    test = "tests/test_old.py"
    _commit_on_base(repo, {test: _lines(base_size)})
    assert _commit(repo, {test: "# covers: mod.py\n" + _lines(99)}) == expected


def test_a_file_exempt_on_the_branch_does_not_count_towards_the_base_total(repo):
    big = "tests/test_mod_big.py"
    _commit_on_base(repo, {big: _lines(99)})
    files = {big: "# ratio-exempt: generated matrix\n", "tests/test_mod.py": _lines(16)}
    assert _commit(repo, files) == 1


@pytest.mark.parametrize("sibling, expected", [(15, 0), (16, 1)])
def test_a_header_on_a_large_old_file_neither_hides_nor_causes_a_sibling_report(
    repo, sibling, expected
):
    old, test = "tests/test_old.py", "tests/test_mod.py"
    _commit_on_base(repo, {old: _lines(99), test: _lines(15)})
    assert (
        _commit(repo, {old: "# covers: mod.py\n" + _lines(99), test: _lines(sibling)})
        == expected
    )
    report = check(repo, ["mod.py", old, test], {old, test}, _read_base(repo, "base"))
    assert [item.path for item in report.over] == [test] * expected


@pytest.mark.parametrize(
    "before, after",
    [
        ("# ratio-exempt: generated matrix\n", "# covers: mod.py\n"),
        ("# covers: other.py\n", "# covers: mod.py\n"),
    ],
)
def test_a_file_newly_charged_to_a_module_within_the_limit_is_reported(
    repo, before, after
):
    test = "tests/test_big.py"
    _commit_on_base(repo, {"other.py": _lines(99), test: before + _lines(99)})
    assert _commit(repo, {test: after + _lines(99)}) == 1


@pytest.mark.parametrize("base_size, expected", [(16, 0), (15, 1)])
def test_a_moved_module_is_judged_by_its_old_path_on_base(repo, base_size, expected):
    _commit_on_base(repo, {"tests/test_mod.py": _lines(base_size)})
    _git("mv", "mod.py", "moved.py")
    _git("mv", "tests/test_mod.py", "tests/test_moved.py")
    assert _commit(repo, {"tests/test_moved.py": _lines(16)}) == expected


def test_a_renamed_test_file_keeps_its_base_total(repo):
    _commit_on_base(repo, {"tests/test_mod.py": _lines(16)})
    _git("mv", "tests/test_mod.py", "tests/test_mod_core.py")
    assert _commit(repo, {}) == 0


def test_test_files_merged_on_the_branch_keep_their_base_total(repo):
    guards = "tests/test_mod_guards.py"
    _commit_on_base(repo, {"tests/test_mod.py": _lines(8), guards: _lines(8)})
    (repo / guards).unlink()
    assert _commit(repo, {"tests/test_mod.py": _lines(16)}) == 0


def test_the_base_total_splits_a_file_by_the_modules_lines_on_base(repo):
    test = "tests/test_wide.py"
    header = "# covers: mod.py, big.py\n"
    _commit_on_base(repo, {"big.py": _lines(1000), test: header + _lines(99)})
    assert _commit(repo, {"big.py": _lines(10), test: header + _lines(98)}) == 1


def test_the_base_is_the_merge_base_not_the_tip_of_the_ref(repo):
    test = "tests/test_mod.py"
    _commit(repo, {test: _lines(16)})
    _git("checkout", "-q", "base")
    (repo / test).write_text(_lines(99))
    _git("add", ".")
    _git("commit", "-qm", "later on base")
    _git("checkout", "-q", "work")
    assert main(["--base", "base"]) == 1


def test_base_content_is_read_from_any_directory_and_in_any_encoding(repo, monkeypatch):
    test = "tests/test_mod.py"
    (repo / test).write_bytes(b"\xff\n" * 16)
    _commit_on_base(repo, {})
    (repo / test).write_text(_lines(40))
    _git("add", ".")
    _git("commit", "-qm", "grow")
    monkeypatch.chdir(repo / "tests")
    assert main(["--base", "base"]) == 0


def test_a_base_ref_git_cannot_resolve_is_an_error_not_a_pass(repo):
    assert main(["--base", "no-such-ref"]) == 2


@pytest.mark.parametrize("base_size, expected", [(15, 1), (16, 0)])
@pytest.mark.parametrize("linked_test", [False, True])
def test_base_symlinks_count_target_content(
    repo, capsys, base_size, expected, linked_test
):
    test = "tests/test_mod.py"
    _git("checkout", "-q", "base")
    (repo / "source").mkdir()
    (repo / "source/actual.py").write_text(_lines(10))
    (repo / "mod.py").unlink()
    (repo / "mod.py").symlink_to("source/alias.py")
    (repo / "source/alias.py").symlink_to("actual.py")
    if linked_test:
        (repo / "test_content.txt").write_text(_lines(base_size))
        (repo / test).symlink_to("../test_content.txt")
    else:
        (repo / test).write_text(_lines(base_size))
    _git("add", ".")
    _git("commit", "-qm", "linked base")
    _git("checkout", "-qB", "work")
    assert _read_base(repo, "base").tree.lines("mod.py") == 10
    if linked_test:
        (repo / test).unlink()
        (repo / test).symlink_to("../grown_content.txt")
    assert _commit(repo, {test: _lines(16 if expected else 17)}) == expected
    output = capsys.readouterr().out
    assert test + ":" in output
    assert ("already over the limit on base" in output) == (expected == 0)
