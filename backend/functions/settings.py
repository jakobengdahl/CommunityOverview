"""
Instance settings for user functions (docs/USER_FUNCTION_CONTRACT.md §3.1,
§4.4, §8), read from the environment.

Enabling a function, and each grant it declares, is an operator action:
``FUNCTIONS_ENABLED`` maps a function id to the grants enabled for it. The
instance limits may only lower the contract maximums; a value above one is
clamped to it and logged.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet, Mapping, Optional

from backend.functions.broker import MAX_TOOL_CALLS
from backend.functions.runner import (
    LEVEL_DEGRADED,
    LEVEL_FULL,
    MAX_MEMORY_MIB,
    MAX_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "config" / "functions"
ISOLATION_LEVELS = (LEVEL_DEGRADED, LEVEL_FULL)


class FunctionsSettingsError(ValueError):
    """An instance setting for user functions is malformed."""


@dataclass(frozen=True)
class FunctionsSettings:
    root: str = str(DEFAULT_ROOT)
    enabled: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    isolation_minimum: str = LEVEL_DEGRADED
    timeout_seconds: int = MAX_TIMEOUT_SECONDS
    memory_mib: int = MAX_MEMORY_MIB
    max_tool_calls: int = MAX_TOOL_CALLS

    def is_enabled(self, function_id: str) -> bool:
        return function_id in self.enabled

    def grant_enabled(self, function_id: str, grant: str) -> bool:
        return grant in self.enabled.get(function_id, frozenset())

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "FunctionsSettings":
        env = os.environ if environ is None else environ
        return cls(
            root=env.get("FUNCTIONS_ROOT") or str(DEFAULT_ROOT),
            enabled=_parse_enabled(env.get("FUNCTIONS_ENABLED", "")),
            isolation_minimum=_parse_isolation(env.get("FUNCTIONS_ISOLATION_MINIMUM")),
            timeout_seconds=_lowered(
                env, "FUNCTIONS_TIMEOUT_SECONDS", MAX_TIMEOUT_SECONDS
            ),
            memory_mib=_lowered(env, "FUNCTIONS_MEMORY_MIB", MAX_MEMORY_MIB),
            max_tool_calls=_lowered(env, "FUNCTIONS_MAX_TOOL_CALLS", MAX_TOOL_CALLS),
        )


def _parse_enabled(raw: str) -> Mapping[str, FrozenSet[str]]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise FunctionsSettingsError(f"FUNCTIONS_ENABLED is not JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(grants, list) and all(isinstance(g, str) for g in grants)
        for grants in value.values()
    ):
        raise FunctionsSettingsError(
            "FUNCTIONS_ENABLED must map function ids to grant lists, e.g. "
            '{"my-function": ["graph:read"]}'
        )
    return {str(key): frozenset(grants) for key, grants in value.items()}


def _parse_isolation(raw: Optional[str]) -> str:
    value = (raw or LEVEL_DEGRADED).strip().lower()
    if value not in ISOLATION_LEVELS:
        raise FunctionsSettingsError(
            "FUNCTIONS_ISOLATION_MINIMUM must be 'degraded' or 'full'"
        )
    return value


def _lowered(env: Mapping[str, str], name: str, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return maximum
    try:
        value = int(raw)
    except ValueError as exc:
        raise FunctionsSettingsError(f"{name} must be an integer") from exc
    if value <= 0:
        raise FunctionsSettingsError(f"{name} must be above 0")
    if value > maximum:
        logger.warning(
            "%s=%s is above the contract maximum; using %s", name, value, maximum
        )
        return maximum
    return value
