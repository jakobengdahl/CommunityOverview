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
``GraphJSONResponse``, which renders it with the standard-library encoder: no
practical depth limit, and what UTF-8 cannot carry is written as a ``\\u``
escape. Normal content renders byte-for-byte as before.
"""

import functools
import inspect
import json
import math
from typing import Any, Callable

from fastapi.datastructures import DefaultPlaceholder
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute


def _null_non_finite(value: Any) -> Any:
    """Replace NaN/Infinity with None, as Pydantic's JSON serializer does."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _null_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_null_non_finite(item) for item in value]
    return value


def _dumps(content: Any, *, ensure_ascii: bool) -> str:
    kwargs = {
        "ensure_ascii": ensure_ascii,
        "allow_nan": False,
        "separators": (",", ":"),
    }
    try:
        return json.dumps(content, **kwargs)
    except ValueError:
        # NaN/Infinity are not JSON, but json.loads accepts them on write.
        return json.dumps(_null_non_finite(content), **kwargs)


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
