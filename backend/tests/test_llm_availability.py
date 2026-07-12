"""
Unit tests for LLM availability detection in llm_providers.py.
"""
import os
import pytest
from unittest.mock import patch

from backend.llm.llm_providers import get_llm_availability


class TestGetLlmAvailability:
    """Tests for get_llm_availability()."""

    def test_returns_available_false_when_no_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("LLM_PROVIDER", None)
            result = get_llm_availability()
        assert result["available"] is False
        assert result["provider"] == "claude"
        assert result["has_anthropic_key"] is False
        assert result["has_openai_key"] is False

    def test_returns_available_true_when_anthropic_key_set(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "LLM_PROVIDER": "claude"}
        with patch.dict(os.environ, env, clear=False):
            result = get_llm_availability()
        assert result["available"] is True
        assert result["provider"] == "claude"
        assert result["has_anthropic_key"] is True

    def test_returns_available_true_when_openai_key_set(self):
        env = {"OPENAI_API_KEY": "sk-test", "LLM_PROVIDER": "openai"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = get_llm_availability()
        assert result["available"] is True
        assert result["provider"] == "openai"
        assert result["has_openai_key"] is True

    def test_returns_available_false_when_openai_provider_but_no_openai_key(self):
        env = {"LLM_PROVIDER": "openai"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            result = get_llm_availability()
        assert result["available"] is False
        assert result["provider"] == "openai"
        assert result["has_openai_key"] is False

    def test_returns_available_false_when_claude_provider_but_only_openai_key(self):
        env = {"OPENAI_API_KEY": "sk-test", "LLM_PROVIDER": "claude"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = get_llm_availability()
        assert result["available"] is False
        assert result["has_openai_key"] is True
        assert result["has_anthropic_key"] is False

    def test_defaults_to_claude_when_no_provider_env(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("LLM_PROVIDER", None)
            result = get_llm_availability()
        assert result["provider"] == "claude"
        assert result["available"] is True

    def test_whitespace_only_key_treated_as_missing(self):
        env = {"ANTHROPIC_API_KEY": "   ", "LLM_PROVIDER": "claude"}
        with patch.dict(os.environ, env, clear=False):
            result = get_llm_availability()
        assert result["available"] is False
        assert result["has_anthropic_key"] is False

    def test_returns_dict_with_expected_keys(self):
        with patch.dict(os.environ, {}, clear=False):
            result = get_llm_availability()
        assert set(result.keys()) == {"available", "provider", "has_anthropic_key", "has_openai_key"}
