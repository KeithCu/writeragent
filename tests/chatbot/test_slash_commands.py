# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Ask-box slash filter, LRU rank, and key classification."""

from __future__ import annotations

import json
import threading
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
    MOCK_SLASH_COMMANDS,
    SLASH_COMMANDS,
    SlashResult,
    SlashRun,
    effective_slash_run,
    plan_slash,
    run_slash_command,
    run_slash_compact,
    slash_continues_send,
    slash_popup_runs_now,
    slash_typed_prefix,
)
from plugin.chatbot.send_state import ButtonAction


@pytest.fixture(autouse=True)
def _enable_mock_commands(monkeypatch):
    monkeypatch.setattr("plugin.chatbot.slash_commands._mock_commands_enabled", True)


def _cmd(name: str):
    from plugin.chatbot.slash_commands import get_slash_command

    cmd = get_slash_command(name)
    assert cmd is not None, name
    return cmd


def _names(typed: str, lru: list[str] | None = None, doc_type: str | None = None) -> list[str]:
    return [cmd.name for cmd in filter_slash_commands(typed, lru, doc_type)]


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


def test_slash_commands_filtered_by_doc_type():
    assert "formula" not in _names("/", doc_type="writer")
    assert "proofread" in _names("/", doc_type="writer")
    assert "/formula" not in format_help_text("writer")
    assert "/proofread" in format_help_text("writer")

    for app in ("draw", "impress"):
        names = _names("/", doc_type=app)
        assert "formula" not in names
        for rewrite in ("proofread", "shorten", "expand", "tone", "translate"):
            assert rewrite in names
        help_text = format_help_text(app)
        for rewrite in ("proofread", "shorten", "expand", "tone", "translate"):
            assert f"/{rewrite}" in help_text

    calc_names = _names("/", doc_type="calc")
    assert "proofread" in calc_names and "formula" in calc_names

    unknown = _names("/", doc_type="unknown")
    assert "formula" not in unknown and "proofread" not in unknown


def test_run_slash_formula_ignores_apps_filter():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
        cached_doc_type="writer",
        slash_run_formula=MagicMock(),
        session=SimpleNamespace(messages=[]),
        sidebar_state=SimpleNamespace(tool_loop=None),
        _send_cancellation=None,
        query_control=MagicMock(),
        dispatch=MagicMock(),
        _set_status=MagicMock(),
        slash_document_model=MagicMock(return_value=MagicMock()),
        slash_agent_edit_in_flight=lambda: False,
        sync_has_text_from_query=MagicMock(),
    )
    cmd = _cmd("formula")
    assert cmd.apps == ("calc",)
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(cmd, host, "sum column") is SlashResult.CONTINUE_SEND


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
        assert run_slash_command(_cmd("mock-alpha"), host) is SlashResult.HANDLED
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
        cached_doc_type=None,
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(_cmd("help"), host) is SlashResult.HANDLED
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
        assert run_slash_command(_cmd("clear"), host) is SlashResult.HANDLED
    host.clear_ask.assert_called_once()
    host.clear_chat.assert_called_once()


def test_run_stop_uses_stop_button_and_clears_ask():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        stop_in_flight=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(_cmd("stop"), host) is SlashResult.HANDLED
    host.stop_in_flight.assert_called_once()
    host.clear_ask.assert_called_once()


def test_set_config_raising_still_runs_help():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
        cached_doc_type=None,
    )
    with patch("plugin.framework.model_lru.save", side_effect=OSError("write failed")):
        assert run_slash_command(_cmd("help"), host) is SlashResult.HANDLED
    host.append_output.assert_called_once()


def test_run_stop_stops_speech_and_hands_free_and_clears_ask():
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
        clear_ask=MagicMock(),
        stop_in_flight=lambda: StopButtonListener(host).on_action_performed(None),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            with patch("plugin.audio.tts_service.is_speaking", return_value=True):
                with patch("plugin.audio.tts_service.stop_speech") as stop_speech:
                    run_slash_command(_cmd("stop"), host)
    set_text.assert_not_called()
    host.clear_ask.assert_called_once()
    stop_speech.assert_called_once()
    voice_mock.exit_hands_free_record.assert_called_once()
    host.dispatch.assert_not_called()
    assert stop_model.Enabled is False


def test_run_stop_during_inline_approval_opens_change_dialog_and_clears_ask():
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
        clear_ask=MagicMock(),
        stop_in_flight=lambda: StopButtonListener(host).on_action_performed(None),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            run_slash_command(_cmd("stop"), host)
    set_text.assert_not_called()
    host.clear_ask.assert_called_once()
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


def test_build_slash_answer_explain_writer_adds_selection_block():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, build_slash_answer_query
    from plugin.framework.doc_type import DocumentType

    explain = next(c for c in SLASH_COMMANDS if c.name == "explain")
    model = MagicMock()
    with patch("plugin.chatbot.slash_commands.get_document_type", return_value=DocumentType.WRITER):
        with patch("plugin.chatbot.slash_commands._writer_has_selection", return_value=True):
            with patch(
                "plugin.chatbot.slash_commands._writer_selection_explain_block",
                return_value="Selected text:\nHello world",
            ):
                text = build_slash_answer_query(explain, "", model)
    assert "Selected text:" in text
    assert "Hello world" in text


def test_try_dispatch_undo_before_send_busy():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, try_dispatch_slash_on_send_click

    undo = next(c for c in SLASH_COMMANDS if c.name == "undo")
    send = SimpleNamespace(is_busy=False)
    host = SimpleNamespace(
        query_control=MagicMock(),
        sidebar_state=SimpleNamespace(send=send),
        slash_document_model=lambda: None,
    )
    host.query_control.getModel.return_value = True
    with patch("plugin.chatbot.slash_commands._peek_slash_from_ask", return_value=(undo, "")):
        with patch("plugin.chatbot.slash_commands.run_slash_command", return_value=SlashResult.HANDLED) as run_cmd:
            assert try_dispatch_slash_on_send_click(host) is True
    run_cmd.assert_called_once_with(undo, host, "", from_popup=False)


def test_try_dispatch_remember_while_busy():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, try_dispatch_slash_on_send_click

    remember = next(c for c in SLASH_COMMANDS if c.name == "remember")
    send = SimpleNamespace(is_busy=True)
    host = SimpleNamespace(
        query_control=MagicMock(),
        sidebar_state=SimpleNamespace(send=send),
        slash_document_model=lambda: None,
    )
    with patch("plugin.chatbot.slash_commands._peek_slash_from_ask", return_value=(remember, "Denali")):
        with patch("plugin.chatbot.slash_commands.run_slash_command", return_value=SlashResult.HANDLED) as run_cmd:
            assert try_dispatch_slash_on_send_click(host) is True
    run_cmd.assert_called_once_with(remember, host, "Denali", from_popup=False)


def test_try_dispatch_refuses_undo_while_busy():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, try_dispatch_slash_on_send_click

    undo = next(c for c in SLASH_COMMANDS if c.name == "undo")
    send = SimpleNamespace(is_busy=True)
    host = SimpleNamespace(
        query_control=MagicMock(),
        sidebar_state=SimpleNamespace(send=send),
        slash_document_model=lambda: None,
    )
    with patch("plugin.chatbot.slash_commands._peek_slash_from_ask", return_value=(undo, "")):
        with patch("plugin.chatbot.slash_commands.run_slash_command") as run_cmd:
            assert try_dispatch_slash_on_send_click(host) is False
    run_cmd.assert_not_called()


def test_run_rewrite_passes_instruction_text():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        slash_document_model=lambda: object(),
        slash_run_rewrite=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(_cmd("proofread"), host) is SlashResult.HANDLED
    instruction = host.slash_run_rewrite.call_args[0][0]
    assert "grammar" in instruction.lower()


def test_run_answer_returns_continue_for_send():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        slash_document_model=lambda: object(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(_cmd("summarize"), host, "budget") is SlashResult.CONTINUE_SEND


def test_effective_slash_run_for_send_turn_commands():
    compact = next(c for c in SLASH_COMMANDS if c.name == "compact")
    summarize = next(c for c in SLASH_COMMANDS if c.name == "summarize")
    proofread = next(c for c in SLASH_COMMANDS if c.name == "proofread")
    formula = next(c for c in SLASH_COMMANDS if c.name == "formula")
    model = object()
    assert effective_slash_run(compact, model) is SlashRun.COMPACT
    assert effective_slash_run(summarize, model) is SlashRun.ANSWER
    assert effective_slash_run(proofread, model) is SlashRun.REWRITE
    assert effective_slash_run(formula, model) is SlashRun.FORMULA


def test_slash_continues_send_proofread_without_host_is_false():
    proofread = next(c for c in SLASH_COMMANDS if c.name == "proofread")
    host = SimpleNamespace(
        slash_document_model=lambda: object(),
        sidebar_state=SimpleNamespace(send=SimpleNamespace(is_busy=False)),
    )
    assert slash_continues_send(proofread, host) is False


def test_plan_slash_matches_effective_slash_run():
    summarize = next(c for c in SLASH_COMMANDS if c.name == "summarize")
    model = object()
    host = SimpleNamespace(slash_document_model=lambda: model)
    assert plan_slash(summarize, host) == effective_slash_run(summarize, model)


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
                assert run_slash_command(_cmd("translate"), host, "French") is SlashResult.CONTINUE_SEND
    host.slash_run_rewrite.assert_not_called()


def test_slash_compact_returns_continue_for_send():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        slash_document_model=lambda: None,
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru") as record_lru:
        assert run_slash_command(_cmd("compact"), host, "budget") is SlashResult.CONTINUE_SEND
    record_lru.assert_not_called()
    host.hide_popup.assert_not_called()


def test_slash_compact_busy_refuses():
    listener = MagicMock()
    listener.sidebar_state = SimpleNamespace(tool_loop=object())
    listener.append_output = MagicMock()
    listener.ctx = object()
    listener.session = None
    listener._send_cancellation = None
    listener._set_status = MagicMock()

    run_slash_compact(listener, "focus")
    assert "Wait for the current reply" in listener.append_output.call_args[0][0]


def test_slash_compact_sync_passes_tool_schema_list():
    from plugin.framework.send_cancellation import SendCancellation

    cancel = SendCancellation()
    session = MagicMock()
    session.messages = [{"role": "user", "content": "hi"}]
    client = MagicMock()
    schema_list = [{"name": "read_cell_range"}]
    with patch("plugin.framework.config.get_config", return_value=True):
        with patch("plugin.chatbot.compaction.compact_session") as compact_mod:
            compact_mod.return_value = MagicMock(compacted=True, reason="ok")
            with patch("plugin.framework.client.model_fetcher.resolve_context_window", return_value=4096):
                with patch("plugin.framework.llm_lanes.llm_request_lane"):
                    from plugin.chatbot.slash_commands import run_slash_compact_sync

                    run_slash_compact_sync(
                        focus="",
                        session=session,
                        cancel=cancel,
                        client=client,
                        tools=schema_list,
                    )
    assert compact_mod.call_args.kwargs.get("cancel") is cancel
    assert compact_mod.call_args.kwargs.get("tools") == schema_list


def test_slash_compact_sync_passes_cancel():
    from plugin.framework.send_cancellation import SendCancellation

    cancel = SendCancellation()
    session = MagicMock()
    session.messages = [{"role": "user", "content": "hi"}]
    client = MagicMock()
    with patch("plugin.framework.config.get_config", return_value=True):
        with patch("plugin.chatbot.compaction.compact_session") as compact_mod:
            compact_mod.return_value = MagicMock(compacted=True, reason="ok")
            with patch("plugin.framework.client.model_fetcher.resolve_context_window", return_value=4096):
                with patch("plugin.framework.llm_lanes.llm_request_lane"):
                    from plugin.chatbot.slash_commands import run_slash_compact_sync

                    run_slash_compact_sync(
                        focus="",
                        session=session,
                        cancel=cancel,
                        client=client,
                        tools=[],
                    )
    assert compact_mod.call_args.kwargs.get("cancel") is cancel


def test_run_slash_compact_resolves_document_on_main_thread_only():
    """Regression: /compact must not call UNO from run_blocking_in_thread's worker."""
    from plugin.framework.send_cancellation import SendCancellation

    main = threading.main_thread()
    model_threads: list[threading.Thread] = []

    def slash_document_model() -> object:
        model_threads.append(threading.current_thread())
        if threading.current_thread() is not main:
            raise AssertionError("slash_document_model must run on the main thread")
        return object()

    cancel = SendCancellation()
    listener = MagicMock()
    listener.sidebar_state = SimpleNamespace(tool_loop=None)
    listener.session = MagicMock()
    listener.ctx = object()
    listener._send_cancellation = cancel
    listener._set_status = MagicMock()
    listener.append_output = MagicMock()
    listener.cached_doc_type = "writer"
    listener.cached_uno_services = None
    listener.slash_document_model = slash_document_model

    schema_list = [{"name": "read_cell_range"}]
    registry = MagicMock()
    registry.get_schemas.return_value = schema_list

    with patch("plugin.framework.uno_context.get_toolkit", side_effect=RuntimeError("no uno")):
        with patch("plugin.chatbot.selection.create_validated_client", return_value=MagicMock()):
            with patch("plugin.framework.config.get_config", return_value=True):
                with patch("plugin.chatbot.compaction.compact_session") as compact_mod:
                    compact_mod.return_value = MagicMock(compacted=True, reason="ok")
                    with patch("plugin.app.get_tools", return_value=registry):
                        with patch(
                            "plugin.framework.client.model_fetcher.resolve_context_window",
                            return_value=4096,
                        ):
                            with patch("plugin.framework.llm_lanes.llm_request_lane"):
                                run_slash_compact(listener, "focus", cancel=cancel)

    assert model_threads == [main]
    assert compact_mod.call_args.kwargs.get("tools") == schema_list
    listener.append_output.assert_called_once()


def test_run_slash_compact_invalid_config_msgbox_on_main_thread():
    """Config validation (msgbox) must run on the UI thread, not the blocking worker."""
    from plugin.framework.send_cancellation import SendCancellation

    main = threading.main_thread()
    msgbox_threads: list[threading.Thread] = []

    def record_msgbox(ctx: object, title: str, message: str) -> None:
        msgbox_threads.append(threading.current_thread())
        if threading.current_thread() is not main:
            raise AssertionError("msgbox must run on the main thread")

    cancel = SendCancellation()
    listener = MagicMock()
    listener.sidebar_state = SimpleNamespace(tool_loop=None)
    listener.session = MagicMock()
    listener.ctx = object()
    listener._send_cancellation = cancel
    listener._set_status = MagicMock()
    listener.append_output = MagicMock()
    listener.cached_doc_type = "writer"
    listener.cached_uno_services = None
    listener.slash_document_model = MagicMock(return_value=object())

    def fake_create(ctx: object, title: str) -> None:
        record_msgbox(ctx, title, "bad config")
        return None

    with patch("plugin.framework.uno_context.get_toolkit", side_effect=RuntimeError("no uno")):
        with patch("plugin.chatbot.selection.create_validated_client", side_effect=fake_create):
            with patch("plugin.chatbot.compaction.compact_session") as compact_mod:
                run_slash_compact(listener, "focus", cancel=cancel)

    assert msgbox_threads == [main]
    compact_mod.assert_not_called()
    listener.append_output.assert_called_once()
    assert "Configure an API key" in listener.append_output.call_args[0][0]


def test_run_slash_compact_stop_cancels_after_ui_thread_client():
    """Stop still cancels /compact when LlmClient is built on the UI thread in send scope."""
    from plugin.chatbot.slash_commands import prepare_slash_compact_on_ui_thread, run_slash_compact_sync
    from plugin.framework.client.llm_client import LlmClient
    from plugin.framework.send_cancellation import SendCancellation, agent_session

    cancel = SendCancellation()
    host = SimpleNamespace(
        slash_document_model=MagicMock(return_value=object()),
        cached_doc_type="writer",
        cached_uno_services=None,
    )
    ctx = object()
    session = MagicMock()
    session.messages = [{"role": "user", "content": "hi"}]
    api_config = {"endpoint": "http://127.0.0.1:5000", "model": "test-model"}
    stopped_clients: list[LlmClient] = []
    original_stop = LlmClient.stop

    def track_stop(self: LlmClient) -> None:
        stopped_clients.append(self)
        original_stop(self)

    with patch("plugin.chatbot.selection.get_api_config", return_value=api_config):
        with patch("plugin.chatbot.selection.validate_api_config", return_value=(True, "")):
            with patch("plugin.app.get_tools", return_value=MagicMock(get_schemas=MagicMock(return_value=[]))):
                with patch.object(LlmClient, "stop", track_stop):
                    with agent_session(cancel):
                        client, tools = prepare_slash_compact_on_ui_thread(host, ctx, session)
                        assert client is not None
                        cancel.cancel()
                        with patch("plugin.framework.config.get_config", return_value=True):
                            with patch(
                                "plugin.framework.client.model_fetcher.resolve_context_window",
                                return_value=4096,
                            ):
                                with patch("plugin.framework.llm_lanes.llm_request_lane"):
                                    line = run_slash_compact_sync(
                                        focus="",
                                        session=session,
                                        cancel=cancel,
                                        client=client,
                                        tools=tools,
                                    )

    assert stopped_clients
    assert "cancelled" in line.lower()


def test_run_slash_compact_sync_without_api_key_still_compacts():
    from plugin.chatbot.slash_commands import run_slash_compact_sync
    from plugin.framework.send_cancellation import SendCancellation

    client = SimpleNamespace(config={"api_key": "", "model": "llama3", "endpoint": "http://127.0.0.1:11434/v1"})
    compact_result = SimpleNamespace(compacted=True, reason="ok")
    with patch("plugin.framework.config.get_config", return_value=True):
        with patch("plugin.framework.client.model_fetcher.resolve_context_window", return_value=8192):
            with patch("plugin.chatbot.compaction.compact_session", return_value=compact_result):
                line = run_slash_compact_sync(
                    focus="",
                    session=SimpleNamespace(messages=[{"role": "user", "content": "hi"}]),
                    cancel=SendCancellation(),
                    client=client,
                    tools=[],
                )
    assert "Configure an API key" not in line
    assert "compacted" in line.lower()


def test_run_slash_compact_sync_empty_api_key():
    from plugin.chatbot.slash_commands import run_slash_compact_sync
    from plugin.framework.send_cancellation import SendCancellation

    client = SimpleNamespace(
        config={"api_key": "", "model": "gpt-4o", "endpoint": "https://api.openai.com/v1"},
    )
    with patch("plugin.framework.config.get_config", return_value=True):
        with patch("plugin.chatbot.compaction.compact_session") as compact_mod:
            line = run_slash_compact_sync(
                focus="",
                session=SimpleNamespace(messages=[{"role": "user", "content": "hi"}]),
                cancel=SendCancellation(),
                client=client,
                tools=[],
            )
    compact_mod.assert_not_called()
    assert "Configure an API key" in line
    assert "not available for this model" not in line


def test_run_slash_compact_sync_invalid_api_key_401():
    import urllib.error

    from plugin.chatbot.slash_commands import run_slash_compact_sync
    from plugin.framework.send_cancellation import SendCancellation

    client = SimpleNamespace(
        config={"api_key": "junk", "model": "gpt-4o", "endpoint": "https://api.openai.com/v1"},
    )
    with patch("plugin.framework.config.get_config", return_value=True):
        with patch(
            "plugin.framework.client.model_fetcher.resolve_context_window",
            return_value=8192,
        ):
            with patch(
                "plugin.chatbot.compaction.compact_session",
                side_effect=urllib.error.HTTPError(
                    url="https://api.openai.com/v1/chat/completions",
                    code=401,
                    msg="Unauthorized",
                    hdrs=None,
                    fp=None,
                ),
            ):
                line = run_slash_compact_sync(
                    focus="",
                    session=SimpleNamespace(messages=[{"role": "user", "content": "hi"}]),
                    cancel=SendCancellation(),
                    client=client,
                    tools=[],
                )
    assert "Invalid API Key" in line
    assert line.strip() != "Compaction failed."


def test_compaction_auth_line_client_and_exception():
    import urllib.error

    from plugin.chatbot.compaction import auth_line_for_client, auth_line_for_error
    from plugin.framework.client.auth import AuthError
    from plugin.framework.client.llm_client import LlmClient

    empty_key_client = LlmClient(
        {"api_key": "", "model": "gpt-4o", "endpoint": "https://api.openai.com/v1"},
        ctx=None,
    )
    line = auth_line_for_client(empty_key_client)
    assert line is not None
    assert "Configure an API key" in line

    other_auth = auth_line_for_error(AuthError("bad key", code="invalid_key"))
    assert other_auth is not None
    assert "Configure an API key" not in other_auth

    http401 = auth_line_for_error(
        urllib.error.HTTPError(
            url="https://api.openai.com/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
    )
    assert http401 is not None

    http500 = auth_line_for_error(
        urllib.error.HTTPError(
            url="https://api.openai.com/v1/chat/completions",
            code=500,
            msg="Server Error",
            hdrs=None,
            fp=None,
        )
    )
    assert http500 is None

    assert auth_line_for_error(RuntimeError("boom")) is None


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
        assert run_slash_command(_cmd("remember"), host, "British spelling") is SlashResult.HANDLED
    saved = json.loads(user_md.read_text(encoding="utf-8"))
    assert any("British spelling" in str(v) for v in saved.values())
    assert "Saved to memory" in host.append_output.call_args[0][0]


def test_run_remember_migrates_prose_user_md(tmp_path, monkeypatch):
    from plugin.chatbot.memory import MemoryStore, _LEGACY_PROSE_KEY

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    user_md = mem_dir / "USER.md"
    user_md.write_text("# Test User Profile\n", encoding="utf-8")

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
        assert run_slash_command(_cmd("remember"), host, "Denali") is SlashResult.HANDLED
    saved = json.loads(user_md.read_text(encoding="utf-8"))
    assert saved[_LEGACY_PROSE_KEY] == "# Test User Profile"
    assert any("Denali" in str(v) for v in saved.values())
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
    from plugin.chatbot.slash_commands import parse_slash_text

    assert parse_slash_text("/mock-bravo") is None


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


def test_format_instruction_appends_optional_arg_cleanly():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, _format_instruction

    shorten = next(c for c in SLASH_COMMANDS if c.name == "shorten")
    formatted = _format_instruction(shorten, "to about 10 words")
    assert "preserving the meaning." in formatted
    assert formatted.endswith("Additional instruction: to about 10 words")


def test_missing_required_arg_prints_usage():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        append_output=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(_cmd("tone"), host, "") is SlashResult.HANDLED
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
            assert run_slash_command(_cmd("tone"), host, from_popup=True) is SlashResult.HANDLED
            set_text.assert_called_once_with(host.query_control, "/tone ")
    host.clear_ask.assert_not_called()
    host.dispatch.assert_called_once()


def test_summarizer_user_body_includes_focus():
    from plugin.chatbot import compaction as compaction_mod

    body = compaction_mod._summarizer_user_body("line", "", focus="budget")
    assert "Keep details about: budget" in body


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


def test_enable_mock_slash_commands_adds_stubs(monkeypatch):
    import plugin.chatbot.slash_commands as slash

    monkeypatch.setattr(slash, "_mock_commands_enabled", False)
    slash.enable_mock_slash_commands()
    names = [c.name for c in slash.registered_slash_commands()]
    assert names[:3] == ["help", "clear", "stop"]
    assert "mock-bravo" in names
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        host = MagicMock()
        assert run_slash_command(_cmd("mock-bravo"), host) is SlashResult.HANDLED
    slash._mock_commands_enabled = False
    assert "mock-bravo" not in [c.name for c in slash.registered_slash_commands()]


def test_run_undo_delegates_to_agent_undo():
    host = MagicMock()
    host.slash_agent_edit_in_flight.return_value = False
    model = object()
    host.slash_document_model.return_value = model
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        with patch("plugin.doc.agent_undo.undo_last_agent_edit", return_value="Undid the last agent edit (2 steps)") as undo_fn:
            assert run_slash_command(_cmd("undo"), host) is SlashResult.HANDLED
    undo_fn.assert_called_once_with(model)
    host.clear_ask.assert_called_once()
    assert "Undid" in host.append_output.call_args[0][0]


def test_run_undo_refuses_while_in_flight():
    host = MagicMock()
    host.slash_agent_edit_in_flight.return_value = True
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(_cmd("undo"), host) is SlashResult.HANDLED
    host.slash_document_model.assert_not_called()


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
            assert run_slash_command(_cmd("undo"), host, from_popup=True) is SlashResult.HANDLED
    host.clear_ask.assert_called_once()
    host.append_output.assert_called_once()


def test_slash_ask_past_command_name_with_arg_or_trailing_space():
    from plugin.chatbot.slash_commands import slash_ask_past_command_name

    assert slash_ask_past_command_name("/tone very formal") is True
    assert slash_ask_past_command_name("/tone ") is True
    assert slash_ask_past_command_name("/to") is False
    assert slash_ask_past_command_name("/tone") is False


def test_format_slash_popup_item_omits_usage_hint():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, format_slash_item, format_slash_popup_item

    tone = next(c for c in SLASH_COMMANDS if c.name == "tone")
    full = format_slash_item(tone)
    assert "tone" in full.lower()
    popup_line = format_slash_popup_item(tone)
    assert popup_line.startswith("/tone —")
    assert "<" not in popup_line
    assert len(popup_line) <= 80


def test_slash_answer_send_keeps_typed_transcript_line():
    from plugin.chatbot.rich_text_paste import session_history_items

    session = SimpleNamespace(
        messages=[
            {
                "role": "user",
                "content": "Summarize the selected text, or the whole document if nothing is selected. in 400 words",
                "_transcript_text": "/summarize in 400 words",
            }
        ]
    )
    items = session_history_items(session)
    assert items == [("user", "/summarize in 400 words")]


def test_run_stop_from_popup_clears_partial_draft():
    host = SimpleNamespace(
        hide_popup=MagicMock(),
        clear_ask=MagicMock(),
        stop_in_flight=MagicMock(),
    )
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(_cmd("stop"), host, from_popup=True) is SlashResult.HANDLED
    host.clear_ask.assert_called_once()


def test_format_instruction_avoids_doubled_period_after_arg():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, _format_instruction

    translate = next(c for c in SLASH_COMMANDS if c.name == "translate")
    text = _format_instruction(translate, "Spanish: Good morning, friends.")
    assert "friends.." not in text
    assert "Spanish: Good morning, friends." in text


def test_format_instruction_keeps_trailing_ellipsis_when_template_has_period():
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, _format_instruction

    translate = next(c for c in SLASH_COMMANDS if c.name == "translate")
    text = _format_instruction(translate, "Spanish: Hello...")
    assert "Spanish: Hello..." in text
    assert "Spanish: Hello...." not in text


def test_format_instruction_avoids_doubled_question_mark():
    from plugin.chatbot.slash_commands import SlashCommand, SlashRun, _format_instruction

    cmd = SlashCommand("test", "x", run=SlashRun.ANSWER, instruction="Answer: {arg}?")
    text = _format_instruction(cmd, "Why?")
    assert "Why??" not in text
    assert "Why?" in text


def test_slash_local_messages_omitted_from_llm_view():
    from plugin.chatbot.compaction import messages_for_llm
    from plugin.chatbot.rich_text_paste import SLASH_LOCAL

    session = SimpleNamespace(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "help text", SLASH_LOCAL: True},
        ]
    )
    view = messages_for_llm(session)
    assert all(not m.get(SLASH_LOCAL) for m in view)
    assert len(view) == 2


def test_slash_local_survives_next_user_message_in_session():
    from plugin.chatbot.rich_text_paste import SLASH_LOCAL, append_slash_local_message

    session = SimpleNamespace(messages=[{"role": "system", "content": "sys"}])
    assert append_slash_local_message(session, "\nSaved to memory: x\n")
    session.messages.append({"role": "user", "content": "next"})
    assert any(m.get(SLASH_LOCAL) for m in session.messages)


def test_summary_lines_omit_slash_local():
    from plugin.chatbot.compaction import _summary_lines
    from plugin.chatbot.rich_text_paste import SLASH_LOCAL

    lines = _summary_lines(
        [
            {"role": "assistant", "content": "Slash commands: /help", SLASH_LOCAL: True},
            {"role": "user", "content": "hello"},
        ]
    )
    assert lines == ["USER: hello"]
    assert not any("Slash commands" in line for line in lines)


def test_popup_runs_now_names():
    immediate = {
        "help",
        "clear",
        "stop",
        "undo",
        "mock-alpha",
        "mock-bravo",
        "mock-charlie",
        "apply",
        "web",
        "model",
    }
    for cmd in registered_slash_commands():
        if cmd.name in immediate:
            assert slash_popup_runs_now(cmd), cmd.name
        else:
            assert not slash_popup_runs_now(cmd), cmd.name


def test_slash_registry_handler_invariant():
    from plugin.chatbot.slash_commands import _SEND_TURN_RUNS

    for cmd in SLASH_COMMANDS + MOCK_SLASH_COMMANDS:
        preset_kind = cmd.run in _SEND_TURN_RUNS or cmd.run is SlashRun.REWRITE
        assert preset_kind != (cmd.handler is not None), cmd.name
