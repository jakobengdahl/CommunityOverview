"""
Agent Registry for Managing Agent Workers.

The registry:
- Loads Agent nodes from the graph at startup
- Starts workers for enabled agents
- Handles agent lifecycle (create, update, delete)
- Routes events to agent queues
"""

import logging
import threading
from typing import Dict, Any, Optional, List, Callable, TYPE_CHECKING

from .config import AgentConfig, AgentsSettings
from .worker import AgentWorker, ProcessingResult
from .mcp_loader import MCPLoader

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from backend.service import GraphService

logger = logging.getLogger(__name__)


class AgentRegistry:
    """
    Central registry for managing agent workers.

    Responsibilities:
    - Load agent configurations from the graph
    - Start/stop/reload agent workers
    - Route events to appropriate agent queues
    - Handle agent node mutations (create/update/delete)
    """

    def __init__(
        self,
        settings: AgentsSettings,
        graph_storage: "GraphStorage",
        graph_service: "GraphService",
    ):
        """
        Initialize the agent registry.

        Args:
            settings: Global agent settings
            graph_storage: GraphStorage for reading agent nodes
            graph_service: GraphService for agent tool calls
        """
        self.settings = settings
        self._storage = graph_storage
        self._service = graph_service

        # Active workers
        self._workers: Dict[str, AgentWorker] = {}
        self._lock = threading.Lock()

        # MCP loader (shared across agents)
        self._mcp_loader: Optional[MCPLoader] = None

        # Subscription to agent mapping
        # Maps subscription_id -> agent_id
        self._subscription_agent_map: Dict[str, str] = {}

        # Result callback
        self._on_result: Optional[Callable[[ProcessingResult], None]] = None

    @property
    def is_enabled(self) -> bool:
        """Check if the agent system is enabled."""
        return self.settings.enabled

    def set_result_callback(
        self,
        callback: Callable[[ProcessingResult], None],
    ) -> None:
        """Set a callback for processing results."""
        self._on_result = callback

    def _ensure_initialized(self) -> bool:
        """
        Ensure the agent runtime is initialized (MCP loader connected).

        Called lazily when agents are created dynamically, handling the case
        where AGENTS_ENABLED was not set at startup but a user creates an
        agent via the UI.

        Returns:
            True if initialized successfully, False on error.
        """
        if self._mcp_loader is not None:
            return True

        self._mcp_loader = MCPLoader(self.settings.mcp_integrations)

        try:
            tool_results = self._mcp_loader.connect_all()
            total_tools = sum(len(tools) for tools in tool_results.values())
            logger.info(
                f"MCP loader initialized: {total_tools} tools "
                f"from {len(tool_results)} integrations"
            )
        except Exception as e:
            logger.error(f"Failed to initialize MCP loader: {e}")
            self._mcp_loader = None
            return False

        return True

    def start(self) -> None:
        """
        Start the agent registry.

        Loads agent configurations and starts workers for enabled agents.
        """
        if not self.settings.enabled:
            logger.info("Agent system is disabled, skipping startup")
            return

        logger.info("Starting agent registry...")

        # Initialize MCP loader with configured integrations
        if not self._ensure_initialized():
            return

        # Load and start agents
        agents = self._load_agents()
        logger.info(f"Found {len(agents)} agent node(s)")

        started_count = 0
        for agent_config in agents:
            if agent_config.enabled:
                try:
                    self._start_worker(agent_config)
                    started_count += 1
                except Exception as e:
                    logger.error(f"Failed to start agent {agent_config.name}: {e}")

        logger.info(f"Agent registry started: {started_count} agent worker(s) running")

    def stop(self) -> None:
        """Stop all agent workers."""
        logger.info("Stopping agent registry...")

        with self._lock:
            for agent_id, worker in list(self._workers.items()):
                try:
                    worker.stop(wait=True, timeout=5.0)
                except Exception as e:
                    logger.error(f"Error stopping worker {agent_id}: {e}")

            self._workers.clear()
            self._subscription_agent_map.clear()

        # Disconnect MCP loader
        if self._mcp_loader:
            self._mcp_loader.disconnect_all()

        logger.info("Agent registry stopped")

    def _load_agents(self) -> List[AgentConfig]:
        """Load all Agent nodes from the graph."""
        agents = []

        for node in self._storage.nodes.values():
            node_type = node.type.value if hasattr(node.type, "value") else str(node.type)
            if node_type != "Agent":
                continue

            try:
                config = AgentConfig.from_node(node)
                agents.append(config)
            except Exception as e:
                logger.warning(f"Failed to parse agent node {node.id}: {e}")

        return agents

    def _start_worker(self, config: AgentConfig) -> None:
        """Start a worker for an agent."""
        with self._lock:
            if config.agent_id in self._workers:
                logger.warning(f"Worker already exists for agent {config.agent_id}")
                return

            worker = AgentWorker(
                config=config,
                settings=self.settings,
                mcp_loader=self._mcp_loader,
                graph_service=self._service,
                on_result=self._on_result,
            )
            worker.start()
            self._workers[config.agent_id] = worker

            # Register subscription mapping
            if config.subscription_id:
                self._subscription_agent_map[config.subscription_id] = config.agent_id
                logger.debug(
                    f"Registered subscription {config.subscription_id} -> agent {config.agent_id}"
                )

    def _stop_worker(self, agent_id: str) -> None:
        """Stop a worker for an agent."""
        with self._lock:
            worker = self._workers.pop(agent_id, None)
            if worker:
                # Remove subscription mapping
                sub_id = worker.config.subscription_id
                if sub_id and sub_id in self._subscription_agent_map:
                    del self._subscription_agent_map[sub_id]

                worker.stop(wait=True)

    def enqueue(self, agent_id: str, event_payload: Dict[str, Any]) -> bool:
        """
        Enqueue an event for a specific agent.

        Args:
            agent_id: Target agent ID
            event_payload: Event payload

        Returns:
            True if event was enqueued, False if agent not found
        """
        with self._lock:
            worker = self._workers.get(agent_id)
            if not worker:
                logger.warning(f"No worker found for agent {agent_id}")
                return False

            worker.enqueue(event_payload)
            return True

    def enqueue_for_subscription(
        self,
        subscription_id: str,
        event_payload: Dict[str, Any],
    ) -> bool:
        """
        Enqueue an event for the agent linked to a subscription.

        Args:
            subscription_id: EventSubscription node ID
            event_payload: Event payload

        Returns:
            True if event was enqueued, False if no agent found
        """
        with self._lock:
            agent_id = self._subscription_agent_map.get(subscription_id)
            if not agent_id:
                return False

            worker = self._workers.get(agent_id)
            if not worker:
                logger.warning(
                    f"Agent {agent_id} registered for subscription {subscription_id} "
                    f"but no worker found"
                )
                return False

            worker.enqueue(event_payload)
            logger.debug(
                f"Routed event to agent {agent_id} via subscription {subscription_id}"
            )
            return True

    def is_agent_subscription(self, subscription_id: str) -> bool:
        """Check if a subscription is linked to an agent."""
        with self._lock:
            return subscription_id in self._subscription_agent_map

    def get_agent_for_subscription(self, subscription_id: str) -> Optional[str]:
        """Get the agent ID for a subscription."""
        with self._lock:
            return self._subscription_agent_map.get(subscription_id)

    def handle_agent_created(self, node_id: str) -> None:
        """Handle creation of a new Agent node."""
        node = self._storage.get_node(node_id)
        if not node:
            return

        node_type = node.type.value if hasattr(node.type, "value") else str(node.type)
        if node_type != "Agent":
            return

        try:
            config = AgentConfig.from_node(node)
            if config.enabled:
                # Ensure runtime is initialized (handles dynamic creation
                # when AGENTS_ENABLED was not set at startup)
                if not self._ensure_initialized():
                    logger.error(
                        f"Cannot start agent {config.name}: "
                        f"MCP initialization failed"
                    )
                    return

                # Auto-enable agent system when agents are created dynamically
                if not self.settings.enabled:
                    self.settings.enabled = True
                    logger.info(
                        "Agent system auto-enabled (agent created dynamically)"
                    )

                self._start_worker(config)
                logger.info(f"Started worker for new agent: {config.name}")
        except Exception as e:
            logger.error(f"Failed to start worker for new agent {node_id}: {e}")

    def handle_agent_updated(self, node_id: str) -> None:
        """Handle update of an Agent node."""
        node = self._storage.get_node(node_id)
        if not node:
            # Node was deleted
            self._stop_worker(node_id)
            return

        node_type = node.type.value if hasattr(node.type, "value") else str(node.type)
        if node_type != "Agent":
            return

        try:
            config = AgentConfig.from_node(node)

            with self._lock:
                existing_worker = self._workers.get(node_id)

                if config.enabled:
                    if existing_worker:
                        # Update existing worker
                        existing_worker.reload_config(config)

                        # Update subscription mapping
                        old_sub_id = None
                        for sub_id, agent_id in list(self._subscription_agent_map.items()):
                            if agent_id == node_id:
                                old_sub_id = sub_id
                                break

                        if old_sub_id and old_sub_id != config.subscription_id:
                            del self._subscription_agent_map[old_sub_id]

                        if config.subscription_id:
                            self._subscription_agent_map[config.subscription_id] = node_id

                        logger.info(f"Reloaded agent: {config.name}")
                    else:
                        # Start new worker - ensure runtime is initialized
                        self._lock.release()
                        try:
                            if self._ensure_initialized():
                                if not self.settings.enabled:
                                    self.settings.enabled = True
                                    logger.info(
                                        "Agent system auto-enabled "
                                        "(agent enabled dynamically)"
                                    )
                                self._start_worker(config)
                                logger.info(
                                    f"Started worker for enabled agent: "
                                    f"{config.name}"
                                )
                            else:
                                logger.error(
                                    f"Cannot start agent {config.name}: "
                                    f"MCP initialization failed"
                                )
                        finally:
                            self._lock.acquire()
                else:
                    if existing_worker:
                        # Stop disabled worker
                        self._lock.release()
                        try:
                            self._stop_worker(node_id)
                            logger.info(f"Stopped worker for disabled agent: {config.name}")
                        finally:
                            self._lock.acquire()

        except Exception as e:
            logger.error(f"Failed to handle agent update {node_id}: {e}")

    def handle_agent_deleted(self, node_id: str) -> None:
        """Handle deletion of an Agent node."""
        self._stop_worker(node_id)
        logger.info(f"Stopped worker for deleted agent: {node_id}")

    def get_worker_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific worker."""
        with self._lock:
            worker = self._workers.get(agent_id)
            if worker:
                return worker.get_status()
        return None

    def get_all_status(self) -> Dict[str, Any]:
        """Get status of all workers and the registry."""
        with self._lock:
            workers_status = {
                agent_id: worker.get_status()
                for agent_id, worker in self._workers.items()
            }

            return {
                "enabled": self.settings.enabled,
                "worker_count": len(self._workers),
                "subscription_count": len(self._subscription_agent_map),
                "mcp_integrations": [
                    i.to_dict() for i in self.settings.mcp_integrations
                ],
                "workers": workers_status,
            }

    def list_workers(self) -> List[str]:
        """List all active worker agent IDs."""
        with self._lock:
            return list(self._workers.keys())

    def get_available_mcp_integrations(self) -> List[Dict[str, Any]]:
        """Get list of available MCP integrations for UI."""
        return [i.to_dict() for i in self.settings.mcp_integrations]
