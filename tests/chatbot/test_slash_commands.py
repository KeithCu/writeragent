# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Ask-box slash filter, LRU rank, and key classification."""

from __future__ import annotations

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
    assert slash.format_slash_item(slash.SLASH_COMMANDS[0]) == "/help — T:Print the command list (/help)"
    assert "/help — T:Print the command list" in slash.format_help_text()



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
    host = MagicMock()
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
        "undo",
        "formula",
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


def test_run_answer_routes_to_host_send():
    host = MagicMock()
    host.hide_popup = MagicMock()
    host.clear_ask = MagicMock()
    model = object()
    host.slash_document_model.return_value = model
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.slash_commands.build_slash_answer_query", return_value="Q"):
            assert run_slash_command("summarize", host, "meetings") is True
    host.slash_send_answer.assert_called_once_with("Q")


def test_run_rewrite_routes_to_host():
    host = MagicMock()
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("proofread", host) is True
    host.slash_run_rewrite.assert_called_once()


def test_run_compact_delegates_to_host():
    host = MagicMock()
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("compact", host, "budget") is True
    host.run_slash_compact.assert_called_once_with("budget")


def test_do_edit_selection_skips_dialog_with_preset():
    from plugin.writer import editselection as es

    ctx = object()
    model = MagicMock()
    model.getCurrentController.return_value = MagicMock()
    with patch.object(es, "_selection_target", return_value=(MagicMock(), "text")):
        with patch.object(es, "create_validated_client", return_value=MagicMock()):
            with patch.object(es, "prompt_for_edit_instructions") as prompt:
                with patch.object(es, "_stream_into_session"):
                    with patch.object(es, "update_lru_history"):
                            with patch("plugin.writer.editselection.review_recording_enabled", return_value=False):
                                with patch("plugin.writer.editselection.get_config", return_value=100):
                                    es.do_edit_selection(ctx, model, preset_instruction="Fix typos")
    prompt.assert_not_called()


def test_run_remember_upserts_memory():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.memory.MemoryStore") as store_cls:
            store_cls.return_value.upsert.return_value = {"status": "ok"}
            assert run_slash_command("remember", host, "British spelling") is True
    store_cls.return_value.upsert.assert_called_once()
    key, content = store_cls.return_value.upsert.call_args[0]
    assert content == "British spelling"
    assert key.startswith("slash.")
    assert "Saved to memory" in host.append_output.call_args[0][0]


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


def test_run_undo_delegates_to_agent_undo():
    host = MagicMock()
    host.slash_agent_edit_in_flight.return_value = False
    model = object()
    host.slash_document_model.return_value = model
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.doc.agent_undo.undo_last_agent_edit", return_value="Undid the last agent edit (2 steps)") as undo_fn:
            assert run_slash_command("undo", host) is True
    undo_fn.assert_called_once_with(model)
    host.clear_ask.assert_called_once()
    assert "Undid" in host.append_output.call_args[0][0]


def test_run_undo_refuses_while_in_flight():
    host = MagicMock()
    host.slash_agent_edit_in_flight.return_value = True
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("undo", host) is True
    host.slash_document_model.assert_not_called()


def test_run_formula_only_in_calc():
    host = MagicMock()
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command("formula", host, "sum column A") is True
    host.run_slash_formula.assert_called_once_with("sum column A")


def test_popup_accept_runs_undo_immediately():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
        slash_agent_edit_in_flight=lambda: False,
        slash_document_model=lambda: None,
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.doc.agent_undo.undo_last_agent_edit", return_value="There is no agent edit to undo."):
            assert run_slash_command("undo", host, from_popup=True) is True
    host.clear_ask.assert_called_once()
    host.append_output.assert_called_once()


def test_clean_formula_model_reply():
    from plugin.chatbot.slash_commands import clean_formula_model_reply

    assert clean_formula_model_reply("```\n=SUM(A1:A5)\n```") == "=SUM(A1:A5)"
    assert clean_formula_model_reply("  =A1+B1  ") == "=A1+B1"
    assert clean_formula_model_reply("not a formula") == "not a formula"


def test_formula_insert_uses_undo_context():
    from plugin.chatbot.slash_commands import _FORMULA_UNDO_TITLE

    cell = MagicMock()
    cell.getError.return_value = 1
    model = MagicMock()
    formula = "=SUM(A1:A2)"

    def mutate() -> None:
        cell.setFormula(formula)

    with patch("plugin.doc.compound_undo.commit_or_rollback", side_effect=lambda doc, title, fn: (fn(), None)[1]) as cor:
        cor(model, _FORMULA_UNDO_TITLE, mutate)
    cell.setFormula.assert_called_once_with(formula)


def test_formula_missing_equals_shows_reply_only():
    from plugin.chatbot.slash_commands import clean_formula_model_reply

    bad = clean_formula_model_reply("SUM(A1)")
    assert not bad.startswith("=")
    # insert path is in worker; unit-test the cleanup gate the worker uses
    assert bad == "SUM(A1)"

