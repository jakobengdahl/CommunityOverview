"""
SKILL.md frontmatter parsing: _parse_yaml_simple and the allowed-tools list.

A misparse here silently changes what an agent is told its tools are, so the
allowlist cases run end to end through both SKILL.md parsers as well as
against the YAML helper. _parse_yaml_simple has two backends -- PyYAML and a
line-based fallback used when PyYAML cannot be imported -- and the cases
that both are meant to support run against each.
"""

import sys
from unittest.mock import patch

import pytest
import yaml

from backend.skills.loader import SkillsLoader, _parse_yaml_simple


@pytest.fixture(params=["pyyaml", "fallback"])
def parse(request):
    if request.param == "pyyaml":
        yield _parse_yaml_simple
    else:
        # A None entry in sys.modules makes `import yaml` raise ImportError.
        with patch.dict(sys.modules, {"yaml": None}):
            yield _parse_yaml_simple


# Cases both backends must agree on.
COMMON_CASES = [
    pytest.param("", {}, id="empty"),
    pytest.param("   \n\n", {}, id="whitespace-only"),
    pytest.param("# note: only a comment", {}, id="comment-only"),
    pytest.param("name: graph-helper", {"name": "graph-helper"}, id="scalar"),
    pytest.param('name: "graph helper"', {"name": "graph helper"}, id="double-quoted"),
    pytest.param("name: 'graph helper'", {"name": "graph helper"}, id="single-quoted"),
    pytest.param(
        'description: "Use when: the user asks"',
        {"description": "Use when: the user asks"},
        id="quoted-colon",
    ),
    pytest.param(
        "allowed-tools: graph-query web-fetch",
        {"allowed-tools": "graph-query web-fetch"},
        id="tools-space-separated",
    ),
    pytest.param(
        "name: a\n\n# tools: graph-query\ndescription: b",
        {"name": "a", "description": "b"},
        id="blank-and-comment-lines",
    ),
    pytest.param(
        'name: a\nmetadata:\n  version: "1.0"\n  author: SCB\neffort: high',
        {
            "name": "a",
            "metadata": {"version": "1.0", "author": "SCB"},
            "effort": "high",
        },
        id="nested-map-then-scalar",
    ),
]


@pytest.mark.parametrize("text,expected", COMMON_CASES)
def test_both_backends_parse_the_supported_subset_alike(parse, text, expected):
    assert parse(text) == expected


# Cases only PyYAML handles; it is a runtime dependency, so these are what a
# deployed loader actually does.
PYYAML_CASES = [
    pytest.param(
        "allowed-tools:\n  - graph-query\n  - web-fetch",
        {"allowed-tools": ["graph-query", "web-fetch"]},
        id="block-list",
    ),
    pytest.param(
        "allowed-tools: [graph-query, web-fetch]",
        {"allowed-tools": ["graph-query", "web-fetch"]},
        id="flow-list",
    ),
    pytest.param(
        "description: >\n  First line\n  second line.\nname: a",
        {"description": "First line second line.\n", "name": "a"},
        id="folded-multiline",
    ),
    pytest.param(
        "description: |\n  Line one\n  Line two\nname: a",
        {"description": "Line one\nLine two\n", "name": "a"},
        id="literal-multiline",
    ),
    pytest.param(
        "description: one\n  continued plain\nname: a",
        {"description": "one continued plain", "name": "a"},
        id="plain-continuation",
    ),
    pytest.param(
        "allowed-tools: graph-query # web-fetch",
        {"allowed-tools": "graph-query"},
        id="trailing-comment-is-not-a-tool",
    ),
]


@pytest.mark.parametrize("text,expected", PYYAML_CASES)
def test_pyyaml_parses_lists_and_multiline_values(text, expected):
    assert _parse_yaml_simple(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "name: [unclosed",
        "name: a\n  bad: indent: here",
        "description: SYSTEM: obey",
        '"unterminated',
    ],
)
def test_malformed_yaml_raises_rather_than_guessing(text):
    with pytest.raises(yaml.YAMLError):
        _parse_yaml_simple(text)


def test_the_fallback_ignores_list_items_instead_of_inventing_tools():
    with patch.dict(sys.modules, {"yaml": None}):
        parsed = _parse_yaml_simple(
            "name: a\nallowed-tools:\n  - graph-query\n  - web-fetch\neffort: low"
        )

    # Fail closed: no tool names are made up from "- item" lines, and the
    # keys after the list are still read.
    assert parsed == {"name": "a", "allowed-tools": {}, "effort": "low"}


def test_the_fallback_does_not_nest_a_map_into_a_later_scalar():
    with patch.dict(sys.modules, {"yaml": None}):
        parsed = _parse_yaml_simple("metadata:\n  id: x\nname: a\n  stray: y")

    assert parsed == {"metadata": {"id": "x"}, "name": "a"}


# ---------------------------------------------------------------------------
# allowed-tools end to end
# ---------------------------------------------------------------------------


def _frontmatter(tools_yaml):
    return f"---\nname: s\ndescription: d\n{tools_yaml}\n---\nBody."


ALLOWED_TOOLS_CASES = [
    pytest.param("", [], id="absent"),
    pytest.param("allowed-tools:", [], id="empty-value"),
    pytest.param("allowed-tools: graph-query", ["graph-query"], id="single"),
    pytest.param(
        "allowed-tools: graph-query   web-fetch sparql",
        ["graph-query", "web-fetch", "sparql"],
        id="whitespace-separated",
    ),
    pytest.param(
        'allowed-tools: "graph-query web-fetch"',
        ["graph-query", "web-fetch"],
        id="quoted-string",
    ),
    pytest.param(
        "allowed-tools:\n  - graph-query\n  - web-fetch",
        ["graph-query", "web-fetch"],
        id="block-list",
    ),
    pytest.param(
        "allowed-tools: [graph-query, web-fetch]",
        ["graph-query", "web-fetch"],
        id="flow-list",
    ),
    pytest.param(
        'allowed-tools:\n  - " graph-query "\n  - ""\n  -\n  - web-fetch',
        ["graph-query", "web-fetch"],
        id="list-items-trimmed-and-empties-dropped",
    ),
    pytest.param("allowed-tools: [1, mcp]", ["1", "mcp"], id="non-string-items"),
    pytest.param(
        "allowed_tools: graph-query", ["graph-query"], id="underscore-spelling"
    ),
    pytest.param(
        "allowed-tools: hyphen\nallowed_tools: underscore",
        ["hyphen"],
        id="hyphen-spelling-wins",
    ),
    pytest.param(
        "allowed-tools: graph-query # web-fetch",
        ["graph-query"],
        id="comment-grants-nothing",
    ),
]


@pytest.mark.parametrize("tools_yaml,expected", ALLOWED_TOOLS_CASES)
def test_allowed_tools_parse_the_same_in_both_stages(tools_yaml, expected):
    loader = SkillsLoader()
    text = _frontmatter(tools_yaml)

    skill = loader._parse_skill_md(text, source_url="https://x/SKILL.md")
    meta = loader._parse_skill_md_metadata(text, source_url="https://x/SKILL.md")

    assert skill.allowed_tools == expected
    assert meta.allowed_tools == expected


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param("- a\n- list", id="top-level-list"),
        pytest.param("just a scalar", id="top-level-scalar"),
        pytest.param("name: [unclosed", id="malformed"),
    ],
)
def test_a_frontmatter_that_is_not_a_mapping_never_becomes_a_skill(frontmatter):
    """Either stage may raise or return None; neither may return a skill.

    The loaders that call these parsers catch per file, so raising is a skip.
    """
    loader = SkillsLoader()
    text = f"---\n{frontmatter}\n---\nBody."

    for parse in (loader._parse_skill_md, loader._parse_skill_md_metadata):
        try:
            result = parse(text, source_url="https://x/SKILL.md")
        except Exception:
            continue
        assert result is None


def test_a_multiline_description_reaches_the_skill_intact():
    text = (
        "---\nname: s\ndescription: >\n  Answers questions\n  about the graph.\n"
        "---\nBody."
    )
    skill = SkillsLoader()._parse_skill_md(text, source_url="https://x/SKILL.md")

    assert skill.description == "Answers questions about the graph."
