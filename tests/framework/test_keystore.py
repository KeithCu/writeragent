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


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_concurrent_readers_share_one_backend_call(fake_keystore, locked: bool):
    import threading
    from plugin.framework import keystore

    fake_keystore.store["k"] = "secret-val"
    fake_keystore.locked = locked
    entered = threading.Event()
    release = threading.Event()
    results: list[str | None] = []

    def _on_get(_name: str) -> None:
        entered.set()
        release.wait(timeout=5)

    fake_keystore.on_get = _on_get

    threads = [threading.Thread(target=lambda: results.append(keystore.get_secret("k"))) for _ in range(3)]
    for t in threads:
        t.start()
    assert entered.wait(timeout=5)
    release.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert fake_keystore.get_calls == 1
    expected = None if locked else "secret-val"
    assert results == [expected, expected, expected]


def test_stopped_waiter_leaves_without_locking_while_leader_caches(fake_keystore):
    import threading
    from plugin.framework import keystore
    from plugin.framework.send_cancellation import SendCancellation

    fake_keystore.store["k"] = "secret-val"
    entered = threading.Event()
    release = threading.Event()
    waiter_result: list[str | None] = []
    leader_result: list[str | None] = []

    def _on_get(_name: str) -> None:
        entered.set()
        release.wait(timeout=5)

    fake_keystore.on_get = _on_get
    waiter_scope = SendCancellation()

    leader = threading.Thread(target=lambda: leader_result.append(keystore.get_secret("k")))
    leader.start()
    assert entered.wait(timeout=5)

    waiter = threading.Thread(target=lambda: waiter_result.append(keystore.get_secret("k", cancel=waiter_scope)))
    waiter.start()
    waiter_scope.cancel()
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert waiter_result == [None]
    assert keystore.is_locked("k") is False

    release.set()
    leader.join(timeout=5)
    assert not leader.is_alive()
    assert leader_result == ["secret-val"]
    assert keystore.is_cached("k") is True
    assert fake_keystore.get_calls == 1


def test_already_stopped_scope_makes_no_backend_call(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.send_cancellation import SendCancellation

    fake_keystore.store["k"] = "secret-val"
    scope = SendCancellation()
    scope.cancel()
    assert keystore.get_secret("k", cancel=scope) is None
    assert fake_keystore.get_calls == 0
    assert keystore.is_locked("k") is False
    assert keystore.is_cached("k") is False


def test_libsecret_cancelled_lookup_raises_locked_and_retries_after_window(monkeypatch):
    import ctypes
    from plugin.framework import keystore
    from plugin.framework.send_cancellation import SendCancellation

    scope = SendCancellation()
    cancelled = {"val": 0}
    freed = {"errors": 0, "unref": 0}
    lookup_calls = {"n": 0}

    def _noop(*_args):
        return None

    def _g_cancellable_new():
        return 0x1234

    def _g_cancellable_cancel(_ptr):
        cancelled["val"] = 1

    def _g_cancellable_is_cancelled(_ptr):
        return cancelled["val"]

    def _g_object_unref(_ptr):
        freed["unref"] += 1

    def _g_error_free(_err):
        freed["errors"] += 1

    def _lookupv_sync(_schema, _table, _cancellable, err_out):
        lookup_calls["n"] += 1
        scope.cancel()
        gerr = keystore._GError(domain=1, code=19, message=b"Operation was cancelled")
        err_out._obj.contents = gerr
        return None

    class _FakeLib:
        def __init__(self, mapping):
            for k, v in mapping.items():
                setattr(self, k, v)

    fake_libsecret = _FakeLib({
        "secret_password_lookupv_sync": _lookupv_sync,
        "secret_password_storev_sync": _noop,
        "secret_password_clearv_sync": _noop,
        "secret_password_free": _noop,
    })
    fake_glib = _FakeLib({
        "g_str_hash": _noop,
        "g_str_equal": _noop,
        "g_hash_table_new": lambda *_a: 0x1,
        "g_hash_table_insert": lambda *_a: 1,
        "g_hash_table_unref": _noop,
        "g_error_free": _g_error_free,
        "g_list_free_full": _noop,
    })
    fake_gio = _FakeLib({
        "g_cancellable_new": _g_cancellable_new,
        "g_cancellable_cancel": _g_cancellable_cancel,
        "g_cancellable_is_cancelled": _g_cancellable_is_cancelled,
    })
    fake_gobject = _FakeLib({"g_object_unref": _g_object_unref})

    def _cdll(name):
        if "secret" in name:
            return fake_libsecret
        if "glib" in name:
            return fake_glib
        if "gio" in name:
            return fake_gio
        if "gobject" in name:
            return fake_gobject
        raise AssertionError(name)

    monkeypatch.setattr(ctypes, "CDLL", _cdll)
    backend = object.__new__(keystore._LibSecret)
    backend._g_str_hash = fake_glib.g_str_hash
    backend._g_str_equal = fake_glib.g_str_equal
    backend._g_hash_table_new = fake_glib.g_hash_table_new
    backend._g_hash_table_insert = fake_glib.g_hash_table_insert
    backend._g_hash_table_unref = fake_glib.g_hash_table_unref
    backend._g_error_free = fake_glib.g_error_free
    backend._lookupv_sync = fake_libsecret.secret_password_lookupv_sync
    backend._searchv_sync = None
    backend._g_object_unref = fake_gobject.g_object_unref
    backend._g_cancellable_new = fake_gio.g_cancellable_new
    backend._g_cancellable_cancel = fake_gio.g_cancellable_cancel
    backend._g_cancellable_is_cancelled = fake_gio.g_cancellable_is_cancelled
    backend._secret_password_free = fake_libsecret.secret_password_free
    backend._schema = keystore._SecretSchema(name=b"org.freedesktop.Secret.Generic", flags=2)

    keystore.reset_for_tests(backend)
    try:
        assert keystore.get_secret("k", cancel=scope) is None
        assert lookup_calls["n"] == 1
        assert freed["errors"] == 1
        assert keystore.is_locked("k") is True
        assert keystore.is_cached("k") is False
        assert scope._hooks == []

        with keystore._cache_lock:
            keystore._locked["k"] -= keystore.LOCKED_RETRY_S + 1.0

        scope2 = SendCancellation()
        cancelled["val"] = 0
        scope = scope2
        assert keystore.get_secret("k", cancel=scope2) is None
        assert lookup_calls["n"] == 2
    finally:
        keystore.reset_for_tests()


def test_keystore_unlocked_event_fires_only_after_locked_read(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.event_bus import get_event_bus

    events: list[str] = []
    bus = get_event_bus()

    def handler(**_kw):
        events.append("unlocked")

    bus.subscribe("keystore:unlocked", handler)
    try:
        fake_keystore.store["ordinary"] = "v1"
        assert keystore.get_secret("ordinary") == "v1"
        assert events == []

        fake_keystore.store["locked_one"] = "v2"
        fake_keystore.locked = True
        assert keystore.get_secret("locked_one") is None
        assert events == []

        fake_keystore.locked = False
        with keystore._cache_lock:
            keystore._locked["locked_one"] -= keystore.LOCKED_RETRY_S + 1.0
        assert keystore.get_secret("locked_one") == "v2"
        assert events == ["unlocked"]
    finally:
        bus.unsubscribe("keystore:unlocked", handler)
