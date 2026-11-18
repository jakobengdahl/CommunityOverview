"""Webhook configuration, admission and bounded JSON or multipart ingress."""

import json
import os
import time

from anyio import CancelScope
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool

from backend.agents.run_history import function_run_to_dict
from backend.functions.invocation import (
    DEFERRED_INPUT,
    Admission,
    _schema,
    _valid_input,
    load_manifest,
)
from backend.functions.run_files import (
    MAX_RUN_FILE_BYTES,
    MAX_RUN_FILES,
    RunFiles,
    RunFilesTooLarge,
)

MAX_INPUT_BYTES = 65536
MAX_PART_HEADER_BYTES = 4096
# Files, the JSON part and every part's headers and boundaries.
MAX_MULTIPART_BYTES = (
    MAX_RUN_FILE_BYTES
    + MAX_INPUT_BYTES
    + (MAX_RUN_FILES + 1) * 2 * MAX_PART_HEADER_BYTES
)


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
            db.execute(
                "CREATE TABLE IF NOT EXISTS webhook_rate_events (function_id TEXT NOT NULL, admitted REAL NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS webhook_rate_events_function_time ON webhook_rate_events (function_id, admitted)"
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
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute(
                "DELETE FROM webhook_rate_events WHERE function_id = ? AND admitted <= ?",
                (function_id, now - 60),
            )
            count = db.execute(
                "SELECT COUNT(*) FROM webhook_rate_events WHERE function_id = ?",
                (function_id,),
            ).fetchone()[0]
            # Preserve admissions made by the old fixed-window limiter during
            # upgrades until its entire window has expired.
            row = db.execute(
                "SELECT window, count FROM webhook_rate WHERE function_id = ?",
                (function_id,),
            ).fetchone()
            if row and now < row[0] + 60:
                count += row[1]
            if count >= 60:
                return False
            db.execute(
                "INSERT INTO webhook_rate_events VALUES (?, ?)",
                (function_id, now),
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
        binding = {
            "actor_id": f"function_webhook:{function_id}",
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
            return _response(existing_job)
        media, options = parse_options_header(request.headers.get("content-type", ""))
        if media == b"multipart/form-data":
            return await _invoke_with_files(
                service,
                function_id,
                request,
                options.get(b"boundary"),
                binding,
                idempotency_key,
            )
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_INPUT_BYTES:
                raise HTTPException(413)
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
        return _response(job)


def _response(job):
    result = function_run_to_dict(job)
    if (result.get("error") or {}).get("code") == "busy":
        return JSONResponse(result, status_code=409)
    return result


async def _invoke_with_files(
    service, function_id, request, boundary, binding, idempotency_key
):
    """Claim the run before reading the body, so only a claimed run stores files."""
    try:
        manifest, _ = load_manifest(service.root, function_id)
    except (ValueError, OSError):
        raise HTTPException(422)
    if manifest["contract_version"] != "3" or not boundary:
        raise HTTPException(422)
    # A claimed record must be finished even if the client goes away, or it
    # would hold the function busy; shield every step that owns it.
    with CancelScope(shield=True):
        try:
            admitted = await run_in_threadpool(
                service.admit,
                function_id,
                DEFERRED_INPUT,
                trigger="webhook",
                actor_binding=binding,
                idempotency_key=idempotency_key,
            )
        except PermissionError:
            raise HTTPException(403)
    if not isinstance(admitted, Admission):
        return _response(admitted)
    files = None
    try:
        files = RunFiles()
        value = await _read_multipart(request, boundary, files)
        if not _valid_input(admitted.manifest, value):
            raise ValueError("Invalid input")
    except BaseException as exc:
        if files is not None:
            files.close()
        too_large = isinstance(exc, RunFilesTooLarge)
        with CancelScope(shield=True):
            await run_in_threadpool(
                service.reject,
                admitted,
                "input_too_large" if too_large else "invalid_input",
            )
        if too_large:
            raise HTTPException(413)
        if isinstance(exc, ValueError):
            raise HTTPException(422)
        raise
    with CancelScope(shield=True):
        job = await run_in_threadpool(service.execute, admitted, value, files)
    return _response(job)


async def _read_multipart(request, boundary, files):
    """Stream one JSON ``input`` part and file parts; files go to ``files``."""
    state = {"parts": 0, "input": None, "done": False}

    def part_begin():
        state.update(headers={}, field=b"", value=b"", size=0, file=None, json=None)

    def header_bytes(key, data, start, end):
        state["size"] += end - start
        if state["size"] > MAX_PART_HEADER_BYTES:
            raise ValueError()
        state[key] += data[start:end]

    def header_end():
        state["headers"][state["field"].lower()] = state["value"]
        state.update(field=b"", value=b"")

    def headers_finished():
        state["parts"] += 1
        if state["parts"] > MAX_RUN_FILES + 1:
            raise RunFilesTooLarge()
        headers = state["headers"]
        kind, params = parse_options_header(headers.get(b"content-disposition", b""))
        if kind != b"form-data":
            raise ValueError()
        if b"filename" in params:
            state["file"] = files.add(
                params[b"filename"].decode("utf-8", "replace"),
                headers.get(b"content-type", b"").decode("latin-1"),
            )
        elif params.get(b"name") == b"input" and state["input"] is None:
            state["json"] = bytearray()
        else:
            raise ValueError()

    def part_data(data, start, end):
        if state["file"] is not None:
            files.append(state["file"], data[start:end])
        else:
            state["json"].extend(data[start:end])
            if len(state["json"]) > MAX_INPUT_BYTES:
                raise RunFilesTooLarge()

    def part_end():
        if state["json"] is not None:
            state["input"] = json.loads(state["json"])

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": part_begin,
            "on_header_field": lambda *a: header_bytes("field", *a),
            "on_header_value": lambda *a: header_bytes("value", *a),
            "on_header_end": header_end,
            "on_headers_finished": headers_finished,
            "on_part_data": part_data,
            "on_part_end": part_end,
            "on_end": lambda: state.update(done=True),
        },
    )
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_MULTIPART_BYTES:
            raise RunFilesTooLarge()
        parser.write(chunk)
    parser.finalize()
    if not state["done"] or state["input"] is None:
        raise ValueError()
    return state["input"]
