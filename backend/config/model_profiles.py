"""
Model profile configuration and resolution.

Defines the app-config contract for multiple named LLM/model profiles across
providers, selectable by AI agents (backend/agents) and the user-facing chat
interface (backend/ui). A "model profile" bundles a provider, a model, and
provider-specific endpoint/options behind a stable id — never a secret value,
only a reference to where the credential lives (an environment variable name).

Backward compatibility: when no profiles are configured (the default), agents
and chat fall back unchanged to the legacy single-provider environment
configuration (LLM_PROVIDER / LLM_MODEL / OPENAI_API_KEY / ANTHROPIC_API_KEY).
See docs/PROFILES.md for the migration and precedence rules.
"""

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Environment variable names are UPPER_SNAKE_CASE by convention. credential_ref
# must look like one — this is also the guardrail against inline secret values
# (API keys, tokens) being pasted into the field instead of a reference to one.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Option keys that commonly carry secrets — flagged if given a literal value
# instead of being left out (the credential should go through credential_ref).
_SECRET_LOOKING_OPTION_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|credential)", re.IGNORECASE
)


class ModelProfile(BaseModel):
    """A single named LLM/model profile."""

    id: str
    name: str
    provider: str
    model: str
    enabled: bool = True
    default: bool = False
    endpoint: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)
    # Name of the environment variable holding the credential — never the
    # credential value itself.
    credential_ref: Optional[str] = None

    @field_validator("id", "name", "provider", "model")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("credential_ref")
    @classmethod
    def _credential_ref_must_be_a_reference(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v or None
        if not _ENV_VAR_NAME_RE.match(v):
            raise ValueError(
                f"credential_ref {v!r} does not look like an environment variable "
                "name — it must reference a secret (e.g. 'ANTHROPIC_API_KEY'), "
                "never embed one."
            )
        return v

    @field_validator("options")
    @classmethod
    def _options_must_not_embed_secrets(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in v.items():
            if isinstance(value, str) and value and _SECRET_LOOKING_OPTION_KEY_RE.search(key):
                raise ValueError(
                    f"options.{key} looks like an inline secret value — use "
                    "credential_ref to point at an environment variable instead "
                    "of embedding credentials in options."
                )
        return v


def validate_profiles(profiles: List[ModelProfile]) -> List[str]:
    """
    Cross-profile validation that cannot be expressed on a single ModelProfile.

    Returns a list of human-readable error messages (empty when valid). An
    empty profile list is valid — it means "no profiles configured", which is
    the legacy single-provider mode.
    """
    errors: List[str] = []
    if not profiles:
        return errors

    ids = [p.id for p in profiles]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dup_ids:
        errors.append(f"Duplicate model profile id(s): {dup_ids}")

    names = [p.name for p in profiles]
    dup_names = sorted({n for n in names if names.count(n) > 1})
    if dup_names:
        errors.append(f"Duplicate model profile name(s): {dup_names}")

    defaults = [p for p in profiles if p.default]
    if len(defaults) == 0:
        errors.append(
            "No model profile marked as default — exactly one enabled profile "
            "must be the application default."
        )
    elif len(defaults) > 1:
        errors.append(
            "Multiple model profiles marked as default: "
            f"{sorted(p.id for p in defaults)} — exactly one is allowed."
        )
    else:
        default_profile = defaults[0]
        if not default_profile.enabled:
            errors.append(
                f"Default model profile '{default_profile.id}' is disabled — "
                "the default profile must be enabled."
            )

    return errors


class ModelProfilesConfig(BaseModel):
    """Root config section for model profiles (schema_config.json: 'model_profiles')."""

    # Whether the user-facing chat interface may switch to a profile other
    # than the default. When False, only the default profile is used and
    # any explicit selection in a chat request is ignored.
    selection_enabled: bool = True
    profiles: List[ModelProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_profile_set(self) -> "ModelProfilesConfig":
        errors = validate_profiles(self.profiles)
        if errors:
            raise ValueError("; ".join(errors))
        return self


@dataclass
class ProfileResolution:
    """Outcome of resolving a (possibly absent) profile id against configured profiles."""

    profile: Optional[ModelProfile]
    error: Optional[str] = None


def get_enabled_profiles(profiles: List[ModelProfile]) -> List[ModelProfile]:
    """Return only the enabled profiles, preserving configured order."""
    return [p for p in profiles if p.enabled]


def get_default_profile(profiles: List[ModelProfile]) -> Optional[ModelProfile]:
    """Return the single enabled+default profile, or None if none is configured."""
    for p in profiles:
        if p.default and p.enabled:
            return p
    return None


def find_profile(profiles: List[ModelProfile], profile_id: str) -> Optional[ModelProfile]:
    """Look up a profile by id regardless of its enabled state."""
    for p in profiles:
        if p.id == profile_id:
            return p
    return None


def resolve_profile_reference(
    profiles: List[ModelProfile], profile_id: Optional[str]
) -> ProfileResolution:
    """
    Resolve an optional explicit profile id against the configured profiles.

    - profile_id is None: inherit the default profile (agents/chat requests
      without an explicit selection inherit the default, per contract).
    - profile_id set: must reference a known, enabled profile.

    When `profiles` is empty, this always returns profile=None with no error —
    callers should treat that as "no profiles configured, use the legacy path".
    """
    if not profiles:
        return ProfileResolution(profile=None, error=None)

    if profile_id is None:
        default = get_default_profile(profiles)
        if default is None:
            return ProfileResolution(
                profile=None, error="no default model profile is configured"
            )
        return ProfileResolution(profile=default, error=None)

    profile = find_profile(profiles, profile_id)
    if profile is None:
        return ProfileResolution(
            profile=None, error=f"model profile '{profile_id}' is not configured"
        )
    if not profile.enabled:
        return ProfileResolution(
            profile=None, error=f"model profile '{profile_id}' is disabled"
        )
    return ProfileResolution(profile=profile, error=None)


def validate_agent_profile_references(
    agent_profile_ids: Dict[str, Optional[str]], profiles: List[ModelProfile]
) -> List[str]:
    """
    Validate a set of agent -> model_profile_id references against configured profiles.

    Args:
        agent_profile_ids: mapping of agent_id -> model_profile_id (None means
            "inherits the default", which is always valid and skipped here).
        profiles: the configured model profiles.

    Returns a list of human-readable error messages (empty when all references
    are valid or absent).
    """
    errors: List[str] = []
    for agent_id, profile_id in agent_profile_ids.items():
        if profile_id is None:
            continue
        resolution = resolve_profile_reference(profiles, profile_id)
        if resolution.profile is None and resolution.error:
            errors.append(f"Agent '{agent_id}': {resolution.error}")
    return errors


class MissingCredentialError(RuntimeError):
    """Raised when a model profile's credential cannot be resolved at runtime."""


def resolve_credential(profile: ModelProfile) -> Optional[str]:
    """Read the credential value from the environment variable named by credential_ref."""
    if not profile.credential_ref:
        return None
    return os.environ.get(profile.credential_ref)


def create_provider_from_profile(
    profile: ModelProfile, api_key_override: Optional[str] = None
):
    """
    Create an LLMProvider from a resolved model profile.

    Args:
        profile: The resolved ModelProfile to instantiate.
        api_key_override: Optional caller-supplied API key that takes
            precedence over the profile's credential_ref (e.g. a per-request
            BYO key from the chat UI).

    Raises:
        MissingCredentialError: if no credential is available.
        ValueError: if the profile's provider is not supported.
    """
    from backend.llm.llm_providers import ClaudeProvider, OpenAIProvider

    api_key = api_key_override or resolve_credential(profile)
    if not api_key:
        ref = profile.credential_ref or "(no credential_ref configured)"
        raise MissingCredentialError(
            f"Model profile '{profile.id}' requires a credential via environment "
            f"variable '{ref}', but it is not set."
        )

    provider_id = profile.provider.lower()
    if provider_id in ("claude", "anthropic"):
        return ClaudeProvider(api_key=api_key, model=profile.model, base_url=profile.endpoint)
    if provider_id == "openai":
        return OpenAIProvider(api_key=api_key, model=profile.model, base_url=profile.endpoint)
    raise ValueError(
        f"Model profile '{profile.id}' uses unsupported provider '{profile.provider}'. "
        "Supported providers: 'claude', 'openai' (OpenAI-compatible endpoints, e.g. "
        "for locally hosted models, use provider 'openai' with a custom 'endpoint')."
    )
