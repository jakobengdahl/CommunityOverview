"""The conftest teardown that stops apps a test built but never started."""

import contextlib
import gc
import threading
import weakref

import pytest
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.tests import conftest
from backend.api_host.tests.conftest import (
    _BUILT_APPS,
    release_fastapi_callable_caches,
    shut_down_unstarted_app,
)


def _delivery_workers():
    return [t for t in threading.enumerate() if t.name == "event-delivery-worker"]


def test_shutting_down_an_unstarted_app_stops_its_delivery_worker(app_config):
    before = len(_delivery_workers())
    app = create_app(app_config)
    TestClient(app).get("/health")
    assert len(_delivery_workers()) == before + 1

    shut_down_unstarted_app(app)

    assert len(_delivery_workers()) == before


def test_a_shut_down_app_is_freed_once_fastapi_caches_are_released(app_config, request):
    app = create_app(app_config)
    TestClient(app).get("/health")
    ref = weakref.ref(app)
    # The recording fixture holds the app until teardown; take it over here.
    request.node.stash[_BUILT_APPS].clear()

    shut_down_unstarted_app(app)
    release_fastapi_callable_caches()
    del app
    gc.collect()

    assert ref() is None


def test_an_app_started_by_its_client_is_left_to_the_client(app_config):
    app = create_app(app_config)
    with TestClient(app):
        workers = len(_delivery_workers())
        shut_down_unstarted_app(app)
        assert len(_delivery_workers()) == workers


def test_the_teardown_hook_stops_every_app_the_test_built(app_config, request):
    before = len(_delivery_workers())
    apps = [create_app(app_config), create_app(app_config)]
    assert len(_delivery_workers()) == before + 2
    del apps

    hook = conftest.pytest_runtest_teardown(request.node, None)
    next(hook)

    assert len(_delivery_workers()) == before
    # The item lives all session; holding the apps there would pin them.
    assert request.node.stash[_BUILT_APPS] == []
    with pytest.raises(StopIteration):
        hook.send(None)


def test_a_failing_shutdown_reaches_fixture_teardown_before_it_is_raised(
    app_config, request
):
    before = len(_delivery_workers())
    failing = create_app(app_config)
    healthy = create_app(app_config)

    real_lifespan = failing.router.lifespan_context

    @contextlib.asynccontextmanager
    async def broken(app):
        async with real_lifespan(app):
            yield
        raise RuntimeError("shutdown broke")

    # After the real shutdown, so only the raise is under test; the healthy app
    # recorded after it must still be shut down.
    failing.router.lifespan_context = broken
    del failing, healthy

    hook = conftest.pytest_runtest_teardown(request.node, None)
    next(hook)  # reaching the yield is reaching fixture teardown

    assert len(_delivery_workers()) == before
    with pytest.raises(RuntimeError, match="shutdown broke"):
        hook.send(None)
