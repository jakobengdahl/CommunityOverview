"""
Configuration models for the agent runtime.

Defines settings for agents, MCP integrations, and global agent system configuration.
"""

import os
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum

_WEEKDAY_NAMES: Dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_DISPLAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class AgentSchedule:
    """
    Single time-based trigger for an agent.

    day_of_week: 0=Monday … 6=Sunday
    hour/minute: local time in the given timezone
    """
    day_of_week: int
    hour: int
    minute: int
    timezone: str = "UTC"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["AgentSchedule"]:
        """
        Parse schedule from a metadata dict.  Returns None when data is missing
        or contains invalid values — callers should treat None as "no schedule".

        Accepted formats::

            # integer weekday (0=Mon)
            {"day_of_week": 1, "time": "14:00", "timezone": "Europe/Stockholm"}

            # weekday name
            {"day_of_week": "tuesday", "hour": 14, "minute": 0}
        """
        if not data:
            return None

        raw_day = data.get("day_of_week")
        if raw_day is None:
            return None
        if isinstance(raw_day, str):
            day = _WEEKDAY_NAMES.get(raw_day.lower())
            if day is None:
                return None
        elif isinstance(raw_day, int):
            day = raw_day
        else:
            return None

        if not 0 <= day <= 6:
            return None

        raw_time = data.get("time")
        if raw_time and isinstance(raw_time, str):
            try:
                parts = raw_time.split(":")
                hour = int(parts[0])
                minute = int(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError):
                return None
        else:
            try:
                hour = int(data.get("hour", 0))
                minute = int(data.get("minute", 0))
            except (TypeError, ValueError):
                return None

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None

        tz_str = str(data.get("timezone", "UTC"))
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, KeyError):
            return None

        return cls(day_of_week=day, hour=hour, minute=minute, timezone=tz_str)

    @property
    def day_name(self) -> str:
        return _WEEKDAY_DISPLAY[self.day_of_week]

    def to_cron(self) -> str:
        """
        Return a standard 5-field cron expression for this schedule.

        Cron day-of-week convention: 0=Sunday, 1=Monday … 6=Saturday.
        Python weekday convention:   0=Monday … 6=Sunday.
        Conversion: cron_dow = (python_dow + 1) % 7
        """
        cron_dow = (self.day_of_week + 1) % 7
        return f"{self.minute} {self.hour} * * {cron_dow}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "day_of_week": self.day_of_week,
            "day_name": self.day_name,
            "hour": self.hour,
            "minute": self.minute,
            "timezone": self.timezone,
            "cron": self.to_cron(),
        }


class MCPTransport(str, Enum):
    """Transport type for MCP server connections."""
    HTTP = "http"  # HTTP/SSE transport
    STDIO = "stdio"  # stdio transport (subprocess)


@dataclass
class MCPIntegration:
    """
    Configuration for a single MCP server integration.

    Attributes:
        id: Unique identifier used as namespace prefix (e.g., "GRAPH", "WEB")
        name: Human-readable name
        description: Description of what this integration provides
        transport: How to connect (http or stdio)
        url: URL for HTTP transport (e.g., "http://localhost:8000/mcp/sse")
        command: Command for stdio transport (e.g., ["npx", "-y", "@anthropic/fetch-mcp"])
        env: Environment variables to pass to stdio subprocess
        enabled: Whether this integration is active
    """
    id: str
    name: str
    description: str = ""
    transport: MCPTransport = MCPTransport.HTTP
    url: Optional[str] = None
    command: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "transport": self.transport.value,
            "url": self.url,
            "command": self.command,
            "env": {k: v for k, v in self.env.items()},  # Don't expose secrets
            "enabled": self.enabled,
        }


@dataclass
class AgentPrompts:
    """Prompts configuration for an agent."""
    task_prompt: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentPrompts":
        return cls(
            task_prompt=data.get("task_prompt", ""),
        )


@dataclass
class AgentConfig:
    """
    Configuration for a single agent instance.

    Parsed from Agent node metadata in the graph.
    """
    agent_id: str
    name: str
    enabled: bool = True
    subscription_id: Optional[str] = None
    mcp_integration_ids: List[str] = field(default_factory=list)
    prompts: AgentPrompts = field(default_factory=AgentPrompts)
    tool_allowlist: Optional[List[str]] = None
    # Skills: URLs to SKILL.md files or GitHub repos (loaded at runtime)
    skills_urls: List[str] = field(default_factory=list)
    # Skill node IDs in the graph (linked via USES_SKILL edges)
    skill_node_ids: List[str] = field(default_factory=list)
    schedule: Optional[AgentSchedule] = None

    @classmethod
    def from_node(cls, node: Any) -> "AgentConfig":
        """
        Create AgentConfig from an Agent node.

        Args:
            node: A Node object with type="Agent"

        Returns:
            AgentConfig instance
        """
        metadata = node.metadata or {}

        prompts_data = metadata.get("prompts", {})
        if isinstance(prompts_data, str):
            # Support legacy format where prompts is just the task_prompt string
            prompts_data = {"task_prompt": prompts_data}

        return cls(
            agent_id=node.id,
            name=node.name,
            enabled=metadata.get("enabled", True),
            subscription_id=metadata.get("subscription_id"),
            mcp_integration_ids=metadata.get("mcp_integration_ids", []),
            prompts=AgentPrompts.from_dict(prompts_data),
            tool_allowlist=metadata.get("tool_allowlist"),
            skills_urls=metadata.get("skills_urls", []),
            skill_node_ids=metadata.get("skill_node_ids", []),
            schedule=AgentSchedule.from_dict(metadata.get("schedule") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "enabled": self.enabled,
            "subscription_id": self.subscription_id,
            "mcp_integration_ids": self.mcp_integration_ids,
            "prompts": {"task_prompt": self.prompts.task_prompt},
            "tool_allowlist": self.tool_allowlist,
            "skills_urls": self.skills_urls,
            "skill_node_ids": self.skill_node_ids,
            "schedule": self.schedule.to_dict() if self.schedule else None,
        }


@dataclass
class AgentsSettings:
    """
    Global settings for the agent runtime system.

    Loaded from environment variables and/or config file.
    """
    enabled: bool = False
    # scheduler_enabled: run the in-process time-based scheduler.
    # Keep False (default) when the deployment platform can scale to zero —
    # use POST /agents/{id}/trigger from an external scheduler (e.g. GCP Cloud
    # Scheduler) instead.
    scheduler_enabled: bool = False
    llm_provider: str = "openai"  # "openai" or "anthropic"
    llm_model: Optional[str] = None  # If None, uses provider default
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    mcp_integrations: List[MCPIntegration] = field(default_factory=list)
    max_agent_turns: int = 10  # Max LLM turns per event processing
    event_timeout: float = 60.0  # Timeout for processing a single event

    @classmethod
    def from_env(cls) -> "AgentsSettings":
        """
        Load settings from environment variables.

        Environment variables:
            AGENTS_ENABLED: "true" or "false" (default: false)
            AGENTS_SCHEDULER_ENABLED: "true" or "false" (default: false).
                Keep false on scale-to-zero deployments; use the external
                POST /agents/{id}/trigger endpoint instead.
            LLM_PROVIDER: "openai" or "anthropic" (default: openai)
            LLM_MODEL: Model name (optional, uses provider default)
            OPENAI_API_KEY: OpenAI API key
            ANTHROPIC_API_KEY: Anthropic API key
            MCP_INTEGRATIONS: JSON array of integration configs (optional)
            AGENTS_MAX_TURNS: Max LLM turns per event (default: 10)
            AGENTS_EVENT_TIMEOUT: Event processing timeout in seconds (default: 60)
        """
        # Parse enabled flag
        enabled_str = os.environ.get("AGENTS_ENABLED", "false").lower()
        enabled = enabled_str in ("true", "1", "yes")

        scheduler_enabled_str = os.environ.get("AGENTS_SCHEDULER_ENABLED", "false").lower()
        scheduler_enabled = scheduler_enabled_str in ("true", "1", "yes")

        # Get LLM settings (share with chat service)
        llm_provider = os.environ.get("LLM_PROVIDER", "openai").lower()
        llm_model = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")

        # Get API keys
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")

        # Parse MCP integrations from JSON
        mcp_integrations = []
        mcp_json = os.environ.get("MCP_INTEGRATIONS")
        if mcp_json:
            try:
                integrations_data = json.loads(mcp_json)
                for item in integrations_data:
                    integration = MCPIntegration(
                        id=item["id"],
                        name=item.get("name", item["id"]),
                        description=item.get("description", ""),
                        transport=MCPTransport(item.get("transport", "http")),
                        url=item.get("url"),
                        command=item.get("command"),
                        env=item.get("env", {}),
                        enabled=item.get("enabled", True),
                    )
                    mcp_integrations.append(integration)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Warning: Failed to parse MCP_INTEGRATIONS: {e}")

        # Add default integrations if none configured
        if not mcp_integrations:
            mcp_integrations = cls._get_default_integrations()

        return cls(
            enabled=enabled,
            scheduler_enabled=scheduler_enabled,
            llm_provider=llm_provider,
            llm_model=llm_model,
            openai_api_key=openai_api_key,
            anthropic_api_key=anthropic_api_key,
            mcp_integrations=mcp_integrations,
            max_agent_turns=int(os.environ.get("AGENTS_MAX_TURNS", "10")),
            event_timeout=float(os.environ.get("AGENTS_EVENT_TIMEOUT", "60")),
        )

    @staticmethod
    def _get_default_integrations() -> List[MCPIntegration]:
        """
        Get default MCP integrations for development.

        These are the built-in and common MCP servers.
        """
        integrations = []

        # GRAPH: Internal graph MCP (always available via local endpoint)
        port = os.environ.get("PORT", "8000")
        integrations.append(MCPIntegration(
            id="GRAPH",
            name="Graph API",
            description="Read and write to the knowledge graph",
            transport=MCPTransport.HTTP,
            url=f"http://localhost:{port}/mcp/sse",
            enabled=True,
        ))

        # WEB: Fetch MCP server for web content
        integrations.append(MCPIntegration(
            id="WEB",
            name="Web Fetch",
            description="Fetch and convert web content",
            transport=MCPTransport.STDIO,
            command=["npx", "-y", "@anthropic/fetch-mcp"],
            enabled=True,
        ))

        # FS: Filesystem MCP server
        integrations.append(MCPIntegration(
            id="FS",
            name="Filesystem",
            description="Read and write files",
            transport=MCPTransport.STDIO,
            command=["npx", "-y", "@anthropic/filesystem-mcp", "/tmp/agent-workspace"],
            enabled=True,
        ))

        # SEARCH: Brave Search MCP (only if API key is available)
        brave_api_key = os.environ.get("BRAVE_API_KEY")
        if brave_api_key:
            integrations.append(MCPIntegration(
                id="SEARCH",
                name="Brave Search",
                description="Search the web using Brave Search",
                transport=MCPTransport.STDIO,
                command=["npx", "-y", "@anthropic/brave-search-mcp"],
                env={"BRAVE_API_KEY": brave_api_key},
                enabled=True,
            ))

        return integrations

    def get_integration(self, integration_id: str) -> Optional[MCPIntegration]:
        """Get an MCP integration by ID."""
        for integration in self.mcp_integrations:
            if integration.id == integration_id:
                return integration
        return None

    def get_enabled_integrations(self) -> List[MCPIntegration]:
        """Get all enabled MCP integrations."""
        return [i for i in self.mcp_integrations if i.enabled]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses (excludes secrets)."""
        return {
            "enabled": self.enabled,
            "scheduler_enabled": self.scheduler_enabled,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "max_agent_turns": self.max_agent_turns,
            "event_timeout": self.event_timeout,
            "mcp_integrations": [i.to_dict() for i in self.mcp_integrations],
        }
