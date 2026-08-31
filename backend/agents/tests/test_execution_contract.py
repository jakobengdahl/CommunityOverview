"""Bind the execution-store contract tests to the reference in-memory adapter."""

from backend.agents.execution.contract import ExecutionStoreContractTests
from backend.agents.execution.memory_store import InMemoryExecutionStore
from backend.agents.execution.models import RetryPolicy
from backend.agents.execution.store import ExecutionStore


class TestInMemoryExecutionStore(ExecutionStoreContractTests):
    def make_store(self, retry_policy: RetryPolicy) -> ExecutionStore:
        return InMemoryExecutionStore(retry_policy=retry_policy)


def test_in_memory_store_satisfies_protocol():
    # runtime_checkable Protocol: structural conformance of the reference adapter.
    assert isinstance(InMemoryExecutionStore(), ExecutionStore)
