"""
Agent Runtime Package

Provides background agent execution for the knowledge graph system.
Agents receive events via EventSubscriptions and can use MCP tools to act on them.

Components:
- AgentRegistry: Manages agent workers lifecycle
- AgentWorker: Background worker that processes events for a single agent
- MCPLoader: Loads and namespaces tools from MCP servers
- LLMClient: Wrapper for LLM API calls (OpenAI/Anthropic)
"""

from .config import AgentConfig, MCPIntegration, AgentsSettings
from .registry import AgentRegistry
from .worker import AgentWorker
from .mcp_loader import MCPLoader
from .llm_client import LLMClient

__all__ = [
    "AgentConfig",
    "MCPIntegration",
    "AgentsSettings",
    "AgentRegistry",
    "AgentWorker",
    "MCPLoader",
    "LLMClient",
]
