# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for mock_llm_harness config snapshot/restore and keystore isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugin.framework.client.model_fetcher import set_native_audio_support, set_native_vision_support
from plugin.framework.config import (
    api_key_slot_value,
    get_config,
    get_config_dict,
    get_config_file_path,
    get_current_endpoint,
    save,
)
from plugin.framework.model_lru import update_lru_history
from plugin.framework.url_utils import normalize_endpoint_url
from tests.chatbot.mock_llm_harness import (
    _config_backup_path,
    start_mock_sidebar_session,
    stop_mock_sidebar_session,
)
from tests.harness.strip_bundle import is_release_build

pytestmark = pytest.mark.skipif(
    is_release_build(),
    reason="scripts.mock_llm_server is not available in stripped release builds",
)

USER_URL = "https://api.example.com/v1"


def _seed_user_config() -> bytes:
    save({"endpoint": USER_URL, "text_model": "user-model", "chatbot.prompt_for_web_research": True})
    save({"api_keys_by_endpoint": {normalize_endpoint_url(USER_URL): "sk-user"}})
    update_lru_history("user-model", "model_lru", USER_URL)
    return Path(get_config_file_path()).read_bytes()


def _simulate_sidebar_writes(url: str) -> None:
    update_lru_history("mock-model", "model_lru", url)
    update_lru_history("mock-img", "image_model_lru", url)
    update_lru_history("mock-aud", "audio_model_lru", url)
    update_lru_history("mock-tts", "tts_model_lru", url)
    update_lru_history(url, "endpoint_lru", "")
    set_native_audio_support("mock-model", url, True)
    set_native_vision_support("mock-model", url, True)
    save({"slash_command_lru": ["/x"], "chatbot.prompt_for_web_research": False})


def test_stop_restores_config_bytes_and_keystore(fake_keystore) -> None:
    seed_bytes = _seed_user_config()
    keys_before = set(fake_keystore.store.keys())
    set_calls_before = fake_keystore.set_calls

    session = start_mock_sidebar_session(
        delay_ms=0,
        offline=True,
        extra_config={
            "chatbot.librarian_invoked": True,
            "chatbot.prompt_for_web_research": False,
        },
    )
    try:
        assert get_current_endpoint() == session.base_url
        assert api_key_slot_value(session.base_url) == ""
        assert get_config("chatbot.librarian_invoked") is True
        assert get_config("chatbot.prompt_for_web_research") is False
        _simulate_sidebar_writes(session.base_url)
    finally:
        stop_mock_sidebar_session(session)

    assert Path(get_config_file_path()).read_bytes() == seed_bytes
    assert set(fake_keystore.store.keys()) == keys_before
    assert fake_keystore.set_calls == set_calls_before
    assert not Path(_config_backup_path()).exists()


def test_start_recovers_config_left_by_killed_session(fake_keystore) -> None:
    seed_bytes = _seed_user_config()

    killed = start_mock_sidebar_session(delay_ms=0, offline=True)
    first_url = killed.base_url
    _simulate_sidebar_writes(first_url)
    # Simulate os._exit / watchdog kill: stop HTTP thread without calling stop_mock_sidebar_session.
    killed.httpd.shutdown()
    killed.httpd.server_close()
    killed.thread.join(timeout=2)

    session = start_mock_sidebar_session(delay_ms=0, offline=True)
    try:
        cfg_text = Path(get_config_file_path()).read_text(encoding="utf-8")
        assert first_url not in cfg_text
        assert all(first_url not in str(k) and first_url not in str(v) for k, v in get_config_dict().items())
    finally:
        stop_mock_sidebar_session(session)

    assert Path(get_config_file_path()).read_bytes() == seed_bytes


def test_start_keeps_config_user_changed_after_killed_session(fake_keystore) -> None:
    _seed_user_config()

    killed = start_mock_sidebar_session(delay_ms=0, offline=True)
    _simulate_sidebar_writes(killed.base_url)
    killed.httpd.shutdown()
    killed.httpd.server_close()
    killed.thread.join(timeout=2)

    save({"endpoint": "https://other.example.com", "text_model": "m2"})
    user_changed_bytes = Path(get_config_file_path()).read_bytes()

    session = start_mock_sidebar_session(delay_ms=0, offline=True)
    try:
        assert session.base_url != "https://other.example.com"
    finally:
        stop_mock_sidebar_session(session)

    assert Path(get_config_file_path()).read_bytes() == user_changed_bytes
    assert not Path(_config_backup_path()).exists()


def test_failed_start_restores_config(fake_keystore, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_bytes = _seed_user_config()

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("plugin.framework.client.model_fetcher.set_text_model", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        start_mock_sidebar_session(delay_ms=0, offline=True)

    assert Path(get_config_file_path()).read_bytes() == seed_bytes
    assert not Path(_config_backup_path()).exists()


def test_start_drops_malformed_backup(fake_keystore) -> None:
    seed_bytes = _seed_user_config()
    Path(_config_backup_path()).write_text("{not json", encoding="utf-8")

    session = start_mock_sidebar_session(delay_ms=0, offline=True)
    try:
        assert get_current_endpoint() == session.base_url
    finally:
        stop_mock_sidebar_session(session)

    assert Path(get_config_file_path()).read_bytes() == seed_bytes
    assert not Path(_config_backup_path()).exists()


def test_stop_preserves_crlf_bytes(fake_keystore) -> None:
    seed_bytes = _seed_user_config().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    Path(get_config_file_path()).write_bytes(seed_bytes)

    session = start_mock_sidebar_session(delay_ms=0, offline=True)
    stop_mock_sidebar_session(session)

    assert Path(get_config_file_path()).read_bytes() == seed_bytes
    assert not Path(_config_backup_path()).exists()
