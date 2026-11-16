from __future__ import annotations

import logging
import os
import sys
import uuid

import pytest

from plugin.framework import keystore


def test_roundtrip_and_cache(fake_keystore):
    assert keystore.backend_label() == "Fake Store"
    assert keystore.can_store() is True

    assert keystore.set_secret("api_key:https://api.test", "sk-123") is True
    assert fake_keystore.store["api_key:https://api.test"] == "sk-123"
    assert keystore.get_secret("api_key:https://api.test") == "sk-123"
    # set_secret warmed the cache, so get_secret should not hit the backend.
    assert fake_keystore.get_calls == 0

    # Cold cache: first get hits backend once, second get hits cache.
    keystore.reset_for_tests(fake_keystore)
    assert keystore.get_secret("api_key:https://api.test") == "sk-123"
    assert keystore.get_secret("api_key:https://api.test") == "sk-123"
    assert fake_keystore.get_calls == 1

    keystore.delete_secret("api_key:https://api.test")
    assert "api_key:https://api.test" not in fake_keystore.store
    assert keystore.get_secret("api_key:https://api.test") is None


def test_backend_read_error_returns_none(fake_keystore, caplog):
    fake_keystore.fail_get = True
    with caplog.at_level(logging.WARNING, logger="plugin.framework.keystore"):
        assert keystore.get_secret("api_key:https://api.test") is None
        assert keystore.get_secret("api_key:https://api.test") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_locked_read_is_not_cached(fake_keystore, caplog, monkeypatch):
    fake_keystore.store["api_key:https://api.test"] = "sk-123"
    fake_keystore.locked = True
    with caplog.at_level(logging.WARNING, logger="plugin.framework.keystore"):
        assert keystore.get_secret("api_key:https://api.test") is None
        assert keystore.get_secret("api_key:https://api.test") is None
    assert keystore.is_locked("api_key:https://api.test") is True
    # A read right after a locked one does not prompt again.
    assert fake_keystore.get_calls == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    # After the retry window the backend is asked again (the user may have unlocked).
    monkeypatch.setattr(keystore, "LOCKED_RETRY_S", 0.0)
    assert keystore.get_secret("api_key:https://api.test") is None
    assert fake_keystore.get_calls == 2

    fake_keystore.locked = False
    assert keystore.get_secret("api_key:https://api.test") == "sk-123"
    assert keystore.is_locked("api_key:https://api.test") is False


def test_write_failure_disables_writes_for_session(fake_keystore):
    fake_keystore.fail_set = True
    assert keystore.set_secret("k1", "v1") is False
    assert keystore.can_store() is False
    assert fake_keystore.set_calls == 1

    fake_keystore.fail_set = False
    assert keystore.set_secret("k2", "v2") is False
    assert fake_keystore.set_calls == 1


def test_env_switch_disables_store(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_KEYSTORE", "0")
    keystore.reset_for_tests()
    try:
        assert keystore.backend_label() == ""
        assert keystore.can_store() is False
        assert keystore.set_secret("k", "v") is False
        assert keystore.get_secret("k") is None
    finally:
        keystore.reset_for_tests()


def test_libsecret_missing_means_no_store(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_KEYSTORE", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    def _raise_oserror(*args, **kwargs):
        raise OSError("no libsecret")

    monkeypatch.setattr(keystore.ctypes, "CDLL", _raise_oserror)
    assert keystore._detect() is None


def test_stale_reader_does_not_overwrite_newer_set(fake_keystore):
    name = "api_key:https://api.test"
    fake_keystore.store[name] = "old"

    def hijack_with_set(key: str) -> None:
        fake_keystore.on_get = None
        assert keystore.set_secret(key, "new") is True

    fake_keystore.on_get = hijack_with_set
    assert keystore.get_secret(name) == "new"
    assert keystore.get_secret(name) == "new"

    # Same interleaving with delete_secret during an in-flight read.
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.store[name] = "old"

    def hijack_with_delete(key: str) -> None:
        fake_keystore.on_get = None
        keystore.delete_secret(key)

    fake_keystore.on_get = hijack_with_delete
    assert keystore.get_secret(name) is None
    assert keystore.get_secret(name) is None


def test_libsecret_without_search_still_reads(monkeypatch):
    from unittest.mock import MagicMock

    class _FakeLib:
        def __getattr__(self, item: str):
            if item == "secret_password_searchv_sync":
                raise AttributeError(item)
            return MagicMock(return_value=0)

    monkeypatch.setattr(keystore.ctypes, "CDLL", lambda *_args, **_kwargs: _FakeLib())
    backend = keystore._LibSecret()
    assert backend.get("missing") is None


def test_locked_read_does_not_hide_a_key_saved_meanwhile(fake_keystore):
    name = "api_key:https://api.test"
    fake_keystore.store[name] = "sk-old"
    fake_keystore.locked = True

    def save_meanwhile(key: str) -> None:
        fake_keystore.on_get = None
        fake_keystore.locked = False
        assert keystore.set_secret(key, "sk-new") is True
        fake_keystore.locked = True

    fake_keystore.on_get = save_meanwhile
    assert keystore.get_secret(name) == "sk-new"
    assert keystore.is_locked(name) is False


@pytest.mark.skipif(os.environ.get("CI") != "true", reason="Live OS keystore test runs on CI runners only")
def test_live_backend_roundtrip():
    try:
        if sys.platform == "win32":
            backend = keystore._WinCredentials()
        elif sys.platform == "darwin":
            backend = keystore._MacKeychain()
        else:
            backend = keystore._LibSecret()
    except (OSError, AttributeError, keystore.KeystoreError) as exc:
        pytest.skip(f"Platform keystore unavailable: {exc}")

    name = f"ci_probe_{uuid.uuid4().hex}"
    try:
        backend.set(name, "sk-é-ü")
        assert backend.get(name) == "sk-é-ü"
        backend.delete(name)
        assert backend.get(name) is None
        backend.delete(name)
        assert backend.get(f"missing_{uuid.uuid4().hex}") is None
    finally:
        backend.delete(name)
