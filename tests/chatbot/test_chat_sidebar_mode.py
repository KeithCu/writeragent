# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.chatbot.chat_sidebar_mode import (
    CHAT_MODE_BRAINSTORMING,
    CHAT_MODE_CHAT,
    CHAT_MODE_DEEP_RESEARCH,
    CHAT_MODE_IMAGE,
    CHAT_MODE_LIBRARIAN,
    CHAT_MODE_WEB_RESEARCH,
    CHAT_MODE_WRITING_PLAN,
    clear_brainstorming_session,
    clear_librarian_session,
    clear_writing_plan_session,
    get_mode_labels,
    mode_from_label,
    mode_from_selector,
    populate_mode_selector,
    set_selector_mode,
)


def test_mode_labels_include_brainstorming_when_writer():
    labels = get_mode_labels(include_brainstorming=True, include_writing_plan=True)
    assert len(labels) == 7
    assert mode_from_label(labels[0], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_CHAT
    assert labels[1] == "Image"
    assert mode_from_label(labels[1], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_IMAGE
    assert mode_from_label(labels[2], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_WEB_RESEARCH
    assert mode_from_label(labels[3], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_DEEP_RESEARCH
    assert mode_from_label(labels[4], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_BRAINSTORMING
    assert mode_from_label(labels[5], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_WRITING_PLAN
    assert mode_from_label(labels[6], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_LIBRARIAN


def test_mode_labels_omit_brainstorming_for_calc():
    labels = get_mode_labels(include_brainstorming=False, include_writing_plan=True, include_ppt_master=False)
    assert len(labels) == 6
    assert mode_from_label(labels[4], include_brainstorming=False, include_writing_plan=True) == CHAT_MODE_WRITING_PLAN
    assert mode_from_label(labels[-1], include_brainstorming=False, include_writing_plan=True) == CHAT_MODE_LIBRARIAN


def test_mode_labels_ppt_master_for_impress():
    from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_PPT_MASTER, sidebar_mode_flags_for_doc_type

    flags = sidebar_mode_flags_for_doc_type("impress")
    labels = get_mode_labels(**flags.__dict__)
    assert len(labels) == 6
    assert mode_from_label(labels[4], **flags.__dict__) == CHAT_MODE_PPT_MASTER
    assert mode_from_label(labels[-1], **flags.__dict__) == CHAT_MODE_LIBRARIAN


def test_mode_from_selector_reads_combobox_text():
    ctrl = MagicMock()
    labels = get_mode_labels(include_brainstorming=True)
    ctrl.getSelectedItemPos.return_value = -1
    ctrl.getText.return_value = labels[1]
    assert mode_from_selector(ctrl, include_brainstorming=True) == CHAT_MODE_IMAGE


def test_mode_from_selector_prefers_selected_index_over_stale_text():
    """XDL ComboBox spin=true can keep the default Chat text while index is Librarian."""
    ctrl = MagicMock()
    labels = get_mode_labels(include_brainstorming=True, include_writing_plan=True)
    ctrl.getSelectedItemPos.return_value = len(labels) - 1
    ctrl.getText.return_value = labels[0]
    assert (
        mode_from_selector(ctrl, include_brainstorming=True, include_writing_plan=True)
        == CHAT_MODE_LIBRARIAN
    )


def test_mode_from_selector_oob_index_falls_back_to_text():
    ctrl = MagicMock()
    labels = get_mode_labels(include_brainstorming=True)
    ctrl.getSelectedItemPos.return_value = 99
    ctrl.getText.return_value = labels[1]
    assert mode_from_selector(ctrl, include_brainstorming=True) == CHAT_MODE_IMAGE


def test_set_selector_mode_selects_by_index():
    ctrl = MagicMock()
    labels = get_mode_labels(include_brainstorming=True)
    set_selector_mode(ctrl, CHAT_MODE_WEB_RESEARCH, include_brainstorming=True)
    ctrl.selectItemPos.assert_called_once_with(2, True)
    ctrl.setText.assert_called_once_with(labels[2])


def test_set_selector_mode_sets_text_if_select_item_pos_raises():
    ctrl = MagicMock()
    labels = get_mode_labels(include_brainstorming=True)
    ctrl.selectItemPos.side_effect = Exception("disposed")
    set_selector_mode(ctrl, CHAT_MODE_WEB_RESEARCH, include_brainstorming=True)
    ctrl.setText.assert_called_once_with(labels[2])


def test_populate_mode_selector_sets_string_item_list_on_model():
    ctrl = MagicMock()
    model = MagicMock()
    ctrl.getModel.return_value = model
    ctrl.getItemCount.return_value = 0
    populate_mode_selector(ctrl, include_brainstorming=True)
    labels = tuple(str(x) for x in get_mode_labels(include_brainstorming=True))
    assert model.StringItemList == labels
    ctrl.addItems.assert_called_once_with(labels, 0)


def test_clear_brainstorming_session_resets_flags():
    listener = MagicMock()
    listener._in_brainstorming_mode = True
    listener._brainstorming_topic = "topic"
    clear_brainstorming_session(listener)
    assert listener._in_brainstorming_mode is False
    assert listener._brainstorming_topic == ""


def test_clear_writing_plan_session_resets_flags():
    listener = MagicMock()
    listener._in_writing_plan_mode = True
    listener._writing_plan_topic = "topic"
    clear_writing_plan_session(listener)
    assert listener._in_writing_plan_mode is False
    assert listener._writing_plan_topic == ""


def test_clear_librarian_session_resets_flag_only():
    listener = MagicMock()
    listener._in_librarian_mode = True
    clear_librarian_session(listener)
    assert listener._in_librarian_mode is False


def test_librarian_default_mode_first_then_chat():
    from plugin.chatbot.chat_sidebar_mode import librarian_default_mode, mark_librarian_invoked

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        assert librarian_default_mode() == CHAT_MODE_LIBRARIAN
    with patch("plugin.framework.config.get_config_bool_safe", return_value=True):
        assert librarian_default_mode() == CHAT_MODE_CHAT
    with patch("plugin.framework.config.set_config") as mock_set:
        mark_librarian_invoked()
        mock_set.assert_called_once_with("chatbot.librarian_invoked", True)


def test_librarian_default_mode_existing_profile_skips_and_sets_flag():
    from plugin.chatbot.chat_sidebar_mode import librarian_default_mode

    with patch("plugin.framework.config.get_config_bool_safe", return_value=False), patch(
        "plugin.chatbot.memory.user_profile_exists", return_value=True
    ), patch("plugin.chatbot.chat_sidebar_mode.mark_librarian_invoked") as mock_mark:
        assert librarian_default_mode(ctx=object()) == CHAT_MODE_CHAT
        mock_mark.assert_called_once()


def test_librarian_history_session_id_is_global():
    from plugin.chatbot.chat_sidebar_mode import LIBRARIAN_HISTORY_SESSION_ID

    assert LIBRARIAN_HISTORY_SESSION_ID == "writeragent_librarian"
    assert "_web" not in LIBRARIAN_HISTORY_SESSION_ID


def test_set_selector_mode_librarian_is_last_index():
    ctrl = MagicMock()
    labels = get_mode_labels(include_brainstorming=True, include_writing_plan=True)
    set_selector_mode(ctrl, CHAT_MODE_LIBRARIAN, include_brainstorming=True, include_writing_plan=True)
    ctrl.selectItemPos.assert_called_once_with(len(labels) - 1, True)


def test_mode_from_label_dropped_from_check_all_fqns():
    """Deep check-all run 32840960268: Prev 10:13 on gettext labels."""
    from pathlib import Path

    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(Path("plugin/chatbot/chat_sidebar_mode.py"), require_deal=True)
    assert not any(f.endswith(".mode_from_label") for f in fqns)
    assert any(f.endswith(".get_mode_labels") for f in fqns)
