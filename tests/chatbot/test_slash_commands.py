# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Ask-box slash filter, LRU rank, and key classification."""

from __future__ import annotations

import json
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
    classify_slash_key,
    filter_slash_commands,
    format_help_text,
    record_slash_lru,
    registered_slash_commands,
    run_slash_command,
    slash_typed_prefix,
)
from plugin.chatbot.send_state import ButtonAction


@pytest.fixture(autouse=True)
def _enable_mock_commands(monkeypatch):
    monkeypatch.setattr("plugin.chatbot.slash_commands._mock_commands_enabled", True)


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
    assert names == [cmd.name for cmd in registered_slash_commands()]


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
    with patch("plugin.framework.model_lru.update_lru_history") as update:
        record_slash_lru("/Help")
        update.assert_called_once_with("help", SLASH_LRU_KEY, "")


def test_run_mock_echoes_and_records_lru():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru") as record:
        assert run_slash_command("mock-alpha", host) is True
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
    with patch("plugin.framework.model_lru.save", side_effect=OSError("write failed")):
        assert run_slash_command("help", host) is True
    host.append_output.assert_called_once()


def test_run_stop_stops_speech_and_hands_free_without_clearing_ask():
    from plugin.chatbot.panel import StopButtonListener

    stop_model = SimpleNamespace(Enabled=True)
    voice_mock = SimpleNamespace(exit_hands_free_record=MagicMock(), stop_during_take=lambda: False)
    host = SimpleNamespace(
        slash_popup=None,
        query_control=SimpleNamespace(text="/stop"),
        dispatch=MagicMock(),
        _approval_event=None,
        _send_busy=False,
        _voice=voice_mock,
        _stop_button_action=ButtonAction.IGNORE,
        stop_control=SimpleNamespace(getModel=lambda: stop_model),
        hide_popup=MagicMock(),
        stop_in_flight=lambda: StopButtonListener(host).on_action_performed(None),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            with patch("plugin.audio.tts_service.is_speaking", return_value=True):
                with patch("plugin.audio.tts_service.stop_speech") as stop_speech:
                    run_slash_command("stop", host)
    set_text.assert_not_called()
    assert host.query_control.text == "/stop"
    stop_speech.assert_called_once()
    voice_mock.exit_hands_free_record.assert_called_once()
    host.dispatch.assert_not_called()
    assert stop_model.Enabled is False


def test_run_stop_during_inline_approval_opens_change_dialog_without_clearing_ask():
    from plugin.chatbot.panel import StopButtonListener
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
        hide_popup=MagicMock(),
        stop_in_flight=lambda: StopButtonListener(host).on_action_performed(None),
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
    tone = next(c for c in slash.SLASH_COMMANDS if c.name == "tone")
    assert slash.format_slash_item(tone) == "/tone — T:Rewrite the selection to match a described tone (/tone <description>)"
    assert "/help — T:Print the command list" in slash.format_help_text()


def test_build_slash_answer_query_is_instruction_only():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, build_slash_answer_query
    from plugin.framework.doc_type import DocumentType

    summarize = next(c for c in SLASH_COMMANDS if c.name == "summarize")
    model = MagicMock()
    with patch("plugin.chatbot.slash_commands.get_document_type", return_value=DocumentType.WRITER):
        text = build_slash_answer_query(summarize, "meetings", model)
    assert "meetings" in text
    assert "Selected text" not in text
    assert "Whole document" not in text


def test_build_slash_answer_explain_calc_adds_formula_line():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, build_slash_answer_query
    from plugin.framework.doc_type import DocumentType

    explain = next(c for c in SLASH_COMMANDS if c.name == "explain")
    model = MagicMock()
    sheet = MagicMock()
    cell = MagicMock()
    cell.getFormula.return_value = "=SUM(A1:A3)"
    cell.getString.return_value = ""
    sheet.getCellByPosition.return_value = cell
    view = MagicMock()
    view.ActiveSheet = sheet
    selection = MagicMock()
    selection.getCellAddress.return_value = SimpleNamespace(Column=0, Row=1)
    view.getSelection.return_value = selection
    model.CurrentController = view
    with patch("plugin.chatbot.slash_commands.get_document_type", return_value=DocumentType.CALC):
        text = build_slash_answer_query(explain, "", model)
    assert "formula: =SUM(A1:A3)" in text
    assert "A2" in text


def test_run_rewrite_passes_instruction_text():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        slash_document_model=lambda: object(),
        slash_run_rewrite=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("proofread", host) is True
    instruction = host.slash_run_rewrite.call_args[0][0]
    assert "grammar" in instruction.lower()


def test_run_answer_returns_continue_for_send():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        slash_document_model=lambda: object(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("summarize", host, "budget") is False


def test_run_translate_writer_no_selection_continues_send():
    from plugin.framework.doc_type import DocumentType

    model = MagicMock()
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        slash_document_model=lambda: model,
        slash_run_rewrite=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.slash_commands._writer_has_selection", return_value=False):
            with patch("plugin.chatbot.slash_commands.get_document_type", return_value=DocumentType.WRITER):
                assert run_slash_command("translate", host, "French") is False
    host.slash_run_rewrite.assert_not_called()


def test_run_compact_returns_continue_for_send():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        run_slash_compact=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("compact", host, "budget") is False
    host.run_slash_compact.assert_not_called()


def test_run_compact_busy_refuses():
    from plugin.chatbot.panel import SendButtonListener

    listener = MagicMock(spec=SendButtonListener)
    listener.sidebar_state = SimpleNamespace(tool_loop=object())
    listener.append_output = MagicMock()
    listener.ctx = object()
    listener.session = None
    listener._send_cancellation = None
    listener._set_status = MagicMock()

    listener.run_slash_compact = SendButtonListener.run_slash_compact.__get__(listener, SendButtonListener)
    listener.run_slash_compact("focus")
    assert "Wait for the current reply" in listener.append_output.call_args[0][0]


def test_run_compact_sync_passes_cancel():
    from plugin.framework.send_cancellation import SendCancellation

    cancel = SendCancellation()
    host = SimpleNamespace()
    session = MagicMock()
    session.messages = [{"role": "user", "content": "hi"}]
    ctx = object()
    with patch("plugin.chatbot.selection.create_validated_client", return_value=MagicMock()):
        with patch("plugin.framework.config.get_config", return_value=True):
            with patch("plugin.chatbot.compaction.compact_session") as compact_mod:
                compact_mod.return_value = MagicMock(compacted=True, reason="ok")
                with patch("plugin.main.get_tools", return_value=MagicMock()):
                    with patch("plugin.framework.client.model_fetcher.resolve_context_window", return_value=4096):
                        with patch("plugin.framework.llm_lanes.llm_request_lane"):
                            from plugin.chatbot.slash_commands import run_slash_compact_sync

                            run_slash_compact_sync(host, focus="", session=session, ctx=ctx, cancel=cancel)
    assert compact_mod.call_args.kwargs.get("cancel") is cancel


def test_run_remember_writes_user_md(tmp_path, monkeypatch):
    from plugin.chatbot.memory import MemoryStore

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    user_md = mem_dir / "USER.md"
    user_md.write_text("{}", encoding="utf-8")

    def fake_store() -> MemoryStore:
        store = MemoryStore.__new__(MemoryStore)
        store.path = str(user_md)
        return store

    monkeypatch.setattr("plugin.chatbot.memory.MemoryStore", fake_store)
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("remember", host, "British spelling") is True
    saved = json.loads(user_md.read_text(encoding="utf-8"))
    assert any("British spelling" in str(v) for v in saved.values())
    assert "Saved to memory" in host.append_output.call_args[0][0]


def test_mock_commands_not_registered_outside_harness(monkeypatch):
    # Keith 2026-10-29: "/mock-bravo [mock]" showed in a real dev build.
    import plugin.chatbot.slash_commands as slash

    monkeypatch.setattr(slash, "_mock_commands_enabled", False)
    names = [c.name for c in slash.registered_slash_commands()]
    assert names[0] == "help"
    assert "summarize" in names
    assert "compact" in names
    assert "mock-bravo" not in names
    assert "[mock]" not in format_help_text()
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    assert run_slash_command("mock-bravo", host) is False
    host.append_output.assert_not_called()


def test_parse_slash_text_known_and_unknown():
    from plugin.chatbot.slash_commands import parse_slash_text

    parsed = parse_slash_text("/tone friendly")
    assert parsed is not None
    cmd, arg = parsed
    assert cmd.name == "tone"
    assert arg == "friendly"
    assert parse_slash_text("/not-a-command foo") is None
    assert parse_slash_text("hello /tone") is None


def test_tier1_commands_are_in_product_table():
    names = {c.name for c in registered_slash_commands()}
    for name in (
        "summarize",
        "proofread",
        "shorten",
        "expand",
        "tone",
        "translate",
        "explain",
        "remember",
        "compact",
    ):
        assert name in names


def test_missing_required_arg_prints_usage():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("tone", host, "") is True
    assert "Usage:" in host.append_output.call_args[0][0]
    host.clear_ask.assert_not_called()


def test_popup_accept_completes_draft_for_preset():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
        query_control=SimpleNamespace(),
        dispatch=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            assert run_slash_command("tone", host, from_popup=True) is True
            set_text.assert_called_once_with(host.query_control, "/tone ")
    host.clear_ask.assert_not_called()
    host.dispatch.assert_called_once()


def test_summarizer_user_body_includes_focus():
    from plugin.chatbot import compaction as compaction_mod

    body = compaction_mod._summarizer_user_body("line", "", focus="budget")
    assert "Keep details about: budget" in body


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
    slash._mock_commands_enabled = False
    assert "mock-bravo" not in [c.name for c in slash.registered_slash_commands()]
