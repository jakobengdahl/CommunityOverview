import os
from pathlib import Path

import pytest

from backend.llm.language_policy import format_language_policy_for_prompt


@pytest.fixture(autouse=True)
def configured_test_schema():
    from backend import config_loader

    test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test" / "schema_config.json")
    os.environ["SCHEMA_FILE"] = test_config_path
    config_loader.reset_loader()
    yield
    config_loader.reset_loader()
    del os.environ["SCHEMA_FILE"]


def test_system_prompt_includes_language_policy():
    from backend.ui.chat_logic import _build_system_prompt

    prompt = _build_system_prompt()

    assert "LANGUAGE POLICY:" in prompt
    assert "Mode: required" in prompt
    assert "Primary language for graph content: en" in prompt
    assert "Allowed languages for graph content: en" in prompt
    assert "Graph content must be written in English." in prompt


def test_external_language_policy_instructions_allow_user_language_separately():
    from backend import config_loader

    instructions = format_language_policy_for_prompt(config_loader.get_presentation(), external_agent=True)

    assert "LANGUAGE POLICY:" in instructions
    assert "Mode: required" in instructions
    assert "Primary language for graph content: en" in instructions
    assert "Allowed languages for graph content: en" in instructions
    assert "Graph content must be written in English." in instructions
    assert "You may still respond to the user in their own language" in instructions
