"""
Tests for agent configuration models.
"""

import os
from unittest.mock import patch

from backend.agents.config import (
    MCPIntegration,
    AgentConfig,
    AgentsSettings,
    MCPTransport,
    REDACTED_ENV_VALUE,
)
from backend.agents.secrets import SECRET_REF_PREFIX


class TestMCPIntegration:
    """Tests for MCPIntegration dataclass."""

    def test_create_http_integration(self):
        """Test creating an HTTP-based MCP integration."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
            description="Graph tools",
        )

        assert integration.id == "GRAPH"
        assert integration.transport == MCPTransport.HTTP
        assert integration.url == "http://localhost:8000/mcp"
        assert integration.command is None

    def test_create_stdio_integration(self):
        """Test creating a stdio-based MCP integration."""
        integration = MCPIntegration(
            id="FS",
            name="Filesystem",
            transport=MCPTransport.STDIO,
            command=["/usr/bin/node", "mcp-fs-server", "--read-only"],
            description="Filesystem tools",
        )

        assert integration.id == "FS"
        assert integration.transport == MCPTransport.STDIO
        assert integration.command == ["/usr/bin/node", "mcp-fs-server", "--read-only"]
        assert integration.url is None

    def test_to_dict(self):
        """Test converting integration to dictionary."""
        integration = MCPIntegration(
            id="WEB",
            name="Web Fetch",
            transport=MCPTransport.HTTP,
            url="http://example.com/mcp",
            description="Web tools",
        )

        result = integration.to_dict()

        assert result["id"] == "WEB"
        assert result["transport"] == "http"
        assert result["url"] == "http://example.com/mcp"
        assert result["description"] == "Web tools"

    def test_to_dict_redacts_literal_env_secret(self):
        """A literal env value is never serialized; secret:// refs survive."""
        integration = MCPIntegration(
            id="SEARCH",
            name="Brave Search",
            transport=MCPTransport.STDIO,
            command=["npx", "-y", "@anthropic/brave-search-mcp"],
            env={
                "BRAVE_API_KEY": "sk-literal-plaintext-secret",
                "REF_KEY": f"{SECRET_REF_PREFIX}BRAVE_API_KEY",
            },
        )

        result = integration.to_dict()

        assert "sk-literal-plaintext-secret" not in str(result)
        assert result["env"]["BRAVE_API_KEY"] == REDACTED_ENV_VALUE
        assert result["env"]["REF_KEY"] == f"{SECRET_REF_PREFIX}BRAVE_API_KEY"


class TestAgentConfig:
    """Tests for AgentConfig model."""

    def test_from_node_basic(self, sample_agent_node):
        """Test creating AgentConfig from a mock node."""
        config = AgentConfig.from_node(sample_agent_node)

        assert config.agent_id == "agent-001"
        assert config.name == "Test Agent"
        assert config.enabled is True
        assert config.prompts.task_prompt == "Process events and log a summary."
        assert config.subscription_id == "sub-001"
        assert config.mcp_integration_ids == ["GRAPH"]

    def test_from_node_disabled(self, sample_agent_node):
        """Test AgentConfig with disabled agent."""
        sample_agent_node.metadata["enabled"] = False

        config = AgentConfig.from_node(sample_agent_node)

        assert config.enabled is False

    def test_from_node_missing_agent_config(self, sample_agent_node):
        """Test AgentConfig with missing agent configuration."""
        # Clear metadata essentially
        sample_agent_node.metadata = {}

        config = AgentConfig.from_node(sample_agent_node)

        # Should use defaults
        assert config.enabled is True  # default is True
        assert config.prompts.task_prompt == ""
        assert config.mcp_integration_ids == []

    def test_from_node_multiple_integrations(self, sample_agent_node):
        """Test AgentConfig with multiple MCP integrations."""
        sample_agent_node.metadata["mcp_integration_ids"] = ["GRAPH", "WEB", "SEARCH"]

        config = AgentConfig.from_node(sample_agent_node)

        assert config.mcp_integration_ids == ["GRAPH", "WEB", "SEARCH"]


class TestAgentsSettings:
    """Tests for AgentsSettings global configuration."""

    def test_default_settings(self):
        """Test default settings when no environment variables set."""
        with patch.dict(os.environ, {}, clear=True):
            settings = AgentsSettings()

            assert settings.enabled is False
            assert settings.llm_provider == "openai"
            assert settings.max_agent_turns == 10

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

    def test_from_env_loads_model_profiles_from_schema_config(self, tmp_path):
        """Agent settings should use schema-configured model profiles."""
        import json
        from backend.config import config_loader

        config_file = tmp_path / "schema_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "schema": {"node_types": {}, "relationship_types": {}},
                    "model_profiles": {
                        "profiles": [
                            {
                                "id": "agent-fast",
                                "name": "Agent Fast",
                                "provider": "openai",
                                "model": "gpt-4o-mini",
                                "default": True,
                                "credential_ref": "OPENAI_API_KEY",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"SCHEMA_FILE": str(config_file)}, clear=True):
            config_loader.reset_loader()
            settings = AgentsSettings.from_env()

        assert [profile.id for profile in settings.model_profiles] == ["agent-fast"]
        assert settings.resolve_model_profile(None).profile.id == "agent-fast"
        config_loader.reset_loader()

    def test_from_env_with_mcp_integrations_json(self):
        """Test loading MCP integrations from JSON environment variable."""
        import json

        integrations = [
            {"id": "CUSTOM", "transport": "http", "url": "http://custom.com/mcp"}
        ]
        env = {
            "AGENTS_ENABLED": "true",
            "MCP_INTEGRATIONS": json.dumps(integrations),
        }

        with patch.dict(os.environ, env, clear=True):
            settings = AgentsSettings.from_env()

            # Should include custom only (defaults are skipped if env var provided)
            ids = [i.id for i in settings.mcp_integrations]
            assert "CUSTOM" in ids
            assert "GRAPH" not in ids

    def test_default_mcp_integrations(self):
        """Test that GRAPH integration is included by default."""
        with patch.dict(os.environ, {"AGENTS_ENABLED": "true"}, clear=True):
            settings = AgentsSettings.from_env()

            ids = [i.id for i in settings.mcp_integrations]
            assert "GRAPH" in ids
