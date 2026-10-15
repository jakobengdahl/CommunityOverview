"""User-function endpoints (docs/USER_FUNCTION_CONTRACT.md §5, §11)."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from backend.functions.runner import RunnerUnavailable
from backend.functions.service import (
    RUN_STATUSES,
    STATUS_RUNNING,
    FunctionNotFound,
    FunctionService,
    InvocationError,
)
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    use_request_authorization,
)
from backend.service import access

logger = logging.getLogger(__name__)


def register_function_routes(
    app: FastAPI, function_service: FunctionService, graph_service
) -> None:
    """Register /functions/* for listing, invoking and inspecting runs."""

    def require(request: Request, action: str, target: str) -> None:
        with use_request_authorization(headers=request.headers):
            try:
                decision = access.evaluate_graph_access(
                    graph_service.authorization_hook, action=action, target=target
                )
            except Exception:  # noqa: BLE001 - a failing hook denies
                logger.exception("Authorization hook failed for %s", target)
                decision = None
        if decision is None or not decision.allowed:
            raise HTTPException(status_code=403, detail="Access denied.")

    @app.get("/functions")
    def functions_list(request: Request) -> List[Dict[str, Any]]:
        """List the configured functions with their validation and enablement."""
        require(request, GRAPH_ACTION_READ, "functions.list")
        return function_service.list_functions()

    @app.get("/functions/runner")
    def functions_runner(request: Request) -> Dict[str, Any]:
        """The runner's isolation level, missing controls and runtimes."""
        require(request, GRAPH_ACTION_READ, "functions.runner")
        try:
            return function_service.runner_status()
        except RunnerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Registered before /functions/{function_id} so "runs" is not read as an id.
    @app.get("/functions/runs")
    def functions_runs(
        request: Request,
        function_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None, description=" | ".join(RUN_STATUSES)),
        limit: int = Query(100, ge=1, le=500),
    ) -> List[Dict[str, Any]]:
        """List function runs, newest-first, inside the run-history read window."""
        require(request, GRAPH_ACTION_READ, "functions.runs")
        return function_service.list_runs(
            function_id=function_id, status=status, limit=limit
        )

    @app.get("/functions/runs/{run_id}")
    def functions_run_detail(request: Request, run_id: str) -> Dict[str, Any]:
        """Get one function run."""
        require(request, GRAPH_ACTION_READ, "functions.runs")
        run = function_service.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return run

    @app.post("/functions/runs/{run_id}/cancel")
    def functions_run_cancel(request: Request, run_id: str) -> Dict[str, Any]:
        """Terminate a run in progress; writes it already made stay (§11)."""
        require(request, GRAPH_ACTION_MUTATE, "functions.cancel")
        if not function_service.cancel_run(run_id):
            raise HTTPException(
                status_code=409, detail=f"Run '{run_id}' is not in progress"
            )
        return {"id": run_id, "cancelling": True}

    @app.get("/functions/{function_id}")
    def functions_detail(request: Request, function_id: str) -> Dict[str, Any]:
        """Get one function's manifest summary and state."""
        require(request, GRAPH_ACTION_READ, "functions.list")
        try:
            return function_service.get_function(function_id)
        except FunctionNotFound as exc:
            raise HTTPException(
                status_code=404, detail=f"Function '{function_id}' not found"
            ) from exc

    @app.post("/functions/{function_id}/invoke")
    def functions_invoke(
        request: Request,
        function_id: str,
        body: Dict[str, Any] = Body(default_factory=dict),
    ):
        """
        Invoke a function manually (§5.1) as the calling actor.

        Returns the run record: ``running`` (202) for a run that started, or the
        earlier run for a repeated ``idempotency_key``, or a ``rejected`` record
        naming why the run could not start (§11). A caller without read access
        is refused before any record is created.
        """
        require(request, GRAPH_ACTION_READ, "functions.invoke")
        with use_request_authorization(headers=request.headers):
            try:
                run = function_service.invoke(
                    function_id,
                    body.get("input", {}),
                    idempotency_key=body.get("idempotency_key"),
                    correlation_id=body.get("correlation_id")
                    or request.headers.get("x-correlation-id"),
                    event_origin=request.headers.get("x-event-origin") or "api",
                )
            except FunctionNotFound as exc:
                raise HTTPException(
                    status_code=404, detail=f"Function '{function_id}' not found"
                ) from exc
            except InvocationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RunnerUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(
            run, status_code=202 if run["status"] == STATUS_RUNNING else 200
        )
