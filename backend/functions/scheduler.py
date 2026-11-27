"""Application scheduler for enabled, graph-bound function packages."""

import logging
import threading
from datetime import datetime, timezone

from backend.functions.invocation import load_manifest
from backend.runtime.extensions import bind_runtime_extensions, get_runtime_extensions

logger = logging.getLogger(__name__)


class FunctionScheduler:
    def __init__(self, service, slots, graph_bindings):
        self.service = service
        self.slots = slots
        self.graph_bindings = graph_bindings
        self.extensions = get_runtime_extensions()
        self.stop_event = threading.Event()
        self.thread = None
        self.slots.reconcile(self.service)

    def on_graph_event(self, event):
        from backend.agents.execution import RetryPolicy
        from backend.functions.graph_change import matches

        origin = event.origin.event_origin or ""
        entity = event.to_webhook_payload()["entity"]
        data = entity["data"]
        snapshot = data["after"] or data["before"] or {}
        graph_id = (snapshot.get("metadata") or {}).get("origin_graph_id")
        if not graph_id and event.origin.attribution:
            graph_id = event.origin.attribution.scope.graph_id
        for function_id, bound_graph in self.graph_bindings.items():
            if (
                function_id not in self.service.enabled
                or graph_id != bound_graph
                or origin.startswith(f"function:{function_id}@")
            ):
                continue
            try:
                manifest, _ = load_manifest(self.service.root, function_id)
                if any(
                    t["kind"] == "graph_change" and matches(t, event)
                    for t in manifest["triggers"]
                ):
                    retry = manifest.get("retry")
                    self.slots.enqueue_event(
                        function_id, event, RetryPolicy(**retry) if retry else None
                    )
            except (OSError, ValueError, TypeError):
                logger.warning(
                    "Cannot enqueue graph event for function %s",
                    function_id,
                    exc_info=True,
                )

    def tick(self, now):
        self.slots.reconcile(self.service)
        # Epoch-aligned slots make discovery deterministic across restarts. Only
        # the latest slot is generated, regardless of the length of downtime.
        for function_id, graph_id in self.graph_bindings.items():
            if function_id not in self.service.enabled:
                continue
            try:
                manifest, _ = load_manifest(self.service.root, function_id)
                for trigger in manifest["triggers"]:
                    if trigger["kind"] == "schedule":
                        interval = trigger["interval_seconds"]
                        fire = datetime.fromtimestamp(
                            int(now.timestamp()) // interval * interval, timezone.utc
                        )
                        self.slots.enqueue_manifest(
                            self.service.root, function_id, fire
                        )
            except (OSError, ValueError, TypeError):
                logger.warning(
                    "Cannot schedule function %s", function_id, exc_info=True
                )
        # Bound each pass, including when skipped slots or revoked retries occur.
        for _ in range(max(1, len(self.graph_bindings) * 2)):
            self.slots.dispatch(
                self.service,
                "function-scheduler",
                now=datetime.now(timezone.utc),
                lease_seconds=3600,
                actor_binding=lambda function_id: (
                    {
                        "actor_id": f"function_schedule:{function_id}",
                        "actor_type": "function_schedule",
                        "auth_source": "schedule",
                        "graph_id": self.graph_bindings.get(function_id),
                    }
                    if function_id in self.graph_bindings
                    and function_id in self.service.enabled
                    else None
                ),
            )

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run, name="function-scheduler", daemon=True
        )
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                with bind_runtime_extensions(self.extensions):
                    self.tick(datetime.now(timezone.utc))
            except Exception:
                logger.exception("Function scheduler pass failed")
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        self.slots.close()
