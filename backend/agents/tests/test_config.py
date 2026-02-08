"""
Tests for agent configuration models.
"""

import pytest
import os
from unittest.mock import patch

from backend.agents.config import (
    MCPIntegration,
    AgentConfig,
    AgentsSettings,
)


class TestMCPIntegration:
    """Tests for MCPIntegration dataclass."""

    def test_create_http_integration(self):
        """Test creating an HTTP-based MCP integration."""
        integration = MCPIntegration(
            id="GRAPH",
            type="http",
            url="http://localhost:8000/mcp",
            description="Graph tools",
        )

        assert integration.id == "GRAPH"
        assert integration.type == "http"
        assert integration.url == "http://localhost:8000/mcp"
        assert integration.command is None
        assert integration.args is None

    def test_create_stdio_integration(self):
        """Test creating a stdio-based MCP integration."""
        integration = MCPIntegration(
            id="FS",
            type="stdio",
            command="/usr/bin/node",
            args=["mcp-fs-server", "--read-only"],
            description="Filesystem tools",
        )

        assert integration.id == "FS"
        assert integration.type == "stdio"
        assert integration.command == "/usr/bin/node"
        assert integration.args == ["mcp-fs-server", "--read-only"]
        assert integration.url is None

    def test_to_dict(self):
        """Test converting integration to dictionary."""
        integration = MCPIntegration(
            id="WEB",
            type="http",
            url="http://example.com/mcp",
            description="Web tools",
        )

        result = integration.to_dict()

        assert result["id"] == "WEB"
        assert result["type"] == "http"
        assert result["url"] == "http://example.com/mcp"
        assert result["description"] == "Web tools"


class TestAgentConfig:
    """Tests for AgentConfig model."""

    def test_from_node_basic(self, sample_agent_node):
        """Test creating AgentConfig from a mock node."""
        config = AgentConfig.from_node(sample_agent_node)

        assert config.agent_id == "agent-001"
        assert config.name == "Test Agent"
        assert config.enabled is True
        assert config.task_prompt == "Process events and log a summary."
        assert config.subscription_id == "sub-001"
        assert config.mcp_integrations == ["GRAPH"]

    def test_from_node_disabled(self, sample_agent_node):
        """Test AgentConfig with disabled agent."""
        sample_agent_node.metadata["agent"]["enabled"] = False

        config = AgentConfig.from_node(sample_agent_node)

        assert config.enabled is False

    def test_from_node_missing_agent_config(self, sample_agent_node):
        """Test AgentConfig with missing agent configuration."""
        del sample_agent_node.metadata["agent"]

        config = AgentConfig.from_node(sample_agent_node)

        # Should use defaults
        assert config.enabled is False
        assert config.task_prompt == ""
        assert config.mcp_integrations == []

    def test_from_node_multiple_integrations(self, sample_agent_node):
        """Test AgentConfig with multiple MCP integrations."""
        sample_agent_node.metadata["agent"]["mcp_integrations"] = [
            "GRAPH", "WEB", "SEARCH"
        ]

        config = AgentConfig.from_node(sample_agent_node)

        assert config.mcp_integrations == ["GRAPH", "WEB", "SEARCH"]


class TestAgentsSettings:
    """Tests for AgentsSettings global configuration."""

    def test_default_settings(self):
        """Test default settings when no environment variables set."""
        with patch.dict(os.environ, {}, clear=True):
            settings = AgentsSettings()

            assert settings.enabled is False
            assert settings.llm_provider == "openai"
            assert settings.max_events_per_minute == 60
            assert settings.max_turns_per_event == 10

    def test_from_env_enabled(self):
        """Test loading settings from environment with agents enabled."""
        env = {
            "AGENTS_ENABLED": "true",
            "LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "test-key",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = AgentsSettings.from_env()

            assert settings.enabled is True
            assert settings.llm_provider == "anthropic"
            assert settings.anthropic_api_key == "test-key"

    def test_from_env_disabled_by_default(self):
        """Test that agents are disabled by default."""
        with patch.dict(os.environ, {}, clear=True):
            settings = AgentsSettings.from_env()

            assert settings.enabled is False

    def test_from_env_with_mcp_integrations_json(self):
        """Test loading MCP integrations from JSON environment variable."""
        import json
        integrations = [
            {"id": "CUSTOM", "type": "http", "url": "http://custom.com/mcp"}
        ]
        env = {
            "AGENTS_ENABLED": "true",
            "MCP_INTEGRATIONS": json.dumps(integrations),
        }

        with patch.dict(os.environ, env, clear=True):
            settings = AgentsSettings.from_env()

            # Should include default GRAPH plus custom
            ids = [i.id for i in settings.mcp_integrations]
            assert "GRAPH" in ids
            assert "CUSTOM" in ids

    def test_default_mcp_integrations(self):
        """Test that GRAPH integration is included by default."""
        with patch.dict(os.environ, {"AGENTS_ENABLED": "true"}, clear=True):
            settings = AgentsSettings.from_env()

            ids = [i.id for i in settings.mcp_integrations]
            assert "GRAPH" in ids
