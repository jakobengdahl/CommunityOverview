"""Manual REST invocation; all admission belongs to the shared service."""

from typing import Any

from fastapi import HTTPException, Request
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
