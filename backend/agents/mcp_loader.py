"""
MCP Integration Loader for Agent Runtime.

Handles:
- Loading MCP server configurations
- Discovering tools from MCP servers
- Namespacing tools by integration ID
- Routing tool calls to the appropriate server
"""

import json
import logging
import subprocess
import threading
import queue
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
import requests

from .config import MCPIntegration, MCPTransport

logger = logging.getLogger(__name__)


@dataclass
class NamespacedTool:
    """A tool with its integration namespace."""
    integration_id: str
    original_name: str
    namespaced_name: str  # e.g., "GRAPH__search_graph"
    description: str
    input_schema: Dict[str, Any]


@dataclass
class MCPConnection:
    """Active connection to an MCP server."""
    integration: MCPIntegration
    tools: List[NamespacedTool] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_connected(self) -> bool:
        """Check if the connection is active."""
        if self.integration.transport == MCPTransport.STDIO:
            return self.process is not None and self.process.poll() is None
        return True  # HTTP connections are stateless


class MCPLoader:
    """
    Loads and manages MCP server integrations.

    Provides namespaced tool discovery and execution for agents.
    """

    def __init__(self, integrations: Optional[List[MCPIntegration]] = None):
        """
        Initialize the MCP loader.

        Args:
            integrations: List of MCP integrations to manage
        """
        self._integrations = integrations or []
        self._connections: Dict[str, MCPConnection] = {}
        self._tools_cache: Dict[str, NamespacedTool] = {}
        self._lock = threading.Lock()

    def add_integration(self, integration: MCPIntegration) -> None:
        """Add an integration to manage."""
        with self._lock:
            self._integrations.append(integration)

    def get_integration(self, integration_id: str) -> Optional[MCPIntegration]:
        """Get an integration by ID."""
        for integration in self._integrations:
            if integration.id == integration_id:
                return integration
        return None

    def connect_all(self) -> Dict[str, List[NamespacedTool]]:
        """
        Connect to all enabled integrations and discover tools.

        Returns:
            Dict mapping integration ID to list of discovered tools
        """
        results = {}
        for integration in self._integrations:
            if not integration.enabled:
                continue

            try:
                tools = self.connect(integration)
                results[integration.id] = tools
                logger.info(
                    f"Connected to MCP integration {integration.id}: "
                    f"{len(tools)} tools discovered"
                )
            except Exception as e:
                logger.error(f"Failed to connect to {integration.id}: {e}")
                results[integration.id] = []

        return results

    def connect(self, integration: MCPIntegration) -> List[NamespacedTool]:
        """
        Connect to a single MCP integration and discover tools.

        Args:
            integration: The integration to connect to

        Returns:
            List of namespaced tools from this integration
        """
        if integration.transport == MCPTransport.HTTP:
            return self._connect_http(integration)
        elif integration.transport == MCPTransport.STDIO:
            return self._connect_stdio(integration)
        else:
            raise ValueError(f"Unknown transport: {integration.transport}")

    def _connect_http(self, integration: MCPIntegration) -> List[NamespacedTool]:
        """Connect to an HTTP-based MCP server."""
        if not integration.url:
            raise ValueError(f"No URL configured for HTTP integration {integration.id}")

        # For HTTP MCP servers, we need to fetch the tool list
        # This depends on the MCP server implementation
        # For now, we'll try a few common approaches

        tools = []

        # Try to get tools from the server's info endpoint
        try:
            # First, try the execute_tool endpoint to get tool info
            # For our own graph MCP, we can query the available tools
            base_url = integration.url.replace("/sse", "").replace("/mcp", "")

            # Try info endpoint
            info_url = f"{base_url}/info"
            response = requests.get(info_url, timeout=5)
            if response.status_code == 200:
                info = response.json()
                # Our graph MCP includes tools in the info endpoint
                if "endpoints" in info:
                    # We know our graph MCP tools
                    tools = self._get_graph_mcp_tools(integration)

        except requests.RequestException as e:
            logger.warning(f"Could not query {integration.id} info: {e}")

        # If no tools discovered, use known tools for GRAPH integration
        if not tools and integration.id == "GRAPH":
            tools = self._get_graph_mcp_tools(integration)

        # Cache the connection
        conn = MCPConnection(integration=integration, tools=tools)
        with self._lock:
            self._connections[integration.id] = conn
            for tool in tools:
                self._tools_cache[tool.namespaced_name] = tool

        return tools

    def _get_graph_mcp_tools(self, integration: MCPIntegration) -> List[NamespacedTool]:
        """Get the known tools for the GRAPH MCP integration."""
        # These match the tools registered in mcp_tools.py
        graph_tools = [
            ("search_graph", "Search nodes in the graph", {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "node_types": {"type": "array", "items": {"type": "string"}},
                    "communities": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            }),
            ("get_node_details", "Get details for a specific node", {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Node ID"},
                },
                "required": ["node_id"],
            }),
            ("get_related_nodes", "Get nodes related to a specific node", {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Node ID"},
                    "max_depth": {"type": "integer", "default": 1},
                },
                "required": ["node_id"],
            }),
            ("add_nodes", "Add new nodes and edges to the graph", {
                "type": "object",
                "properties": {
                    "nodes": {"type": "array", "items": {"type": "object"}},
                    "edges": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["nodes", "edges"],
            }),
            ("update_node", "Update an existing node", {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "updates": {"type": "object"},
                },
                "required": ["node_id", "updates"],
            }),
            ("delete_nodes", "Delete nodes from the graph", {
                "type": "object",
                "properties": {
                    "node_ids": {"type": "array", "items": {"type": "string"}},
                    "confirmed": {"type": "boolean", "default": False},
                },
                "required": ["node_ids"],
            }),
            ("find_similar_nodes", "Find nodes with similar names", {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "node_type": {"type": "string"},
                    "threshold": {"type": "number", "default": 0.7},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["name"],
            }),
            ("get_graph_stats", "Get graph statistics", {
                "type": "object",
                "properties": {
                    "communities": {"type": "array", "items": {"type": "string"}},
                },
            }),
        ]

        return [
            NamespacedTool(
                integration_id=integration.id,
                original_name=name,
                namespaced_name=f"{integration.id}__{name}",
                description=desc,
                input_schema=schema,
            )
            for name, desc, schema in graph_tools
        ]

    def _connect_stdio(self, integration: MCPIntegration) -> List[NamespacedTool]:
        """
        Connect to a stdio-based MCP server.

        For PoC, we'll start the process and query for tools.
        In production, this would use proper MCP protocol.
        """
        if not integration.command:
            raise ValueError(f"No command configured for stdio integration {integration.id}")

        # For PoC, we'll define known tools for common MCP servers
        # In a full implementation, we'd use the MCP protocol to discover tools

        tools = []

        if integration.id == "WEB":
            tools = self._get_fetch_mcp_tools(integration)
        elif integration.id == "FS":
            tools = self._get_filesystem_mcp_tools(integration)
        elif integration.id == "SEARCH":
            tools = self._get_search_mcp_tools(integration)

        # Cache the connection (process will be started on first tool call)
        conn = MCPConnection(integration=integration, tools=tools)
        with self._lock:
            self._connections[integration.id] = conn
            for tool in tools:
                self._tools_cache[tool.namespaced_name] = tool

        return tools

    def _get_fetch_mcp_tools(self, integration: MCPIntegration) -> List[NamespacedTool]:
        """Get tools for the Fetch MCP server."""
        return [
            NamespacedTool(
                integration_id=integration.id,
                original_name="fetch",
                namespaced_name=f"{integration.id}__fetch",
                description="Fetch content from a URL and convert to markdown",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"},
                        "max_length": {"type": "integer", "description": "Max content length"},
                    },
                    "required": ["url"],
                },
            ),
        ]

    def _get_filesystem_mcp_tools(self, integration: MCPIntegration) -> List[NamespacedTool]:
        """Get tools for the Filesystem MCP server."""
        return [
            NamespacedTool(
                integration_id=integration.id,
                original_name="read_file",
                namespaced_name=f"{integration.id}__read_file",
                description="Read contents of a file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                    },
                    "required": ["path"],
                },
            ),
            NamespacedTool(
                integration_id=integration.id,
                original_name="write_file",
                namespaced_name=f"{integration.id}__write_file",
                description="Write content to a file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "content": {"type": "string", "description": "Content to write"},
                    },
                    "required": ["path", "content"],
                },
            ),
            NamespacedTool(
                integration_id=integration.id,
                original_name="list_directory",
                namespaced_name=f"{integration.id}__list_directory",
                description="List contents of a directory",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path"},
                    },
                    "required": ["path"],
                },
            ),
        ]

    def _get_search_mcp_tools(self, integration: MCPIntegration) -> List[NamespacedTool]:
        """Get tools for the Brave Search MCP server."""
        return [
            NamespacedTool(
                integration_id=integration.id,
                original_name="search",
                namespaced_name=f"{integration.id}__search",
                description="Search the web using Brave Search",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "count": {"type": "integer", "description": "Number of results", "default": 10},
                    },
                    "required": ["query"],
                },
            ),
        ]

    def get_all_tools(self) -> List[NamespacedTool]:
        """Get all discovered tools across all integrations."""
        with self._lock:
            return list(self._tools_cache.values())

    def get_tools_for_integrations(
        self,
        integration_ids: List[str],
    ) -> List[NamespacedTool]:
        """
        Get tools for specific integrations only.

        Args:
            integration_ids: List of integration IDs to include

        Returns:
            List of namespaced tools from those integrations
        """
        with self._lock:
            return [
                tool for tool in self._tools_cache.values()
                if tool.integration_id in integration_ids
            ]

    def get_tool(self, namespaced_name: str) -> Optional[NamespacedTool]:
        """Get a tool by its namespaced name."""
        with self._lock:
            return self._tools_cache.get(namespaced_name)

    def get_tool_definitions(
        self,
        integration_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get tool definitions in Claude format.

        Args:
            integration_ids: Optional list to filter by integration

        Returns:
            List of tool definitions for LLM
        """
        if integration_ids:
            tools = self.get_tools_for_integrations(integration_ids)
        else:
            tools = self.get_all_tools()

        return [
            {
                "name": tool.namespaced_name,
                "description": f"[{tool.integration_id}] {tool.description}",
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ]

    def create_tool_executor(
        self,
        graph_service: Optional[Any] = None,
        agent_id: Optional[str] = None,
    ) -> Callable[[str, Dict[str, Any]], Any]:
        """
        Create a tool executor function for an agent.

        Args:
            graph_service: GraphService instance for GRAPH integration
            agent_id: Agent ID for setting event origin

        Returns:
            Function that executes tools by namespaced name
        """

        def execute_tool(namespaced_name: str, input_args: Dict[str, Any]) -> Any:
            """Execute a namespaced tool."""
            tool = self.get_tool(namespaced_name)
            if not tool:
                return {"error": f"Unknown tool: {namespaced_name}"}

            integration_id = tool.integration_id
            original_name = tool.original_name

            # Route to appropriate executor
            if integration_id == "GRAPH":
                return self._execute_graph_tool(
                    original_name, input_args, graph_service, agent_id
                )
            elif integration_id == "WEB":
                return self._execute_fetch_tool(original_name, input_args)
            elif integration_id == "FS":
                return self._execute_fs_tool(original_name, input_args)
            elif integration_id == "SEARCH":
                return self._execute_search_tool(original_name, input_args)
            else:
                return {"error": f"No executor for integration: {integration_id}"}

        return execute_tool

    def _execute_graph_tool(
        self,
        tool_name: str,
        input_args: Dict[str, Any],
        graph_service: Any,
        agent_id: Optional[str],
    ) -> Any:
        """Execute a GRAPH integration tool."""
        if not graph_service:
            return {"error": "Graph service not available"}

        # Add agent origin for event tracking
        if agent_id and tool_name in ("add_nodes", "update_node", "delete_nodes"):
            input_args["event_origin"] = f"agent:{agent_id}"

        # Route to the appropriate GraphService method
        method = getattr(graph_service, tool_name, None)
        if not method:
            return {"error": f"Unknown graph tool: {tool_name}"}

        try:
            return method(**input_args)
        except Exception as e:
            logger.error(f"Graph tool {tool_name} failed: {e}")
            return {"error": str(e)}

    def _execute_fetch_tool(
        self,
        tool_name: str,
        input_args: Dict[str, Any],
    ) -> Any:
        """Execute a WEB/Fetch integration tool."""
        # For PoC, use requests directly
        if tool_name == "fetch":
            url = input_args.get("url")
            if not url:
                return {"error": "URL required"}

            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()

                # Simple HTML to text conversion
                content = response.text
                max_length = input_args.get("max_length", 10000)
                if len(content) > max_length:
                    content = content[:max_length] + "... (truncated)"

                return {
                    "url": url,
                    "status": response.status_code,
                    "content": content,
                }
            except Exception as e:
                return {"error": f"Fetch failed: {e}"}

        return {"error": f"Unknown fetch tool: {tool_name}"}

    def _execute_fs_tool(
        self,
        tool_name: str,
        input_args: Dict[str, Any],
    ) -> Any:
        """Execute a FS/Filesystem integration tool."""
        # For PoC, implement basic file operations in /tmp/agent-workspace
        import os
        base_path = "/tmp/agent-workspace"
        os.makedirs(base_path, exist_ok=True)

        path = input_args.get("path", "")
        if not path:
            return {"error": "Path required"}

        # Security: ensure path is within workspace
        full_path = os.path.normpath(os.path.join(base_path, path))
        if not full_path.startswith(base_path):
            return {"error": "Path must be within agent workspace"}

        try:
            if tool_name == "read_file":
                with open(full_path, "r") as f:
                    return {"path": path, "content": f.read()}

            elif tool_name == "write_file":
                content = input_args.get("content", "")
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w") as f:
                    f.write(content)
                return {"path": path, "written": len(content)}

            elif tool_name == "list_directory":
                if not os.path.isdir(full_path):
                    return {"error": "Not a directory"}
                entries = os.listdir(full_path)
                return {"path": path, "entries": entries}

            else:
                return {"error": f"Unknown fs tool: {tool_name}"}

        except Exception as e:
            return {"error": str(e)}

    def _execute_search_tool(
        self,
        tool_name: str,
        input_args: Dict[str, Any],
    ) -> Any:
        """Execute a SEARCH/Brave integration tool."""
        import os
        api_key = os.environ.get("BRAVE_API_KEY")
        if not api_key:
            return {"error": "Brave API key not configured"}

        if tool_name == "search":
            query = input_args.get("query")
            if not query:
                return {"error": "Query required"}

            count = input_args.get("count", 10)

            try:
                response = requests.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": count},
                    headers={"X-Subscription-Token": api_key},
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()

                # Extract relevant results
                results = []
                for item in data.get("web", {}).get("results", [])[:count]:
                    results.append({
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "description": item.get("description"),
                    })

                return {"query": query, "results": results}

            except Exception as e:
                return {"error": f"Search failed: {e}"}

        return {"error": f"Unknown search tool: {tool_name}"}

    def disconnect_all(self) -> None:
        """Disconnect from all integrations."""
        with self._lock:
            for conn in self._connections.values():
                if conn.process:
                    try:
                        conn.process.terminate()
                        conn.process.wait(timeout=5)
                    except Exception:
                        conn.process.kill()

            self._connections.clear()
            self._tools_cache.clear()
