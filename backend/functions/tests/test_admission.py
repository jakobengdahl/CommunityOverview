"""Function admission uses host context and starts no process on refusal."""

import time
from unittest.mock import Mock

import pytest

from backend.functions.broker import BrokerContext
from backend.functions.runner import RunSpec, run_function
from backend.runtime.extensions import AdmissionDecision, RuntimeExtensions


@pytest.mark.parametrize("outcome,status", [("deny", "failed"), ("defer", "deferred")])
def test_function_refusal_precedes_runner_and_uses_fixed_context(outcome, status):
    runner = Mock()
    broker = Mock()
    broker.context = BrokerContext(
        run_id="run",
        function_id="function",
        function_version="1",
        declared_grants=frozenset(),
        correlation_id="corr",
        authorization_inputs={"actor_id": "invoker", "graph_id": "trusted"},
        grant_enabled=lambda grant: True,
    )
    hook = Mock()
    hook.admit.return_value = AdmissionDecision(
        outcome, "function_blocked", time.time() + 1000
    )
    result = run_function(
        runner,
        RunSpec("run", "python", "main.py", {}),
        broker,
        {"run_id": "untrusted", "scope": {"graph_id": "untrusted"}},
        runtime_extensions=RuntimeExtensions(execution_admission=hook),
        attempt=2,
    )
    runner.start.assert_not_called()
    assert result.status == status and result.code == "function_blocked"
    context = hook.admit.call_args.args[0]
    assert (
        context.run_id == "run"
        and context.correlation_id == "corr"
        and context.attempt == 2
    )
    assert (
        context.actor["actor_id"] == "invoker"
        and context.scope["graph_id"] == "trusted"
    )
    if outcome == "defer":
        assert time.time() < result.retry_at <= time.time() + 60
