"""Tests for the publishing side's key providers, load rules and fetch log."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from backend.federation import publish
from backend.federation.config import FederationSettings
from backend.federation.publish import (
    OUTCOME_REFUSED_AUTH,
    OUTCOME_REFUSED_DISABLED,
    OUTCOME_REFUSED_EXPIRED,
    OUTCOME_SERVED,
    FederationAuthResult,
    build_key_provider,
    log_fetch,
    presented_key,
)


def _settings(enabled=True, **publish_fields):
    return FederationSettings.model_validate(
        {"enabled": enabled, "publish": {"enabled": True, **publish_fields}}
    )


def _consumer(consumer_id, token_env, **fields):
    return {"consumer_id": consumer_id, "token_env": token_env, **fields}


def test_no_provider_unless_both_switches_are_on(monkeypatch):
    monkeypatch.setenv("SHARED", "s3cret")
    off = _settings(enabled=False, access="shared_token", shared_token_env="SHARED")
    publish_off = FederationSettings.model_validate(
        {"enabled": True, "publish": {"enabled": False, "access": "open"}}
    )

    assert build_key_provider(off) is None
    assert build_key_provider(publish_off) is None


def test_open_mode_serves_without_a_key():
    provider = build_key_provider(_settings(access="open"))

    assert provider.authenticate(None) == FederationAuthResult("open", OUTCOME_SERVED)


def test_shared_token_mode_admits_only_the_token(monkeypatch):
    monkeypatch.setenv("SHARED", "s3cret")
    provider = build_key_provider(
        _settings(access="shared_token", shared_token_env="SHARED")
    )

    assert provider.authenticate("s3cret") == FederationAuthResult(
        "shared", OUTCOME_SERVED
    )
    assert provider.authenticate("wrong").outcome == OUTCOME_REFUSED_AUTH
    assert provider.authenticate(None).outcome == OUTCOME_REFUSED_AUTH


@pytest.mark.parametrize(
    "fields",
    [
        {"access": "shared_token", "shared_token_env": "UNSET_SHARED"},
        {"access": "shared_token", "shared_token_env": "INSTANCE"},
        {"access": "no-such-mode"},
    ],
)
def test_an_unusable_mode_turns_publishing_off_and_never_falls_back_to_open(
    monkeypatch, fields
):
    monkeypatch.delenv("UNSET_SHARED", raising=False)
    monkeypatch.setenv("INSTANCE", "instance-token")

    provider = build_key_provider(
        _settings(**fields), reserved_tokens=["instance-token"]
    )

    assert provider is None


def test_per_consumer_mode_names_the_consumer_and_applies_disabled_then_expiry(
    monkeypatch,
):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    for name in ("K_A", "K_OFF", "K_OLD", "K_BOTH", "K_FUTURE"):
        monkeypatch.setenv(name, name.lower())
    provider = build_key_provider(
        _settings(
            access="per_consumer",
            consumers=[
                _consumer("a", "K_A"),
                _consumer("off", "K_OFF", enabled=False),
                _consumer("old", "K_OLD", expires_at=past.isoformat()),
                _consumer("both", "K_BOTH", enabled=False, expires_at=past.isoformat()),
                _consumer("future", "K_FUTURE", expires_at=future.isoformat()),
            ],
        )
    )

    outcomes = {
        key: provider.authenticate(key)
        for key in ("k_a", "k_off", "k_old", "k_both", "k_future", "nope")
    }

    assert outcomes == {
        "k_a": FederationAuthResult("a", OUTCOME_SERVED),
        "k_off": FederationAuthResult("off", OUTCOME_REFUSED_DISABLED),
        "k_old": FederationAuthResult("old", OUTCOME_REFUSED_EXPIRED),
        "k_both": FederationAuthResult("both", OUTCOME_REFUSED_DISABLED),
        "k_future": FederationAuthResult("future", OUTCOME_SERVED),
        "nope": FederationAuthResult("unauthenticated", OUTCOME_REFUSED_AUTH),
    }


def test_per_consumer_skips_unusable_entries_and_keeps_the_rest(monkeypatch):
    monkeypatch.delenv("K_UNSET", raising=False)
    monkeypatch.setenv("K_OK", "ok")
    monkeypatch.setenv("K_DUP_ID", "dup-id")
    monkeypatch.setenv("K_SAME_1", "same")
    monkeypatch.setenv("K_SAME_2", "same")
    monkeypatch.setenv("K_INSTANCE", "instance-token")
    provider = build_key_provider(
        _settings(
            access="per_consumer",
            consumers=[
                _consumer("ok", "K_OK"),
                _consumer("unset", "K_UNSET"),
                _consumer("dup", "K_DUP_ID"),
                _consumer("dup", "K_OK"),
                _consumer("same-1", "K_SAME_1"),
                _consumer("same-2", "K_SAME_2"),
                _consumer("instance", "K_INSTANCE"),
            ],
        ),
        reserved_tokens=["instance-token"],
    )

    assert provider.authenticate("ok").outcome == OUTCOME_SERVED
    for key in ("dup-id", "same", "instance-token", ""):
        assert provider.authenticate(key).outcome == OUTCOME_REFUSED_AUTH


def test_per_consumer_compares_against_every_loaded_consumer(monkeypatch):
    for index in range(3):
        monkeypatch.setenv(f"K{index}", f"key-{index}")
    provider = build_key_provider(
        _settings(
            access="per_consumer",
            consumers=[_consumer(f"c{i}", f"K{i}", enabled=i != 1) for i in range(3)],
        )
    )
    calls = []
    real = publish._matches
    monkeypatch.setattr(publish, "_matches", lambda a, b: calls.append(b) or real(a, b))

    provider.authenticate("key-0")

    assert calls == [b"key-0", b"key-1", b"key-2"]


def test_external_mode_uses_the_installed_provider_or_refuses_everything():
    class Provider:
        def authenticate(self, presented):
            return FederationAuthResult("partner", OUTCOME_SERVED)

    installed = Provider()
    settings = _settings(access="external")

    assert build_key_provider(settings, external_provider=installed) is installed
    assert (
        build_key_provider(settings).authenticate("anything").outcome
        == OUTCOME_REFUSED_AUTH
    )


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"authorization": "Bearer k"}, ("k", True)),
        ({"x-api-key": "k"}, ("k", True)),
        ({"authorization": "Bearer k", "x-api-key": "k"}, ("k", True)),
        ({"authorization": "Bearer k", "x-api-key": "other"}, (None, False)),
        ({"authorization": "Basic abc"}, (None, True)),
        ({}, (None, True)),
    ],
)
def test_presented_key_reads_bearer_or_api_key_and_rejects_a_mismatch(
    headers, expected
):
    assert presented_key(headers) == expected


def test_fetch_log_records_consumer_graph_time_and_outcome(caplog):
    with caplog.at_level(logging.INFO, logger=publish.__name__):
        log_fetch(
            FederationAuthResult("a", OUTCOME_SERVED),
            graph_id="g",
            request_id="r1",
            nodes=3,
            edges=2,
        )
        log_fetch(FederationAuthResult("b", OUTCOME_REFUSED_EXPIRED), graph_id="g")

    served, refused = (record.federation_fetch for record in caplog.records)
    assert {k: served[k] for k in served if k != "timestamp"} == {
        "event": publish.FETCH_EVENT,
        "consumer_id": "a",
        "graph_id": "g",
        "outcome": OUTCOME_SERVED,
        "request_id": "r1",
        "nodes": 3,
        "edges": 2,
    }
    assert datetime.fromisoformat(served["timestamp"]).tzinfo is not None
    assert (refused["consumer_id"], refused["outcome"]) == (
        "b",
        OUTCOME_REFUSED_EXPIRED,
    )
    assert "nodes" not in refused
