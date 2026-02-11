import pytest
import time
import uuid
from unittest.mock import MagicMock, patch

from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService
from backend.agents import AgentRegistry, AgentsSettings, AgentConfig
from backend.core.events.dispatcher import EventDispatcher

class TestAgentFlow:
    @pytest.fixture
    def setup_components(self, tmp_path):
        """Setup the full stack of components."""
        # 1. Storage
        db_path = tmp_path / "graph.json"
        storage = GraphStorage(str(db_path))

        # 2. Service
        service = GraphService(storage)

        # 3. Settings (Mocked)
        settings = AgentsSettings(
            enabled=True,
            llm_provider="openai",
            openai_api_key="sk-test",
            mcp_integrations=[], # No external tools needed for this test
        )

        # 4. Registry
        registry = AgentRegistry(settings, storage, service)

        # 5. Enable events on storage
        storage.setup_events(enabled=True)

        # 6. Wire up Registry -> Dispatcher (Agent Delivery)
        def agent_delivery_callback(event, subscription_id: str) -> bool:
            if not registry.is_enabled:
                return False
            if not registry.is_agent_subscription(subscription_id):
                return False
            return registry.enqueue_for_subscription(subscription_id, event.to_webhook_payload())

        storage.set_agent_delivery_callback(agent_delivery_callback)

        # 7. Wire up System Listener (Lifecycle) - MIMICS server.py
        def agent_lifecycle_listener(event):
            if event.entity.kind != "node" or event.entity.type != "Agent":
                return

            node_id = event.entity.id
            if event.event_type == "node.create":
                registry.handle_agent_created(node_id)
            elif event.event_type == "node.update":
                registry.handle_agent_updated(node_id)
            elif event.event_type == "node.delete":
                registry.handle_agent_deleted(node_id)

        storage.add_system_listener(agent_lifecycle_listener)

        registry.start()

        yield storage, service, registry

        registry.stop()
        storage.shutdown_events()

    def test_full_agent_flow(self, setup_components):
        """
        Test the flow:
        1. Create Agent + Subscription
        2. Verify Worker Started
        3. Create Resource
        4. Verify Agent Triggered
        """
        storage, service, registry = setup_components

        # Mock the LLM Client in the registry's worker factory or patch where it's used
        # Since we just want to verify triggering, checking the queue is enough.

        # 1. Define Nodes
        sub_id = f"sub-{uuid.uuid4()}"
        agent_name = "Test Agent"

        subscription_node = {
            "id": sub_id,
            "name": f"{agent_name} - Subscription",
            "type": "EventSubscription",
            "metadata": {
                "filters": {
                    "target": {"entity_kind": "node", "node_types": ["Resource"]},
                    "operations": ["create"],
                    "keywords": {"any": []}
                },
                "delivery": {
                    "webhook_url": f"internal://agent/placeholder",
                    "ignore_origins": [],
                    "ignore_session_ids": []
                }
            }
        }

        agent_node = {
            "name": agent_name,
            "type": "Agent",
            "metadata": {
                "subscription_id": sub_id,
                "enabled": True,
                "prompts": {
                    "task_prompt": "Do something."
                },
                "mcp_integration_ids": []
            }
        }

        # 2. Add Agent and Subscription
        print("\nAdding Agent and Subscription...")
        result = service.add_nodes([subscription_node, agent_node], [])
        assert result["success"] is True

        # Get the actual Agent ID assigned
        agent_id = result["added_node_ids"][1]
        print(f"Agent ID: {agent_id}")

        # 3. Verify Worker Started
        # Allow a brief moment for the thread/listener to fire
        time.sleep(0.5)

        assert agent_id in registry._workers, "Worker should be running for the new agent"
        worker = registry._workers[agent_id]
        assert worker.is_running

        # Verify subscription mapping
        assert registry.get_agent_for_subscription(sub_id) == agent_id

        # 4. Create a matching Resource
        print("Adding Resource...")
        resource_node = {
            "name": "My New Resource",
            "type": "Resource",
            "description": "Some description"
        }

        # Mock the worker's enqueue method to verify it gets called
        with patch.object(worker, 'enqueue', wraps=worker.enqueue) as mock_enqueue:
            res_result = service.add_nodes([resource_node], [])
            assert res_result["success"] is True

            # 5. Verify Trigger
            # Event dispatch happens in background thread (delivery worker) or sync?
            # In storage.py, dispatch is called. Dispatcher logic runs sync, calls callback.
            # Callback calls registry.enqueue. Registry calls worker.enqueue.
            # So this should happen almost immediately.

            time.sleep(0.5)

            assert mock_enqueue.called, "Worker.enqueue should have been called"
            call_args = mock_enqueue.call_args
            event_payload = call_args[0][0]

            assert event_payload["event_type"] == "node.create"
            assert event_payload["entity"]["type"] == "Resource"
            assert event_payload["entity"]["data"]["after"]["name"] == "My New Resource"

            print("Agent triggered successfully!")
