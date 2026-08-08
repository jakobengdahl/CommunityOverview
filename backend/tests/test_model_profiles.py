import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from backend.config.model_profiles import (
    MissingCredentialError,
    ModelProfile,
    ModelProfilesConfig,
    create_provider_from_profile,
    resolve_profile_reference,
)


def test_model_profiles_require_single_enabled_default():
    with pytest.raises(ValidationError, match="Multiple model profiles marked as default"):
        ModelProfilesConfig(
            profiles=[
                ModelProfile(id="fast", name="Fast", provider="openai", model="gpt-4o", default=True),
                ModelProfile(id="deep", name="Deep", provider="claude", model="claude-sonnet-4-5", default=True),
            ]
        )


def test_model_profile_rejects_inline_secret_like_credential_ref():
    with pytest.raises(ValidationError, match="environment variable"):
        ModelProfile(
            id="bad",
            name="Bad",
            provider="openai",
            model="gpt-4o",
            credential_ref="sk-live-secret-value",
        )


def test_resolve_profile_reference_inherits_default_and_rejects_disabled():
    profiles = [
        ModelProfile(id="fast", name="Fast", provider="openai", model="gpt-4o", default=True),
        ModelProfile(id="off", name="Off", provider="openai", model="gpt-4o-mini", enabled=False),
    ]

    inherited = resolve_profile_reference(profiles, None)
    assert inherited.error is None
    assert inherited.profile.id == "fast"

    disabled = resolve_profile_reference(profiles, "off")
    assert disabled.profile is None
    assert "disabled" in disabled.error


def test_create_provider_from_profile_uses_credential_ref_without_exposing_secret():
    profile = ModelProfile(
        id="local-openai-compatible",
        name="Local",
        provider="openai",
        model="llama-test",
        endpoint="http://localhost:8080/v1",
        credential_ref="LOCAL_LLM_API_KEY",
    )

    with patch.dict(os.environ, {"LOCAL_LLM_API_KEY": "test-secret"}, clear=False), patch(
        "backend.llm.llm_providers.OpenAIProvider"
    ) as provider_cls:
        create_provider_from_profile(profile)

    provider_cls.assert_called_once_with(
        api_key="test-secret", model="llama-test", base_url="http://localhost:8080/v1"
    )


def test_create_provider_from_profile_requires_configured_credential():
    profile = ModelProfile(
        id="missing",
        name="Missing",
        provider="claude",
        model="claude-sonnet-4-5",
        credential_ref="MISSING_PROFILE_KEY",
    )

    with patch.dict(os.environ, {}, clear=True), pytest.raises(MissingCredentialError, match="MISSING_PROFILE_KEY"):
        create_provider_from_profile(profile)
