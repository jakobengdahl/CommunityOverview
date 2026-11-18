"""Webhook configuration, admission and bounded JSON ingress."""

import json
import os
import time

from fastapi import HTTPException, Request
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from backend.agents.run_history import function_run_to_dict
from backend.functions.invocation import _schema, load_manifest


class WebhookSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    allow_write: bool


class WebhookPolicy:
    """Persist graph-bound switches separately from the credential rows."""

    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS webhook_policy (function_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, token_id TEXT NOT NULL, enabled INTEGER NOT NULL, allow_write INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS webhook_rate (function_id TEXT PRIMARY KEY, window REAL NOT NULL, count INTEGER NOT NULL)"
            )

    def get(self, function_id, graph_id):
        with self.store._connect() as db:
            row = db.execute(
                "SELECT p.enabled, p.allow_write FROM webhook_policy p JOIN webhook_tokens t ON p.token_id = t.id WHERE p.function_id = ? AND p.graph_id = ?",
                (function_id, graph_id),
            ).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, False)

    def set(self, function_id, graph_id, token_id, settings):
        with self.store._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO webhook_policy VALUES (?, ?, ?, ?, ?)",
                (
                    function_id,
                    graph_id,
                    token_id,
                    settings.enabled,
                    settings.allow_write,
                ),
            )

    def admit(self, function_id):
        now = time.time()
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT window, count FROM webhook_rate WHERE function_id = ?",
                (function_id,),
            ).fetchone()
            if row and now < row[0] + 60 and row[1] >= 60:
                return False
            window, count = (
                (row[0], row[1] + 1)
                if row and row[0] <= now < row[0] + 60
                else (now, 1)
            )
            db.execute(
                "INSERT OR REPLACE INTO webhook_rate VALUES (?, ?, ?)",
                (function_id, window, count),
            )
        return True


def register_webhook_routes(router, service, store, authorize, existing):
    policy = WebhookPolicy(store)
    service.webhook_policy = policy

    @router.put("/functions/{function_id}/webhook-settings")
    def set_settings(function_id: str, settings: WebhookSettings, request: Request):
        row = existing(function_id)
        authorize(request, row["graph_id"], "configure_webhook")
        policy.set(function_id, row["graph_id"], row["id"], settings)
        return settings

    @router.post("/functions/{function_id}/webhook")
    async def webhook(function_id: str, request: Request):
        if os.getenv("FUNCTION_WEBHOOKS_ENABLED", "false").lower() not in {
            "1",
            "true",
            "yes",
        }:
            raise HTTPException(404)
        row = store.get(function_id)
        if (
            not row
            or not policy.get(function_id, row["graph_id"])[0]
            or function_id not in service.enabled
        ):
            raise HTTPException(404)
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            raise HTTPException(401)
        scheme, sep, token = headers[0].partition(" ")
        resolved = (
            store.resolve(token)
            if sep
            and scheme.lower() == "bearer"
            and token
            and token.isascii()
            and not any(c.isspace() for c in token)
            else None
        )
        if not resolved or resolved["id"] != row["id"]:
            raise HTTPException(401)
        if not policy.admit(function_id):
            raise HTTPException(429)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 65536:
                raise HTTPException(413)
        binding = {
            "actor_id": function_id,
            "actor_type": "function_webhook",
            "auth_source": "webhook",
            "graph_id": row["graph_id"],
        }
        idempotency_key = request.headers.get("Idempotency-Key")
        try:
            existing_job = await run_in_threadpool(
                service.replay_webhook, function_id, idempotency_key, binding
            )
        except PermissionError:
            raise HTTPException(403)
        if existing_job is not None:
            return function_run_to_dict(existing_job)
        try:
            value = json.loads(data)
            manifest, _ = load_manifest(service.root, function_id)
            if not isinstance(value, dict):
                raise ValueError()
            if "input_schema" in manifest:
                _schema(manifest["input_schema"]).validate(value)
            elif value:
                raise ValueError()
        except (ValueError, OSError, ValidationError):
            raise HTTPException(422)
        try:
            job = await run_in_threadpool(
                service.invoke,
                function_id,
                value,
                trigger="webhook",
                actor_binding=binding,
                idempotency_key=idempotency_key,
            )
        except PermissionError:
            raise HTTPException(403)
        except ValueError:
            raise HTTPException(422)
        result = function_run_to_dict(job)
        if (result.get("error") or {}).get("code") == "busy":
            from fastapi.responses import JSONResponse

            return JSONResponse(result, status_code=409)
        return result
