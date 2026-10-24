"""Application composition selects collaborators before anything captures defaults."""

from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.agents import AgentRegistry, AgentsSettings
from backend.agents.config import AgentConfig
from backend.agents.worker import AgentWorker
from backend.api_host import create_app, create_worker_runtime
from backend.api_host import server
from backend.core import GraphStorage, Node
from backend.core.events.delivery import DeliveryWorker
from backend.core.events.dispatcher import EventDispatcher
from backend.core.events.tests.test_dispatcher import create_event
from backend.core.session_store import SessionStore
from backend.federation import FederationFileConfig
from backend.functions.broker import BrokerContext, FunctionBroker
from backend.functions.runner import RunSpec
from backend.runtime.authorization import (
    DefaultGraphAuthorizationHook,
    GraphAuthorizationDecision,
)
from backend.runtime.extensions import (
    AdmissionDecision,
    DeliveryOutcome,
    MutationPolicyDecision,
    RuntimeExtensions,
    get_runtime_extensions,
)


@pytest.fixture(autouse=True)
def local_background_work(monkeypatch):
    """Keep construction real, with no network or background execution."""
    monkeypatch.setattr(server, "load_federation_config", FederationFileConfig)
    monkeypatch.setattr(DeliveryWorker, "start", Mock())
    monkeypatch.setattr(AgentWorker, "start", Mock())
    monkeypatch.setattr(server, "recover_import_jobs", Mock())
    monkeypatch.setenv("AGENTS_ENABLED", "false")


@pytest.fixture(params=[create_app, create_worker_runtime], ids=["app", "worker"])
def compose(request, app_config):
    runtimes = []

    def build(**kwargs):
        result = request.param(app_config, **kwargs)
        runtime = result.state.runtime if request.param is create_app else result
        runtimes.append(runtime)
        return runtime

    yield build
    for runtime in reversed(runtimes):
        runtime.stop()


@pytest.mark.asyncio
async def test_zero_argument_uvicorn_factory_keeps_defaults(app_config, monkeypatch):
    monkeypatch.setattr(server.AppConfig, "from_env", lambda: app_config)
    default = get_runtime_extensions()
    app = server.get_app()
    try:
        assert app.state.runtime.graph_storage is app.state.graph_storage
        assert app.state.graph_storage.runtime_extensions is default
        assert isinstance(
            app.state.graph_service.authorization_hook, DefaultGraphAuthorizationHook
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
        assert app.state.graph_service.get_node_details("node-1")["success"]
    finally:
        app.state.runtime.stop()


def test_injected_authorization_hook_controls_reads_and_writes(compose):
    hook = Mock()
    hook.evaluate.return_value = GraphAuthorizationDecision(True)
    runtime = compose(authorization_hook=hook)
    service = runtime.graph_service
    assert service.authorization_hook is hook
    assert service.get_node_details("node-1")["success"]
    assert service.add_nodes([{"id": "new", "name": "New", "type": "Actor"}], [])[
        "success"
    ]
    assert {call.args[0].action for call in hook.evaluate.call_args_list} == {
        "read",
        "mutate",
    }

    hook.evaluate.return_value = GraphAuthorizationDecision(False, "injected_denial")
    assert not service.get_node_details("node-1")["success"]
    assert not service.add_nodes(
        [{"id": "denied", "name": "Denied", "type": "Actor"}], []
    )["success"]
    assert "denied" not in runtime.graph_storage.nodes


def test_extensions_precede_construction_and_reach_all_execution_paths(
    compose, monkeypatch
):
    default = get_runtime_extensions()
    policy, admission, adapter = Mock(), Mock(), Mock()
    policy.evaluate.return_value = MutationPolicyDecision()
    admission.admit.return_value = AdmissionDecision("deny", "execution_blocked")
    adapter.deliver.side_effect = lambda subscription, event, context: DeliveryOutcome(
        "delivered", context.delivery_id
    )
    extensions = RuntimeExtensions(
        mutation_policy=policy, execution_admission=admission, event_delivery=adapter
    )
    constructed = set()

    def guard(cls):
        original = cls.__init__

        def checked(self, *args, **kwargs):
            assert get_runtime_extensions() is extensions, cls.__name__
            constructed.add(cls.__name__)
            original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", checked)

    for cls in (
        GraphStorage,
        DeliveryWorker,
        EventDispatcher,
        AgentRegistry,
        AgentWorker,
        SessionStore,
        server.SqliteExecutionStore,
    ):
        guard(cls)
    monkeypatch.setattr(
        AgentsSettings, "from_env", lambda: AgentsSettings(enabled=True)
    )
    monkeypatch.setattr(AgentRegistry, "_ensure_initialized", lambda self: True)
    monkeypatch.setattr(
        AgentRegistry,
        "_load_agents",
        lambda self: [AgentConfig(agent_id="agent", name="Agent", enabled=True)],
    )
    runtime = compose(runtime_extensions=extensions)
    assert constructed >= {
        "GraphStorage",
        "DeliveryWorker",
        "EventDispatcher",
        "AgentRegistry",
        "AgentWorker",
    }
    assert get_runtime_extensions() is default
    assert runtime.runtime_extensions is extensions
    storage = runtime.graph_storage
    assert storage.runtime_extensions is extensions
    storage.add_nodes([Node(id="changed", name="Changed", type="Actor")], [])
    assert any(
        change.resource_id == "changed"
        for call in policy.evaluate.call_args_list
        for change in call.args[1]
    )

    worker = runtime.agent_registry._workers["agent"]
    assert worker._extensions is extensions
    worker.enqueue({"event_id": "event"})
    worker._process_event(worker._queue.get_nowait())
    assert admission.admit.call_args.args[0].operation == "agent_work"
    assert worker.events_failed == 1

    storage.add_nodes(
        [
            Node(
                id="subscription",
                name="External",
                type="EventSubscription",
                metadata={"delivery": {"webhook_url": "https://example.com/hook"}},
            )
        ],
        [],
    )
    dispatcher = storage._event_dispatcher
    dispatcher.invalidate_cache()
    adapter.deliver.reset_mock()
    assert dispatcher.dispatch(create_event()) == 1
    adapter.deliver.assert_called_once()

    broker = FunctionBroker(
        runtime.graph_service,
        BrokerContext(
            run_id="run",
            function_id="function",
            function_version="1",
            declared_grants=frozenset(),
            correlation_id="correlation",
            authorization_inputs={},
            grant_enabled=lambda grant: True,
        ),
    )
    runner = Mock()
    result = runtime.run_function(
        runner, RunSpec("run", "python", "main.py", {}), broker, {}
    )
    assert result.status == "failed" and result.code == "execution_blocked"
    runner.start.assert_not_called()
    assert admission.admit.call_args.args[0].operation == "user_function"
    assert get_runtime_extensions() is default


@pytest.mark.parametrize("explicit", [False, True])
def test_prebuilt_storage_retains_its_extensions(compose, app_config, explicit):
    extensions = RuntimeExtensions()
    storage = GraphStorage(app_config.graph_file, runtime_extensions=extensions)
    kwargs = {"runtime_extensions": extensions} if explicit else {}
    runtime = compose(graph_storage=storage, **kwargs)
    assert runtime.graph_storage is storage
    assert runtime.runtime_extensions is extensions


def test_composition_cannot_replace_extensions_after_startup(compose):
    runtime = compose()
    replacement = RuntimeExtensions()
    with pytest.raises(FrozenInstanceError):
        runtime.runtime_extensions = replacement
    with pytest.raises(ValueError, match="cannot be replaced after construction"):
        compose(graph_storage=runtime.graph_storage, runtime_extensions=replacement)
    assert runtime.graph_storage.runtime_extensions is runtime.runtime_extensions


def test_separate_compositions_keep_their_extensions(compose):
    default = get_runtime_extensions()
    first, second = RuntimeExtensions(), RuntimeExtensions()
    runtime_a = compose(runtime_extensions=first)
    runtime_b = compose(runtime_extensions=second)
    assert runtime_a.graph_storage.runtime_extensions is first
    assert runtime_b.graph_storage.runtime_extensions is second
    assert get_runtime_extensions() is default


def test_workers_created_after_construction_inherit_extensions(compose):
    default = get_runtime_extensions()
    extensions = RuntimeExtensions()
    runtime = compose(runtime_extensions=extensions)
    assert get_runtime_extensions() is default
    registry = runtime.agent_registry
    registry._start_worker(AgentConfig(agent_id="later", name="Later", enabled=True))
    assert registry._workers["later"]._extensions is extensions


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
async def test_http_model_calls_use_selected_extensions(app_config, mode):
    from fastapi.responses import StreamingResponse

    from backend.llm.llm_providers import ClaudeProvider
    from backend.runtime.extensions import AdmissionRefused

    default = get_runtime_extensions()
    admission = Mock()
    admission.admit.return_value = AdmissionDecision("deny", "model_blocked")
    extensions = RuntimeExtensions(execution_admission=admission)
    app = create_app(app_config, runtime_extensions=extensions)
    # Exercise real admission without constructing an SDK or making network calls.
    provider = object.__new__(ClaudeProvider)
    provider.client = Mock()
    provider.model = "test"

    def invoke():
        assert get_runtime_extensions() is extensions
        with pytest.raises(AdmissionRefused, match="model_blocked"):
            provider.create_completion([], "", [])
        return "blocked"

    if mode == "sync":
        app.get("/runtime-probe")(invoke)
    else:

        @app.get("/runtime-probe")
        async def probe():
            if mode == "stream":

                async def body():
                    yield invoke()

                return StreamingResponse(body())
            return invoke()

    try:
        assert get_runtime_extensions() is default
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/runtime-probe")
        assert response.status_code == 200
        assert "blocked" in response.text
        admission.admit.assert_called_once()
        assert admission.admit.call_args.args[0].operation == "model_invocation"
        provider.client.messages.create.assert_not_called()
        assert get_runtime_extensions() is default
    finally:
        app.state.runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["default", "deny", "narrow"])
async def test_agent_skills_obey_injected_graph_authorization(app_config, access):
    from backend.runtime.authorization import GraphAccessNarrowing

    hook = Mock()
    hook.evaluate.return_value = GraphAuthorizationDecision(
        allowed=access != "deny",
        reason="skills_denied" if access == "deny" else "",
        graph_access=GraphAccessNarrowing(
            enabled=access == "narrow",
            allow_local_graph=False,
            include_graph_ids=("visible",),
        ),
    )
    kwargs = {} if access == "default" else {"authorization_hook": hook}
    app = create_app(app_config, **kwargs)
    try:
        app.state.graph_storage.add_nodes(
            [
                Node(
                    id="hidden-skill",
                    name="Hidden",
                    type="Skill",
                    description="Private",
                ),
                Node(
                    id="visible-skill",
                    name="Visible",
                    type="Skill",
                    metadata={"origin_graph_id": "visible"},
                ),
                Node(id="other-type", name="Other", type="Actor"),
            ],
            [],
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/agents/skills",
                headers={
                    "x-communityoverview-actor-id": "reader",
                    "x-communityoverview-graph-id": "visible",
                },
            )
        if access == "deny":
            assert response.status_code == 403
            assert "hidden-skill" not in response.text
            assert "Private" not in response.text
        else:
            assert response.status_code == 200
            expected = [{"id": "visible-skill", "name": "Visible", "description": ""}]
            if access == "default":
                expected.insert(
                    0,
                    {"id": "hidden-skill", "name": "Hidden", "description": "Private"},
                )
            assert sorted(response.json(), key=lambda n: n["id"]) == expected
        if access != "default":
            context = hook.evaluate.call_args.args[0]
            assert context.action == "read"
            assert context.target == "search_graph"
            assert context.actor["actor_id"] == "reader"
            assert context.graph_id == "visible"
    finally:
        app.state.runtime.stop()
