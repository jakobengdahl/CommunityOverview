"""Shared exceptions without dependencies on core package initialization."""


class GraphWriteFailed(RuntimeError):
    """A shared-store write that did not land (§4.4: `write_failed`).

    The instance has confirmed, on two reads of the store, that nothing the
    write touched changed. Memory is back where it was and no event fired.
    """

    code = "write_failed"
