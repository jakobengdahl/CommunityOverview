"""An external adapter owns delivery exclusively; internal routes stay internal."""

from unittest.mock import Mock

import pytest

from backend.core.events.dispatcher import EventDispatcher
from backend.core.events.delivery import DeliveryItem, DeliveryWorker
from backend.core.events.models import SubscriptionInfo
from backend.core.events.tests.test_dispatcher import (
    MockStorage,
    create_event,
    create_subscription_node,
)
from backend.runtime.extensions import DeliveryOutcome


@pytest.mark.parametrize(
    "outcome,count", [("delivered", 1), ("deferred", 1), ("terminal_failure", 0)]
)
def test_adapter_substitution_reports_identity_without_direct_send(outcome, count):
    storage = MockStorage([create_subscription_node("sub", "External")])
    direct, adapter, reported = Mock(), Mock(), Mock()
    event = create_event()
    event.origin.event_correlation_id = "corr"

    def deliver(subscription, received, context):
        assert subscription["id"] == "sub" and received.event_id == event.event_id
        assert context.delivery_id == f"sub:{event.event_id}"
        assert context.correlation_id == "corr"
        return DeliveryOutcome(
            outcome, context.delivery_id, context.correlation_id, "result"
        )

    adapter.deliver.side_effect = deliver
    dispatcher = EventDispatcher(
        storage,
        on_deliver=direct,
        delivery_adapter=adapter,
        on_delivery_outcome=reported,
    )
    assert dispatcher.dispatch(event) == count
    adapter.deliver.assert_called_once()
    direct.assert_not_called()
    assert reported.call_args.args[0].outcome == outcome


def test_adapter_works_without_http_callback_and_failure_never_falls_back():
    storage = MockStorage([create_subscription_node("sub", "External")])
    adapter, reported = Mock(), Mock()
    adapter.deliver.side_effect = RuntimeError("unavailable")
    dispatcher = EventDispatcher(
        storage, delivery_adapter=adapter, on_delivery_outcome=reported
    )
    assert dispatcher.dispatch(create_event()) == 0
    assert reported.call_args.args[0].code == "delivery_adapter_error"


@pytest.mark.parametrize("handled", [False, True])
def test_internal_agent_routing_never_calls_external_adapter(handled):
    storage = MockStorage(
        [create_subscription_node("sub", "Agent", webhook_url="internal://agent/agent")]
    )
    direct, adapter = Mock(), Mock()
    agent = Mock(return_value=handled)
    dispatcher = EventDispatcher(
        storage, on_deliver=direct, on_agent_deliver=agent, delivery_adapter=adapter
    )
    assert dispatcher.dispatch(create_event()) == int(handled)
    agent.assert_called_once()
    direct.assert_not_called()
    adapter.deliver.assert_not_called()


def test_default_transport_still_queues_once_through_existing_callback():
    storage = MockStorage([create_subscription_node("sub", "External")])
    direct, reported = Mock(), Mock()
    dispatcher = EventDispatcher(
        storage, on_deliver=direct, on_delivery_outcome=reported
    )
    assert dispatcher.dispatch(create_event()) == 1
    direct.assert_called_once()
    assert direct.call_args.args[1] == "https://example.com/hook"
    assert reported.call_args.args[0].outcome == "deferred"
    assert reported.call_args.args[0].code == "http_queued"


@pytest.mark.parametrize(
    "status_code,attempt,status",
    [(200, 1, "success"), (500, 1, "retrying"), (500, 3, "dropped")],
)
def test_http_results_keep_delivery_and_correlation_ids(
    monkeypatch, status_code, attempt, status
):
    event = create_event()
    event.subscription = SubscriptionInfo(id="sub", name="External")
    event.origin.event_correlation_id = "corr"
    reported = Mock()
    worker = DeliveryWorker(on_result=reported, backoff_times=[0])
    monkeypatch.setattr("backend.core.events.delivery.is_safe_url", lambda url: True)
    monkeypatch.setattr(
        worker,
        "_post_with_redirect_ssrf_check",
        Mock(return_value=Mock(status_code=status_code, text="result")),
    )
    worker._deliver(DeliveryItem(event, "https://example.com/hook", attempt=attempt))
    result = reported.call_args.args[0]
    assert result.status.value == status
    assert (
        result.delivery_id == f"sub:{event.event_id}"
        and result.correlation_id == "corr"
    )
