"""Manual REST invocation; all admission belongs to the shared service."""

from typing import Any

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.agents.run_history import function_run_to_dict


class InvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


def register_function_routes(router, service):
    @router.post("/functions/{function_id}/invoke")
    def invoke_function(function_id: str, body: InvocationRequest, request: Request):
        from backend.api_host.middleware import (
            AUTHENTICATED_ACTOR_SCOPE_KEY,
            AUTHENTICATED_PRINCIPAL_SCOPE_KEY,
        )

        binding = dict(request.scope.get(AUTHENTICATED_ACTOR_SCOPE_KEY) or {})
        binding["authenticated_principal"] = request.scope.get(
            AUTHENTICATED_PRINCIPAL_SCOPE_KEY
        )
        try:
            job = service.invoke(
                function_id,
                body.input,
                trigger="manual",
                actor_binding=binding,
                idempotency_key=request.headers.get("Idempotency-Key"),
                correlation_id=body.correlation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except PermissionError:
            raise HTTPException(status_code=403, detail="Access denied")
        return function_run_to_dict(job)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=256)


def register_token_routes(router, service, store):
    import re
    import sqlite3

    from backend.runtime.authorization import (
        GRAPH_ACTION_ADMIN,
        use_request_authorization,
    )
    from backend.service.access import evaluate_graph_access

    def authorize(request, graph_id, target):
        from backend.api_host.middleware import (
            AUTHENTICATED_ACTOR_SCOPE_KEY,
            AUTHENTICATED_PRINCIPAL_SCOPE_KEY,
        )

        binding = dict(request.scope.get(AUTHENTICATED_ACTOR_SCOPE_KEY) or {})
        binding["authenticated_principal"] = request.scope.get(
            AUTHENTICATED_PRINCIPAL_SCOPE_KEY
        )
        binding["graph_id"] = graph_id
        with use_request_authorization(**binding):
            decision = evaluate_graph_access(
                service.graph_service.authorization_hook,
                action=GRAPH_ACTION_ADMIN,
                target=target,
            )
        if not decision.allowed or not decision.graph_access.matches(graph_id=graph_id):
            raise HTTPException(status_code=403, detail="Access denied")

    def existing(function_id):
        row = store.get(function_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Token not found")
        return row

    @router.post("/functions/{function_id}/webhook-token", status_code=201)
    def mint(
        function_id: str, body: TokenRequest, request: Request, response: Response
    ):
        if (
            not re.fullmatch(r"[a-z0-9-]{3,64}", function_id)
            or not body.graph_id.strip()
            or not body.label.strip()
        ):
            raise HTTPException(status_code=422, detail="Invalid token binding")
        authorize(request, body.graph_id, "mint_webhook_token")
        from backend.functions.invocation import load_manifest

        if getattr(service, "webhook_graph_bindings", {}).get(function_id) != body.graph_id:
            raise HTTPException(status_code=404, detail="Function not found")
        try:
            load_manifest(service.root, function_id)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Function not found")
        try:
            token = store.mint(function_id, body.graph_id, body.label)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Token already exists")
        response.headers["Cache-Control"] = "no-store"
        return {"token": token}

    @router.post("/functions/{function_id}/webhook-token/rotate")
    def rotate(function_id: str, request: Request, response: Response):
        row = existing(function_id)
        authorize(request, row["graph_id"], "rotate_webhook_token")
        token = store.rotate(function_id, row["graph_id"], row["id"])
        if token is None:
            raise HTTPException(status_code=404, detail="Token not found")
        response.headers["Cache-Control"] = "no-store"
        return {"token": token}

    @router.delete("/functions/{function_id}/webhook-token", status_code=204)
    def revoke(function_id: str, request: Request):
        row = existing(function_id)
        authorize(request, row["graph_id"], "revoke_webhook_token")
        if not store.revoke(function_id, row["graph_id"], row["id"]):
            raise HTTPException(status_code=404, detail="Token not found")
