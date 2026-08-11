"""Unit tests for the signed session-cookie helpers."""

from types import SimpleNamespace

from backend.api_host.session_auth import (
    _value_is_valid,
    credentials_valid,
    is_safe_next_path,
    mint_session_cookie,
    request_has_valid_session,
)


def _cfg(password="secret", bearer=None, username="admin"):
    return SimpleNamespace(
        auth_password=password, auth_bearer_token=bearer, auth_username=username
    )


def test_mint_and_verify_roundtrip():
    cfg = _cfg()
    token = mint_session_cookie(cfg)
    assert token is not None
    assert _value_is_valid(cfg, token)


def test_prefers_bearer_token_as_key():
    # A cookie signed while a bearer token is set must not verify once the
    # signing secret changes (bearer takes precedence over password).
    signed = mint_session_cookie(_cfg(bearer="tok-a"))
    assert not _value_is_valid(_cfg(bearer="tok-b"), signed)


def test_tampered_signature_rejected():
    cfg = _cfg()
    token = mint_session_cookie(cfg)
    payload, _, _sig = token.rpartition(".")
    assert not _value_is_valid(cfg, f"{payload}.deadbeef")


def test_expired_cookie_rejected():
    cfg = _cfg()
    token = mint_session_cookie(cfg, ttl_seconds=-1)
    assert not _value_is_valid(cfg, token)


def test_no_secret_means_no_session():
    cfg = _cfg(password=None, bearer=None)
    assert mint_session_cookie(cfg) is None
    assert not _value_is_valid(cfg, "v1.9999999999.abc")


def test_malformed_values_rejected():
    cfg = _cfg()
    for bad in ["", "nope", "v1.notanumber.sig", "v1"]:
        assert not _value_is_valid(cfg, bad)


def test_request_has_valid_session_reads_cookie():
    cfg = _cfg()
    token = mint_session_cookie(cfg)
    good = SimpleNamespace(cookies={"co_session": token})
    empty = SimpleNamespace(cookies={})
    assert request_has_valid_session(good, cfg)
    assert not request_has_valid_session(empty, cfg)


def test_credentials_valid():
    cfg = _cfg()
    assert credentials_valid(cfg, "admin", "secret")
    assert not credentials_valid(cfg, "admin", "wrong")
    assert not credentials_valid(cfg, "root", "secret")
    assert not credentials_valid(_cfg(password=None), "admin", "secret")


def test_is_safe_next_path():
    assert is_safe_next_path("/web/")
    assert is_safe_next_path("/web/?session=1-2-3")
    assert not is_safe_next_path("//evil.example.com/")
    assert not is_safe_next_path("https://evil.example.com/")
    assert not is_safe_next_path("/\\evil")
    assert not is_safe_next_path("")
    assert not is_safe_next_path("relative")
