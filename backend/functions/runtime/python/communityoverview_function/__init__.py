"""
Helper library for Python user functions (docs/USER_FUNCTION_CONTRACT.md §10).

It hides the broker protocol (§6): a function only writes a handler and calls
``run``::

    from communityoverview_function import run

    def handle(envelope, tools):
        node = tools.graph.get_node(node_id=envelope["invocation"]["input"]["node_id"])
        return {"name": node["name"]}

    run(handle)

The function runs with only its own files and this module on its path, so this
module uses the standard library only. ``print`` and anything else written to
standard output go to the run's log, never into the protocol.
"""

import json
import os
import sys
import traceback

__all__ = ["ToolError", "run"]


class ToolError(Exception):
    """A tool call the broker refused or that failed (§6.4)."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _Channel:
    def __init__(self):
        self._input = os.fdopen(os.dup(0), "rb", buffering=0)
        self._output = os.fdopen(os.dup(1), "wb")
        # Stray writes to standard output would corrupt the protocol: point the
        # descriptor and the Python object at standard error instead.
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self._next_id = 0

    def receive(self):
        line = b""
        while not line.endswith(b"\n"):
            chunk = self._input.readline()
            if not chunk:
                raise EOFError("the host closed the protocol stream")
            line += chunk
        return json.loads(line)

    def send(self, message):
        self._output.write(
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        )
        self._output.flush()

    def call(self, name, arguments):
        self._next_id += 1
        request_id = self._next_id
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tool",
                "params": {"name": name, "arguments": arguments},
            }
        )
        response = self.receive()
        if "error" in response:
            error = response["error"] or {}
            data = error.get("data") or {}
            raise ToolError(data.get("code", "tool_error"), error.get("message", ""))
        return response.get("result")


class _Graph:
    def __init__(self, channel):
        self._channel = channel

    def search(self, **arguments):
        return self._channel.call("graph.search", arguments)

    def get_node(self, **arguments):
        return self._channel.call("graph.get_node", arguments)

    def get_related(self, **arguments):
        return self._channel.call("graph.get_related", arguments)

    def find_similar(self, **arguments):
        return self._channel.call("graph.find_similar", arguments)

    def add(self, **arguments):
        return self._channel.call("graph.add", arguments)

    def update_node(self, **arguments):
        return self._channel.call("graph.update_node", arguments)


class Tools:
    """The tools a handler receives; ``tools.graph.<tool>(**arguments)``."""

    def __init__(self, channel):
        self.graph = _Graph(channel)


def _error(code, message):
    return {"status": "error", "error": {"code": code, "message": message[:1000]}}


def run(handle):
    """Receive the invocation, call ``handle(envelope, tools)``, send its result."""
    channel = _Channel()
    message = channel.receive()
    if message.get("method") != "invoke":
        raise SystemExit("expected the invoke notification")
    envelope = message.get("params")
    try:
        output = handle(envelope, Tools(channel))
        if output is None:
            output = {}
        if not isinstance(output, dict):
            params = _error("invalid_output", "handle() must return a dict.")
        else:
            json.dumps(output)
            params = {"status": "ok", "output": output}
    except MemoryError:
        output = None
        params = _error("memory_exceeded", "The memory limit was reached.")
    except ToolError as exc:
        params = _error(exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - reported to the host, not raised
        traceback.print_exc()
        params = _error("unhandled_exception", f"{type(exc).__name__}: {exc}")
    channel.send({"jsonrpc": "2.0", "method": "result", "params": params})
