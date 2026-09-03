# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Ask-box slash filter, LRU rank, and key classification."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.chatbot.slash_commands import (
    KEY_DOWN,
    KEY_ESCAPE,
    KEY_MODIFIER_SHIFT,
    KEY_RETURN,
    KEY_TAB,
    KEY_UP,
    SLASH_COMMANDS,
    SLASH_LRU_KEY,
    classify_slash_key,
    filter_slash_commands,
    format_help_text,
    record_slash_lru,
    run_slash_command,
    slash_typed_prefix,
)
from plugin.chatbot.send_state import SendEventKind


def _names(typed: str, lru: list[str] | None = None) -> list[str]:
    return [cmd.name for cmd in filter_slash_commands(typed, lru)]


def test_slash_typed_prefix_full_list_and_narrow():
    assert slash_typed_prefix("/") == ""
    assert slash_typed_prefix("/he") == "he"
    assert slash_typed_prefix("  /Help") == "help"
    assert slash_typed_prefix("hello") is None
    assert slash_typed_prefix("") is None


def test_slash_opens_full_registry_order_without_lru():
    names = _names("/")
    assert names[0] == "help"
    assert "clear" in names
    assert "mock-alpha" in names
    assert "mock-bravo" in names
    assert "mock-charlie" in names
    assert names == [cmd.name for cmd in SLASH_COMMANDS]


def test_he_prefix_selects_help_as_top_match():
    matches = filter_slash_commands("/he")
    assert [cmd.name for cmd in matches] == ["help"]


def test_further_typing_narrows_mocks():
    assert _names("/mock-") == ["mock-alpha", "mock-bravo", "mock-charlie"]
    assert _names("/mock-a") == ["mock-alpha"]
    assert _names("/zzz") == []


def test_lru_floats_to_top_on_empty_filter():
    names = _names("/", lru=["mock-bravo", "clear"])
    assert names[:2] == ["mock-bravo", "clear"]
    assert "help" in names


def test_lru_is_tie_break_among_prefix_matches():
    names = _names("/m", lru=["model"])
    assert names[0] == "model"
    assert "mock-alpha" in names


def test_classify_slash_keys():
    assert classify_slash_key(KEY_RETURN, 0) == "enter"
    assert classify_slash_key(KEY_RETURN, KEY_MODIFIER_SHIFT) is None
    assert classify_slash_key(KEY_ESCAPE) == "escape"
    assert classify_slash_key(KEY_UP) == "up"
    assert classify_slash_key(KEY_DOWN) == "down"
    assert classify_slash_key(KEY_TAB) == "tab"
    assert classify_slash_key(65) is None


def test_help_text_lists_wired_and_mocks():
    text = format_help_text()
    assert "/help" in text
    assert "/mock-alpha [mock]" in text
    assert "/clear" in text


def test_record_slash_lru_uses_existing_config_helper():
    with patch("plugin.chatbot.config_ui_helpers.update_lru_history") as update:
        record_slash_lru("/Help")
        update.assert_called_once_with("help", SLASH_LRU_KEY, "")


def test_run_mock_echoes_and_records_lru():
    host = SimpleNamespace(
        slash_popup=SimpleNamespace(hide=MagicMock()),
        query_control=MagicMock(),
        dispatch=MagicMock(),
        _append_response=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru") as record:
        with patch("plugin.chatbot.dialogs.set_control_text"):
            assert run_slash_command("mock-alpha", host) is True
    record.assert_called_once_with("mock-alpha")
    host.slash_popup.hide.assert_called_once()
    host._append_response.assert_called_once()
    assert "slash: /mock-alpha" in host._append_response.call_args[0][0]
    event = host.dispatch.call_args[0][0]
    assert event.kind is SendEventKind.TEXT_UPDATED
    assert event.data == {"has_text": False}


def test_run_help_prints_static_list():
    host = SimpleNamespace(
        slash_popup=None,
        query_control=None,
        dispatch=MagicMock(),
        _append_response=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        run_slash_command("help", host)
    body = host._append_response.call_args[0][0]
    assert "Slash commands:" in body
    assert "/help" in body


def test_run_clear_calls_existing_clear_listener():
    clear = SimpleNamespace(on_action_performed=MagicMock())
    host = SimpleNamespace(
        slash_popup=None,
        query_control=None,
        dispatch=MagicMock(),
        clear_listener=clear,
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        run_slash_command("clear", host)
    clear.on_action_performed.assert_called_once_with(None)


def test_run_stop_dispatches_stop_clicked():
    host = SimpleNamespace(
        slash_popup=None,
        query_control=None,
        dispatch=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        run_slash_command("stop", host)
    event = host.dispatch.call_args[0][0]
    assert event.kind is SendEventKind.STOP_CLICKED
