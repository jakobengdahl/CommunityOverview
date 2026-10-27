"""Tests for the shared non-object tool-input check used by every dispatcher."""

from collections import OrderedDict

import pytest

from backend.llm.llm_providers import tool_input_type_error


@pytest.mark.parametrize("tool_input", [{}, {"query": "x"}, OrderedDict(a=1)])
def test_any_mapping_that_is_a_dict_passes(tool_input):
    assert tool_input_type_error("search_graph", tool_input) is None


@pytest.mark.parametrize(
    "tool_input, got",
    [
        ([], "array"),
        ("x", "string"),
        (True, "boolean"),
        (5, "number"),
        (1.5, "number"),
        (None, "null"),
        ((1, 2), "tuple"),
    ],
)
def test_non_object_input_names_the_tool_and_its_json_type(tool_input, got):
    assert tool_input_type_error("search_graph", tool_input) == (
        "Tool 'search_graph' was not run: its arguments must be a JSON object, "
        f"got {got}."
    )
