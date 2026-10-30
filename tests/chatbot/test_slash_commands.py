# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Ask-box slash filter, LRU rank, and key classification."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.slash_commands import (
    KEY_DOWN,
    KEY_ESCAPE,
    KEY_MODIFIER_SHIFT,
    KEY_RETURN,
    KEY_TAB,
    KEY_UP,
    SLASH_LRU_KEY,
    SlashCommand,
    classify_slash_key,
    filter_slash_commands,
    format_help_text,
    record_slash_lru,
    run_slash_command,
    slash_typed_prefix,
)
from plugin.chatbot.send_state import ButtonAction


@pytest.fixture
def mock_commands() -> tuple[SlashCommand, ...]:
    return (
        SlashCommand("help", "Print the command list", "wired", clears_ask=True),
        SlashCommand("clear", "Start a new chat", "wired", clears_ask=True),
        SlashCommand("stop", "Cancel the in-flight send", "wired", clears_ask=False),
        SlashCommand("mock-alpha", "Mock command (echo only)", "mock", clears_ask=True),
        SlashCommand("mock-bravo", "Mock command (echo only)", "mock", clears_ask=True),
        SlashCommand("mock-charlie", "Mock command (echo only)", "mock", clears_ask=True),
        SlashCommand("apply", "Apply last draft (mock)", "mock", clears_ask=True),
        SlashCommand("web", "Web research (mock)", "mock", clears_ask=True),
        SlashCommand("model", "Switch model (mock)", "mock", clears_ask=True),
    )


def _names(typed: str, lru: list[str] | None = None, commands: Sequence[SlashCommand] | None = None) -> list[str]:
    return [cmd.name for cmd in filter_slash_commands(typed, lru, commands=commands)]


def test_slash_typed_prefix_full_list_and_narrow():
    assert slash_typed_prefix("/") == ""
    assert slash_typed_prefix("/he") == "he"
    assert slash_typed_prefix("  /Help") == "help"
    assert slash_typed_prefix("hello") is None
    assert slash_typed_prefix("") is None


def test_slash_opens_full_registry_order_without_lru(mock_commands):
    names = _names("/", commands=mock_commands)
    assert names[0] == "help"
    assert "clear" in names
    assert "mock-alpha" in names
    assert "mock-bravo" in names
    assert "mock-charlie" in names
    assert names == [cmd.name for cmd in mock_commands]


def test_he_prefix_selects_help_as_top_match():
    matches = filter_slash_commands("/he")
    assert [cmd.name for cmd in matches] == ["help"]


def test_further_typing_narrows_mocks(mock_commands):
    assert _names("/mock-", commands=mock_commands) == ["mock-alpha", "mock-bravo", "mock-charlie"]
    assert _names("/mock-a", commands=mock_commands) == ["mock-alpha"]
    assert _names("/zzz", commands=mock_commands) == []


def test_lru_floats_to_top_on_empty_filter(mock_commands):
    names = _names("/", lru=["mock-bravo", "clear"], commands=mock_commands)
    assert names[:2] == ["mock-bravo", "clear"]
    assert "help" in names


def test_lru_is_tie_break_among_prefix_matches(mock_commands):
    names = _names("/m", lru=["model"], commands=mock_commands)
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


def test_help_text_lists_wired_and_mocks(mock_commands):
    text = format_help_text(mock_commands)
    assert "/help" in text
    assert "/mock-alpha [mock]" in text
    assert "/clear" in text


def test_record_slash_lru_uses_existing_config_helper():
    with patch("plugin.framework.model_lru.update_lru_history") as update:
        record_slash_lru("/Help")
        update.assert_called_once_with("help", SLASH_LRU_KEY, "")


def test_run_mock_echoes_and_records_lru(mock_commands):
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru") as record:
        assert run_slash_command("mock-alpha", host, commands=mock_commands) is True
    record.assert_called_once_with("mock-alpha")
    host.hide_popup.assert_called_once()
    host.clear_ask.assert_called_once()
    host.append_output.assert_called_once()
    assert "slash: /mock-alpha" in host.append_output.call_args[0][0]
    # Real append_output(text) takes no role keyword (panel.py, _UrpSlashHost).
    assert host.append_output.call_args.kwargs == {}


def test_run_help_prints_static_list():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("help", host) is True
    body = host.append_output.call_args[0][0]
    assert "Slash commands:" in body
    assert "/help" in body
    host.clear_ask.assert_called_once()


def test_run_clear_calls_existing_clear_listener():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        clear_chat=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("clear", host) is True
    host.clear_ask.assert_called_once()
    host.clear_chat.assert_called_once()


def test_run_stop_uses_stop_button_and_keeps_draft():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        stop_in_flight=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("stop", host) is True
    host.stop_in_flight.assert_called_once()
    host.clear_ask.assert_not_called()


def test_set_config_raising_still_runs_help():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.framework.model_lru.set_config", side_effect=OSError("write failed")):
        assert run_slash_command("help", host) is True
    host.append_output.assert_called_once()


def test_run_stop_stops_speech_and_hands_free_without_clearing_ask():
    stop_model = SimpleNamespace(Enabled=True)
    host = SimpleNamespace(
        slash_popup=None,
        query_control=SimpleNamespace(text="/stop"),
        dispatch=MagicMock(),
        _approval_event=None,
        _send_busy=False,
        exit_hands_free_record=MagicMock(),
        stop_control=SimpleNamespace(getModel=lambda: stop_model),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            with patch("plugin.audio.tts_service.is_speaking", return_value=True):
                with patch("plugin.audio.tts_service.stop_speech") as stop_speech:
                    run_slash_command("stop", host)
    set_text.assert_not_called()
    assert host.query_control.text == "/stop"
    stop_speech.assert_called_once()
    host.exit_hands_free_record.assert_called_once()
    host.dispatch.assert_not_called()
    assert stop_model.Enabled is False


def test_run_stop_during_inline_approval_opens_change_dialog_without_clearing_ask():
    from plugin.framework.i18n import _

    stop_model = SimpleNamespace(Label=_("Change"), Enabled=True)
    events_mock = SimpleNamespace(
        _open_web_search_change_dialog=MagicMock(),
        _finish_inline_web_approval=MagicMock(),
    )
    host = SimpleNamespace(
        slash_popup=None,
        query_control=SimpleNamespace(text="keep me"),
        dispatch=MagicMock(),
        _approval_event=object(),
        stop_control=SimpleNamespace(getModel=lambda: stop_model),
        _stop_button_action=ButtonAction.CHANGE,
        _events=events_mock,
        _finish_inline_web_approval=events_mock._finish_inline_web_approval,
        _open_web_search_change_dialog=events_mock._open_web_search_change_dialog,
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            run_slash_command("stop", host)
    set_text.assert_not_called()
    assert host.query_control.text == "keep me"
    events_mock._open_web_search_change_dialog.assert_called_once_with()
    events_mock._finish_inline_web_approval.assert_not_called()
    host.dispatch.assert_not_called()


def test_slash_descriptions_are_translated_at_display_time(monkeypatch):
    import plugin.chatbot.slash_commands as slash

    assert slash.SLASH_COMMANDS[0].description == "Print the command list"
    monkeypatch.setattr(slash, "_", lambda msg: "T:" + msg)
    assert slash.format_slash_item(slash.SLASH_COMMANDS[0]) == "/help — T:Print the command list"
    assert "/help — T:Print the command list" in slash.format_help_text()



def test_mock_commands_not_registered_outside_harness(monkeypatch):
    # Keith 2026-10-29: "/mock-bravo [mock]" showed in a real dev build.
    import plugin.chatbot.slash_commands as slash

    monkeypatch.setattr(slash, "_mock_commands_enabled", False)
    assert [c.name for c in slash.registered_slash_commands()] == ["help", "clear", "stop"]
    assert all(c.kind == "wired" for c in slash.SLASH_COMMANDS)
    assert "mock" not in [c.kind for c in filter_slash_commands("/", ["mock-bravo"])]
    assert "[mock]" not in format_help_text()
    host = MagicMock()
    assert run_slash_command("mock-bravo", host) is False
    host.append_output.assert_not_called()


def test_enable_mock_slash_commands_adds_stubs(monkeypatch):
    import plugin.chatbot.slash_commands as slash

    monkeypatch.setattr(slash, "_mock_commands_enabled", False)
    slash.enable_mock_slash_commands()
    names = [c.name for c in slash.registered_slash_commands()]
    assert names[:3] == ["help", "clear", "stop"]
    assert "mock-bravo" in names
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        host = MagicMock()
        assert run_slash_command("mock-bravo", host) is True
    slash.enable_mock_slash_commands(False)
    assert "mock-bravo" not in [c.name for c in slash.registered_slash_commands()]
