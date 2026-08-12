"""
Tests for the secret-provider seam.

Covers the environment-backed default provider and the secret-reference
resolution path (both the standalone helpers and ``MCPIntegration.resolved_env``).
All lookups use an injected environ mapping so the suite never depends on the
real process environment.
"""

import pytest

from backend.agents.config import MCPIntegration, MCPTransport
from backend.agents.secrets import (
    SECRET_REF_PREFIX,
    EnvSecretProvider,
    SecretNotFoundError,
    SecretProvider,
    SecretRef,
    is_secret_ref,
    parse_secret_ref,
    resolve_secret,
    resolve_secret_mapping,
)


# -- EnvSecretProvider (the default) ---------------------------------------


class TestEnvSecretProvider:
    def test_resolves_configured_name(self):
        provider = EnvSecretProvider({"BRAVE_API_KEY": "abc123"})
        assert provider.get_secret("BRAVE_API_KEY") == "abc123"

    def test_missing_name_returns_none(self):
        provider = EnvSecretProvider({})
        assert provider.get_secret("NOPE") is None

    def test_empty_value_is_treated_as_missing(self):
        # A blank env var must not masquerade as a real secret.
        provider = EnvSecretProvider({"BRAVE_API_KEY": ""})
        assert provider.get_secret("BRAVE_API_KEY") is None

    def test_prefix_namespaces_lookups(self):
        provider = EnvSecretProvider({"AGENT_TOKEN": "t"}, prefix="AGENT_")
        assert provider.get_secret("TOKEN") == "t"
        assert provider.get_secret("AGENT_TOKEN") is None

    def test_defaults_to_os_environ(self, monkeypatch):
        monkeypatch.setenv("SECRET_SEAM_TEST_KEY", "from-os")
        assert EnvSecretProvider().get_secret("SECRET_SEAM_TEST_KEY") == "from-os"

    def test_satisfies_protocol(self):
        assert isinstance(EnvSecretProvider({}), SecretProvider)


# -- reference parsing ------------------------------------------------------


class TestReferenceParsing:
    def test_is_secret_ref_true_for_reference(self):
        assert is_secret_ref(f"{SECRET_REF_PREFIX}NAME") is True

    @pytest.mark.parametrize(
        "value",
        [
            "plain-value",
            "",
            SECRET_REF_PREFIX,  # scheme with no name is not a valid reference
            None,
            123,
            {"secret": "x"},
        ],
    )
    def test_is_secret_ref_false_for_non_references(self, value):
        assert is_secret_ref(value) is False

    def test_parse_extracts_name(self):
        assert parse_secret_ref("secret://BRAVE_API_KEY") == SecretRef("BRAVE_API_KEY")

    def test_parse_literal_returns_none(self):
        assert parse_secret_ref("literal") is None

    def test_secret_ref_round_trips_to_uri(self):
        ref = SecretRef("TOKEN")
        assert parse_secret_ref(ref.to_uri()) == ref


# -- single-value resolution ------------------------------------------------


class TestResolveSecret:
    def test_literal_passes_through_untouched(self):
        provider = EnvSecretProvider({})
        assert resolve_secret("just-a-value", provider) == "just-a-value"

    def test_none_passes_through(self):
        assert resolve_secret(None, EnvSecretProvider({})) is None

    def test_reference_is_resolved(self):
        provider = EnvSecretProvider({"BRAVE_API_KEY": "abc123"})
        assert resolve_secret("secret://BRAVE_API_KEY", provider) == "abc123"

    def test_missing_required_reference_raises(self):
        provider = EnvSecretProvider({})
        with pytest.raises(SecretNotFoundError) as exc:
            resolve_secret("secret://MISSING", provider)
        assert exc.value.name == "MISSING"

    def test_missing_optional_reference_resolves_to_none(self):
        provider = EnvSecretProvider({})
        assert resolve_secret("secret://MISSING", provider, required=False) is None


# -- mapping resolution -----------------------------------------------------


class TestResolveSecretMapping:
    def test_mixes_literals_and_references(self):
        provider = EnvSecretProvider({"TOKEN": "resolved"})
        result = resolve_secret_mapping(
            {"PLAIN": "keep-me", "SECRET": "secret://TOKEN"}, provider
        )
        assert result == {"PLAIN": "keep-me", "SECRET": "resolved"}

    def test_empty_mapping_is_empty(self):
        assert resolve_secret_mapping({}, EnvSecretProvider({})) == {}

    def test_missing_required_reference_raises(self):
        with pytest.raises(SecretNotFoundError):
            resolve_secret_mapping({"K": "secret://MISSING"}, EnvSecretProvider({}))


# -- MCPIntegration.resolved_env (the consumption path) ---------------------


class TestIntegrationResolvedEnv:
    def _integration(self, env):
        return MCPIntegration(
            id="SEARCH",
            name="Brave Search",
            transport=MCPTransport.STDIO,
            command=["npx", "-y", "@anthropic/brave-search-mcp"],
            env=env,
        )

    def test_reference_env_resolves_to_value(self):
        integration = self._integration({"BRAVE_API_KEY": "secret://BRAVE_API_KEY"})
        provider = EnvSecretProvider({"BRAVE_API_KEY": "abc123"})
        assert integration.resolved_env(provider) == {"BRAVE_API_KEY": "abc123"}

    def test_literal_env_passes_through(self):
        # File-only/standalone mode: a plain literal env still works with a
        # provider that has no secrets configured.
        integration = self._integration({"PLAIN": "value"})
        assert integration.resolved_env(EnvSecretProvider({})) == {"PLAIN": "value"}

    def test_empty_env_resolves_empty(self):
        integration = self._integration({})
        assert integration.resolved_env(EnvSecretProvider({})) == {}

    def test_missing_required_secret_raises(self):
        integration = self._integration({"BRAVE_API_KEY": "secret://BRAVE_API_KEY"})
        with pytest.raises(SecretNotFoundError):
            integration.resolved_env(EnvSecretProvider({}))

    def test_missing_optional_secret_is_dropped(self):
        integration = self._integration({"BRAVE_API_KEY": "secret://BRAVE_API_KEY"})
        assert integration.resolved_env(EnvSecretProvider({}), required=False) == {}

    def test_default_search_integration_holds_a_reference_not_a_value(self):
        # The built-in SEARCH integration must never inline the raw key.
        import os
        from unittest.mock import patch

        from backend.agents.config import AgentsSettings

        with patch.dict(os.environ, {"BRAVE_API_KEY": "super-secret"}, clear=True):
            settings = AgentsSettings.from_env()

        search = settings.get_integration("SEARCH")
        assert search is not None
        assert search.env["BRAVE_API_KEY"] == "secret://BRAVE_API_KEY"
        assert "super-secret" not in search.env.values()
        # And it round-trips back to the value through the default provider.
        provider = EnvSecretProvider({"BRAVE_API_KEY": "super-secret"})
        assert search.resolved_env(provider) == {"BRAVE_API_KEY": "super-secret"}
