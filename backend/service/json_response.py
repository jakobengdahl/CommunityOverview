"""
Response rendering for the graph REST router that stored data cannot break.

Graph content is accepted as long as it parses as JSON, so it can legally
hold values FastAPI's default response path cannot render: metadata nested a
few hundred levels deep (Pydantic's serializer and ``jsonable_encoder`` both
recurse per level and stop at a fixed depth) or a lone UTF-16 surrogate such
as ``"\\ud800"`` in an id or text (Starlette encodes the body as strict
UTF-8). Either way the route raised an unhandled 500, so one such node made
its own reads and every export fail.

``GraphJSONRoute`` therefore hands an endpoint's return value straight to
``GraphJSONResponse``, which renders it with the standard-library encoder,
falling back to an iterative encoder where that one gives up, so nesting
depth no longer matters; what UTF-8 cannot carry is written as a ``\\u``
escape. Ordinary content renders as the same compact UTF-8 JSON as before.

Error responses echo request text too (a duplicate id in a 400's detail, the
rejected ``input`` of a 422), and FastAPI's default handlers render them with
Starlette's strict encoder, turning the intended 4xx into a 500.
``install_error_handlers`` replaces them with ones that differ only in the
response class.
"""

import functools
import inspect
import json
import math
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.datastructures import DefaultPlaceholder
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.utils import is_body_allowed_for_status_code
from starlette.exceptions import HTTPException as StarletteHTTPException

from .serializers import json_key_text


def _iterencode(content: Any, *, ensure_ascii: bool) -> str:
    """Compact JSON for ``content`` without recursing per nesting level.

    Writes what json.dumps writes, except that NaN/Infinity become null, as
    Pydantic's JSON serializer did. Only used when json.dumps gives up: on a
    non-finite float, or on nesting deeper than its C encoder can recurse
    (bounded by the interpreter's recursion limit on Python 3.11).
    """
    encode_text = (
        json.encoder.encode_basestring_ascii
        if ensure_ascii
        else json.encoder.encode_basestring
    )
    parts: list = []
    open_ids: set = set()
    # One [container, iterator, is_dict, first] frame per open container.
    stack: list = []

    def emit(value: Any) -> None:
        if isinstance(value, str):
            parts.append(encode_text(value))
        elif value is None:
            parts.append("null")
        elif value is True:
            parts.append("true")
        elif value is False:
            parts.append("false")
        elif isinstance(value, int):
            parts.append(int.__repr__(value))
        elif isinstance(value, float):
            parts.append(float.__repr__(value) if math.isfinite(value) else "null")
        elif isinstance(value, (list, tuple, dict)):
            if id(value) in open_ids:
                raise ValueError("Circular reference detected")
            open_ids.add(id(value))
            is_dict = isinstance(value, dict)
            parts.append("{" if is_dict else "[")
            items = iter(value.items()) if is_dict else iter(value)
            stack.append([value, items, is_dict, True])
        else:
            raise TypeError(
                f"Object of type {type(value).__name__} is not JSON serializable"
            )

    emit(content)
    while stack:
        frame = stack[-1]
        container, items, is_dict, first = frame
        try:
            item = next(items)
        except StopIteration:
            stack.pop()
            open_ids.discard(id(container))
            parts.append("}" if is_dict else "]")
            continue
        if not first:
            parts.append(",")
        frame[3] = False
        if is_dict:
            key, value = item
            parts.append(encode_text(json_key_text(key)))
            parts.append(":")
            emit(value)
        else:
            emit(item)
    return "".join(parts)


def _dumps(content: Any, *, ensure_ascii: bool) -> str:
    try:
        return json.dumps(
            content, ensure_ascii=ensure_ascii, allow_nan=False, separators=(",", ":")
        )
    except (ValueError, RecursionError):
        # NaN/Infinity (not JSON, though json.loads accepts them on write) or
        # nesting past the C encoder's recursion limit.
        return _iterencode(content, ensure_ascii=ensure_ascii)


class GraphJSONResponse(JSONResponse):
    """JSONResponse that renders any value ``json.loads`` can produce."""

    def render(self, content: Any) -> bytes:
        try:
            text = _dumps(content, ensure_ascii=False)
        except TypeError:
            # Not JSON-native (a model, a datetime): encode it the way
            # FastAPI would have.
            content = jsonable_encoder(content)
            text = _dumps(content, ensure_ascii=False)
        try:
            return text.encode("utf-8")
        except UnicodeEncodeError:
            # A lone surrogate cannot be UTF-8; as a \\uXXXX escape it is still
            # valid JSON and decodes back to the same string.
            return _dumps(content, ensure_ascii=True).encode("ascii")


def _render_return_value(endpoint: Callable[..., Any], status_code: int) -> Any:
    def wrap(result: Any) -> Any:
        if isinstance(result, Response):
            return result
        return GraphJSONResponse(result, status_code=status_code)

    if inspect.iscoroutinefunction(endpoint):

        @functools.wraps(endpoint)
        async def async_endpoint(*args: Any, **kwargs: Any) -> Any:
            return wrap(await endpoint(*args, **kwargs))

        return async_endpoint

    @functools.wraps(endpoint)
    def sync_endpoint(*args: Any, **kwargs: Any) -> Any:
        return wrap(endpoint(*args, **kwargs))

    return sync_endpoint


class GraphJSONRoute(APIRoute):
    """APIRoute whose JSON body is rendered by ``GraphJSONResponse``.

    FastAPI would otherwise serialize the return value through a response
    model inferred from the ``-> Dict[str, Any]`` annotation, which is where
    the depth limit sits. Returning a Response skips that step; the inferred
    model still documents the route in OpenAPI. A route with its own
    ``response_class`` or a generator endpoint is left as it is.
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if isinstance(kwargs.get("response_class"), DefaultPlaceholder) and not (
            inspect.isgeneratorfunction(endpoint)
            or inspect.isasyncgenfunction(endpoint)
        ):
            endpoint = _render_return_value(endpoint, kwargs.get("status_code") or 200)
        super().__init__(path, endpoint, **kwargs)


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    headers = getattr(exc, "headers", None)
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    return GraphJSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=headers
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    return GraphJSONResponse(
        {"detail": jsonable_encoder(exc.errors())}, status_code=422
    )


def install_error_handlers(app: FastAPI) -> None:
    """Render HTTP and request-validation errors with ``GraphJSONResponse``."""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
