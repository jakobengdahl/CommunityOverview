"""
Secret-provider seam for the agent runtime.

Defines the open-core contract for resolving named secrets that agent and tool
code needs at runtime, plus the reference environment-variable-backed default
adapter. Configuration holds secret *references* (``secret://<name>``) rather
than inline values and resolves them through a :class:`SecretProvider` only at
the point of use.

See ``docs/SECRET_PROVIDER_CONTRACT.md`` and
``docs/adr/0002-env-backed-secret-provider.md``. Hosted/managed secret backends
implement the same ``get_secret`` contract and are injected in place of the
default; the open core never binds to a specific backend.
"""

from .provider import (
    SECRET_REF_PREFIX,
    SECRET_REF_SCHEME,
    EnvSecretProvider,
    SecretNotFoundError,
    SecretProvider,
    SecretRef,
    SecretResolutionError,
    is_secret_ref,
    parse_secret_ref,
    resolve_secret,
    resolve_secret_mapping,
)

__all__ = [
    "SECRET_REF_PREFIX",
    "SECRET_REF_SCHEME",
    "EnvSecretProvider",
    "SecretNotFoundError",
    "SecretProvider",
    "SecretRef",
    "SecretResolutionError",
    "is_secret_ref",
    "parse_secret_ref",
    "resolve_secret",
    "resolve_secret_mapping",
]
