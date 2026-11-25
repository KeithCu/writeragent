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
def test_concurrent_readers_share_one_backend_call(monkeypatch, fake_keystore, locked: bool):
    import threading
    from plugin.framework import keystore

    fake_keystore.store["k"] = "secret-val"
    fake_keystore.locked = locked
    entered = threading.Event()
    release = threading.Event()
    all_arrived = threading.Event()
    arrivals_lock = threading.Lock()
    arrivals = 0
    results: list[str | None] = []

    orig_acquire = keystore.acquire_unless_cancelled

    def _counting_acquire(lock, cancel):
        nonlocal arrivals
        with arrivals_lock:
            arrivals += 1
            if arrivals == 3:
                all_arrived.set()
        return orig_acquire(lock, cancel)

    monkeypatch.setattr(keystore, "acquire_unless_cancelled", _counting_acquire)

    def _on_get(_name: str) -> None:
        entered.set()
        release.wait(timeout=5)

    fake_keystore.on_get = _on_get

    threads = [threading.Thread(target=lambda: results.append(keystore.get_secret("k"))) for _ in range(3)]
    for t in threads:
        t.start()
    assert entered.wait(timeout=5)
    assert all_arrived.wait(timeout=5)
    release.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert fake_keystore.get_calls == 1
    expected = None if locked else "secret-val"
    assert results == [expected, expected, expected]


def test_stopped_waiter_leaves_without_locking_while_leader_caches(monkeypatch, fake_keystore):
    import threading
    from plugin.framework import keystore
    from plugin.framework.send_cancellation import SendCancellation

    fake_keystore.store["k"] = "secret-val"
    entered = threading.Event()
    release = threading.Event()
    waiter_arrived = threading.Event()
    arrivals_lock = threading.Lock()
    arrivals = 0
    waiter_result: list[str | None] = []
    leader_result: list[str | None] = []

    orig_acquire = keystore.acquire_unless_cancelled

    def _counting_acquire(lock, cancel):
        nonlocal arrivals
        with arrivals_lock:
            arrivals += 1
            if arrivals == 2:
                waiter_arrived.set()
        return orig_acquire(lock, cancel)

    monkeypatch.setattr(keystore, "acquire_unless_cancelled", _counting_acquire)

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
    assert waiter_arrived.wait(timeout=5)
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
    import gc
    from plugin.framework import keystore
    from plugin.framework.send_cancellation import SendCancellation

    cancelled = {"val": 0}
    freed = {"errors": 0, "unref": 0}
    lookup_calls = {"n": 0}
    active_scope: dict[str, object] = {"scope": None, "claim_only": False, "copied_hook": None}

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

    def _lookupv_sync(_schema, _table, cancellable_ptr, err_out):
        if cancellable_ptr is None:
            return None
        lookup_calls["n"] += 1
        scope = active_scope["scope"]
        if scope is not None:
            if active_scope["claim_only"]:
                active_scope["copied_hook"] = scope.claim_cancel()
            else:
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
    backend = keystore._LibSecret()

    keystore.reset_for_tests(backend)
    try:
        # 1. Direct backend.get raises KeystoreStopped when cancelled.
        scope0 = SendCancellation()
        active_scope["scope"] = scope0
        with pytest.raises(keystore.KeystoreStopped):
            backend.get("k", cancel=scope0)
        assert freed["errors"] == 1
        assert scope0._hooks == []

        # 2. Late Stop hook keeps GCancellable alive until the copied hook runs and is dropped.
        cancelled["val"] = 0
        scope_late = SendCancellation()
        active_scope["scope"] = scope_late
        active_scope["claim_only"] = True
        unref_before = freed["unref"]
        with pytest.raises(keystore.KeystoreError):
            backend.get("k", cancel=scope_late)
        gc.collect()
        assert freed["unref"] == unref_before
        assert active_scope["copied_hook"] is not None
        active_scope["copied_hook"]()
        active_scope["copied_hook"] = None
        gc.collect()
        assert freed["unref"] == unref_before + 1

        # 3. Through get_secret, cancellation marks locked (not cached) and retries after LOCKED_RETRY_S.
        cancelled["val"] = 0
        active_scope["claim_only"] = False
        scope1 = SendCancellation()
        active_scope["scope"] = scope1
        assert keystore.get_secret("k", cancel=scope1) is None
        assert keystore.is_locked("k") is True
        assert keystore.is_cached("k") is False
        assert scope1._hooks == []

        with keystore._cache_lock:
            keystore._locked["k"] -= keystore.LOCKED_RETRY_S + 1.0

        scope2 = SendCancellation()
        cancelled["val"] = 0
        active_scope["scope"] = scope2
        before_retry = lookup_calls["n"]
        assert keystore.get_secret("k", cancel=scope2) is None
        assert lookup_calls["n"] == before_retry + 1

        # 4. Stop on "k" does not latch another name "other".
        scope3 = SendCancellation()
        cancelled["val"] = 0
        active_scope["scope"] = None
        before_other = lookup_calls["n"]
        assert keystore.get_secret("other", cancel=scope3) is None
        assert lookup_calls["n"] == before_other + 1
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


def test_is_ref_matches_only_the_exact_form():
    assert keystore.is_ref("keystore:deadbeef") is True
    assert keystore.is_ref("keystore:01234567") is True
    assert keystore.is_ref("keystore:9f3a1c2bsk-new") is False
    assert keystore.is_ref("keystore:") is False
    assert keystore.is_ref("keystore:deadbee") is False
    assert keystore.is_ref("keystore:legacy01") is False
    assert keystore.is_ref("keystore:DEADBEEF") is False
    assert keystore.is_ref(None) is False


def test_one_lock_shares_locked_window_across_names(fake_keystore):
    from plugin.framework import keystore

    fake_keystore.store.update({"a": "va", "b": "vb"})
    fake_keystore.locked = True

    fake_keystore.one_lock = True
    assert keystore.get_secret("a") is None
    assert fake_keystore.get_calls == 1
    assert keystore.get_secret("b") is None
    assert fake_keystore.get_calls == 1
    assert keystore.is_locked("b") is True

    keystore.reset_for_tests(fake_keystore)
    fake_keystore.get_calls = 0
    fake_keystore.one_lock = False
    assert keystore.get_secret("a") is None
    assert keystore.get_secret("b") is None
    assert fake_keystore.get_calls == 2


def test_one_lock_concurrent_reads_share_one_backend_call(fake_keystore):
    import threading
    from plugin.framework import keystore

    fake_keystore.store.update({"a": "va", "b": "vb"})
    fake_keystore.locked = True
    fake_keystore.one_lock = True

    entered = threading.Event()
    b_ready = threading.Event()
    release = threading.Event()
    entered_names: list[str] = []

    def slow_locked(name: str) -> None:
        entered_names.append(name)
        entered.set()
        release.wait(timeout=2.0)

    fake_keystore.on_get = slow_locked
    results: list[str | None] = []
    t1 = threading.Thread(target=lambda: results.append(keystore.get_secret("a")))
    t2 = threading.Thread(target=lambda: (b_ready.set(), results.append(keystore.get_secret("b"))))
    t1.start()
    assert entered.wait(timeout=2.0)
    t2.start()
    assert b_ready.wait(timeout=2.0)
    # Wait briefly for t2 to block on the shared read lock before releasing t1.
    t2.join(timeout=0.05)
    assert t2.is_alive()
    assert entered_names == ["a"]
    release.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert results == [None, None]
    assert entered_names == ["a"]
    assert fake_keystore.get_calls == 1


def test_keystore_stopped_does_not_latch_other_names(fake_keystore):
    from plugin.framework import keystore

    fake_keystore.one_lock = True
    fake_keystore.store["b"] = "vb"

    def raise_stopped(name: str) -> None:
        if name == "a":
            raise keystore.KeystoreStopped("stopped")

    fake_keystore.on_get = raise_stopped
    assert keystore.get_secret("a") is None
    assert keystore.is_locked("a") is True
    assert keystore.get_secret("b") == "vb"


def test_one_lock_success_clears_other_locked_marks_and_emits_unlocked(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.event_bus import get_event_bus

    events: list[str] = []
    bus = get_event_bus()

    def handler(**_kw):
        events.append("unlocked")

    bus.subscribe("keystore:unlocked", handler)
    try:
        fake_keystore.one_lock = True
        fake_keystore.store.update({"a": "va", "b": "vb"})
        fake_keystore.locked = True
        assert keystore.get_secret("a") is None
        assert keystore.get_secret("b") is None
        assert keystore.is_locked("a") is True
        assert keystore.is_locked("b") is True

        fake_keystore.locked = False
        with keystore._cache_lock:
            keystore._locked["a"] -= keystore.LOCKED_RETRY_S + 1.0
            keystore._locked["b"] -= keystore.LOCKED_RETRY_S + 1.0
            assert keystore._store_locked_at is not None
            keystore._store_locked_at -= keystore.LOCKED_RETRY_S + 1.0

        assert keystore.get_secret("a") == "va"
        assert keystore.is_locked("a") is False
        assert keystore.is_locked("b") is False
        assert keystore._store_locked_at is None
        assert events == ["unlocked"]
    finally:
        bus.unsubscribe("keystore:unlocked", handler)


def test_one_lock_missing_item_does_not_clear_latch(fake_keystore):
    from plugin.framework import keystore

    fake_keystore.one_lock = True

    def stop_a(name: str) -> None:
        if name == "a":
            raise keystore.KeystoreStopped("stopped")

    fake_keystore.on_get = stop_a
    assert keystore.get_secret("a") is None
    assert keystore.is_locked("a") is True

    # Reading a missing item reaches the backend and must not clear "a"'s locked mark.
    before = fake_keystore.get_calls
    assert keystore.get_secret("missing") is None
    assert fake_keystore.get_calls == before + 1
    assert keystore.is_locked("a") is True
