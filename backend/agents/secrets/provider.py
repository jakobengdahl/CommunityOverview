"""
The secret-provider seam.

``SecretProvider`` is the replaceable contract for resolving named secrets that
agent and tool code needs at runtime (API keys, tokens, subprocess credentials).
Configuration and tool wiring hold **secret references** — opaque names like
``secret://BRAVE_API_KEY`` — rather than inline secret values, and resolve them
to concrete values through a provider only at the point of use.

The reference ``EnvSecretProvider`` reads those names from the process
environment; it is the default that keeps file-only/standalone mode working with
no external dependency. Hosted layers implement the same ``get_secret`` method
against a managed secret store (e.g. a cloud Secret Manager) and inject it in
place of the default — the open core never binds to any specific backend.

This seam is a generic reliability/safety boundary only. Tenant policy, access
control and the choice of a managed secret backend live in the SaaS/infra layer
that consumes this seam; they are deliberately out of scope here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Protocol, runtime_checkable

#: URL-style scheme that marks a string as a secret reference rather than a
#: literal value: ``secret://<name>``.
SECRET_REF_SCHEME = "secret"
SECRET_REF_PREFIX = f"{SECRET_REF_SCHEME}://"


class SecretResolutionError(Exception):
    """Base class for secret-resolution errors."""


class SecretNotFoundError(SecretResolutionError):
    """Raised when a required secret reference cannot be resolved to a value."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"secret '{name}' is not available from the provider")


@dataclass(frozen=True)
class SecretRef:
    """A parsed reference to a named secret (the ``<name>`` in ``secret://<name>``)."""

    name: str

    def to_uri(self) -> str:
        return f"{SECRET_REF_PREFIX}{self.name}"


@runtime_checkable
class SecretProvider(Protocol):
    """
    Resolver for named secrets.

    Implementations MUST:

    * return the secret value for a known name, or ``None`` when the name is not
      configured — a missing secret is not an error at this layer; callers decide
      whether a given reference is required (see :func:`resolve_secret`);
    * treat lookups as read-only and side-effect free;
    * never log or echo resolved values.
    """

    def get_secret(self, name: str) -> Optional[str]:
        """Return the secret value for ``name``, or ``None`` if not configured."""
        ...


class EnvSecretProvider:
    """
    Default provider that resolves secrets from environment variables.

    Keeps standalone/file-only mode working with no external dependency: a
    reference ``secret://BRAVE_API_KEY`` resolves to ``os.environ["BRAVE_API_KEY"]``.

    Args:
        environ: Mapping to read from. Defaults to ``os.environ``. Injecting an
            explicit mapping keeps resolution deterministic and side-effect free
            in tests.
        prefix: Optional prefix prepended to every looked-up name, so a
            deployment can namespace its agent secrets (e.g. ``AGENT_``) without
            changing the references in config.
    """

    def __init__(
        self,
        environ: Optional[Mapping[str, str]] = None,
        *,
        prefix: str = "",
    ):
        self._environ = environ if environ is not None else os.environ
        self._prefix = prefix

    def get_secret(self, name: str) -> Optional[str]:
        value = self._environ.get(f"{self._prefix}{name}")
        # Treat an empty string as "not configured" so a blank env var does not
        # masquerade as a real secret.
        return value or None


def is_secret_ref(value: object) -> bool:
    """Return True if ``value`` is a ``secret://<name>`` reference string."""
    return (
        isinstance(value, str)
        and value.startswith(SECRET_REF_PREFIX)
        and len(value) > len(SECRET_REF_PREFIX)
    )


def parse_secret_ref(value: object) -> Optional[SecretRef]:
    """
    Parse ``value`` into a :class:`SecretRef`, or return ``None`` if it is not a
    secret reference (a plain literal value).
    """
    if not is_secret_ref(value):
        return None
    return SecretRef(name=value[len(SECRET_REF_PREFIX) :])  # type: ignore[index]


def resolve_secret(
    value: Optional[str],
    provider: SecretProvider,
    *,
    required: bool = True,
) -> Optional[str]:
    """
    Resolve a single config value against ``provider``.

    A literal (non-reference) value — including ``None`` — is returned unchanged,
    so mixing plain values and secret references in the same config is safe. A
    ``secret://<name>`` reference is looked up through the provider.

    Args:
        required: When True (default), an unresolved reference raises
            :class:`SecretNotFoundError`. When False, it resolves to ``None`` so
            optional secrets can be absent in standalone mode.
    """
    ref = parse_secret_ref(value)
    if ref is None:
        return value
    resolved = provider.get_secret(ref.name)
    if resolved is None and required:
        raise SecretNotFoundError(ref.name)
    return resolved


def resolve_secret_mapping(
    mapping: Mapping[str, Optional[str]],
    provider: SecretProvider,
    *,
    required: bool = True,
) -> Dict[str, Optional[str]]:
    """
    Resolve every value in ``mapping`` against ``provider``.

    Keys are preserved; literal values pass through untouched and secret
    references are resolved. This is the reference-resolution path config such as
    an MCP integration's subprocess ``env`` uses to turn references into concrete
    values just before they are handed to a tool.
    """
    return {
        key: resolve_secret(val, provider, required=required)
        for key, val in mapping.items()
    }
