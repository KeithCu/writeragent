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
    CHAT_MODE_PPT_MASTER,
    CHAT_MODE_WEB_RESEARCH,
    CHAT_MODE_WRITING_PLAN,
    LIBRARIAN_HISTORY_SESSION_ID,
    SidebarModeFlags,
    SidebarModeController,
    _modes_for,
    clear_brainstorming_session,
    clear_librarian_session,
    clear_writing_plan_session,
    get_mode_labels,
    librarian_default_mode,
    mark_librarian_invoked,
    mode_from_label,
    mode_from_selector_with_flags,
    populate_mode_selector_with_flags,
    set_selector_mode_with_flags,
    sidebar_mode_flags_for_doc_type,
)


def test_mode_labels_match_modes_for_length_and_order():
    for flags in (
        SidebarModeFlags(include_brainstorming=False, include_writing_plan=True, include_ppt_master=False),
        SidebarModeFlags(include_brainstorming=True, include_writing_plan=True, include_ppt_master=False),
        SidebarModeFlags(include_brainstorming=False, include_writing_plan=False, include_ppt_master=True),
        SidebarModeFlags(include_brainstorming=True, include_writing_plan=False, include_ppt_master=False),
    ):
        modes = _modes_for(flags)
        labels = get_mode_labels(flags)
        assert len(labels) == len(modes)
        for mode, label in zip(modes, labels):
            assert mode_from_label(label, flags) == mode


def test_mode_labels_include_brainstorming_when_writer():
    flags = SidebarModeFlags(include_brainstorming=True, include_writing_plan=True)
    labels = get_mode_labels(flags)
    assert len(labels) == 7
    assert mode_from_label(labels[0], flags) == CHAT_MODE_CHAT
    assert labels[1] == "Image"
    assert mode_from_label(labels[1], flags) == CHAT_MODE_IMAGE
    assert mode_from_label(labels[2], flags) == CHAT_MODE_WEB_RESEARCH
    assert mode_from_label(labels[3], flags) == CHAT_MODE_DEEP_RESEARCH
    assert mode_from_label(labels[4], flags) == CHAT_MODE_BRAINSTORMING
    assert mode_from_label(labels[5], flags) == CHAT_MODE_WRITING_PLAN
    assert mode_from_label(labels[6], flags) == CHAT_MODE_LIBRARIAN


def test_mode_labels_omit_brainstorming_for_calc():
    flags = SidebarModeFlags(include_brainstorming=False, include_writing_plan=True, include_ppt_master=False)
    labels = get_mode_labels(flags)
    assert len(labels) == 6
    assert mode_from_label(labels[4], flags) == CHAT_MODE_WRITING_PLAN
    assert mode_from_label(labels[-1], flags) == CHAT_MODE_LIBRARIAN


def test_calc_sidebar_omits_writing_plan():
    flags = sidebar_mode_flags_for_doc_type("calc")
    assert flags.include_writing_plan is False
    assert "Writing Plan" not in get_mode_labels(flags)


def test_mode_labels_ppt_master_for_impress():
    flags = sidebar_mode_flags_for_doc_type("impress")
    labels = get_mode_labels(flags)
    assert len(labels) == 6
    assert mode_from_label(labels[4], flags) == CHAT_MODE_PPT_MASTER
    assert mode_from_label(labels[-1], flags) == CHAT_MODE_LIBRARIAN


def test_mode_from_selector_reads_combobox_text():
    ctrl = MagicMock()
    flags = SidebarModeFlags(include_brainstorming=True)
    labels = get_mode_labels(flags)
    ctrl.getSelectedItemPos.return_value = -1
    ctrl.getText.return_value = labels[1]
    assert mode_from_selector_with_flags(ctrl, flags) == CHAT_MODE_IMAGE


def test_mode_from_selector_prefers_selected_index_over_stale_text():
    """XDL ComboBox spin=true can keep the default Chat text while index is Librarian."""
    ctrl = MagicMock()
    flags = SidebarModeFlags(include_brainstorming=True, include_writing_plan=True)
    labels = get_mode_labels(flags)
    ctrl.getSelectedItemPos.return_value = len(labels) - 1
    ctrl.getText.return_value = labels[0]
    assert (
        mode_from_selector_with_flags(ctrl, flags)
        == CHAT_MODE_LIBRARIAN
    )


def test_mode_from_selector_oob_index_falls_back_to_text():
    ctrl = MagicMock()
    flags = SidebarModeFlags(include_brainstorming=True)
    labels = get_mode_labels(flags)
    ctrl.getSelectedItemPos.return_value = 99
    ctrl.getText.return_value = labels[1]
    assert mode_from_selector_with_flags(ctrl, flags) == CHAT_MODE_IMAGE


def test_set_selector_mode_selects_by_index():
    ctrl = MagicMock()
    flags = SidebarModeFlags(include_brainstorming=True)
    labels = get_mode_labels(flags)
    set_selector_mode_with_flags(ctrl, CHAT_MODE_WEB_RESEARCH, flags)
    ctrl.selectItemPos.assert_called_once_with(2, True)
    ctrl.setText.assert_called_once_with(labels[2])


def test_populate_mode_selector_sets_string_item_list_on_model():
    ctrl = MagicMock()
    model = MagicMock()
    ctrl.getModel.return_value = model
    ctrl.getItemCount.return_value = 0
    flags = SidebarModeFlags(include_brainstorming=True)
    populate_mode_selector_with_flags(ctrl, flags)
    labels = tuple(str(x) for x in get_mode_labels(flags))
    assert model.StringItemList == labels
    ctrl.addItems.assert_called_once_with(labels, 0)


def test_clear_brainstorming_session_resets_flags():
    listener = MagicMock()
    listener._active_mode = CHAT_MODE_BRAINSTORMING
    listener._mode_topic = "topic"
    clear_brainstorming_session(listener)
    assert listener._active_mode == ""
    assert listener._mode_topic == ""


def test_clear_brainstorming_session_skips_when_in_writing_plan():
    listener = MagicMock()
    listener._active_mode = CHAT_MODE_WRITING_PLAN
    listener._mode_topic = "plan topic"
    clear_brainstorming_session(listener)
    assert listener._active_mode == CHAT_MODE_WRITING_PLAN
    assert listener._mode_topic == "plan topic"


def test_clear_writing_plan_session_resets_flags():
    listener = MagicMock()
    listener._active_mode = CHAT_MODE_WRITING_PLAN
    listener._mode_topic = "topic"
    clear_writing_plan_session(listener)
    assert listener._active_mode == ""
    assert listener._mode_topic == ""


def test_clear_ppt_master_session_resets_flags():
    from plugin.chatbot.chat_sidebar_mode import clear_ppt_master_session

    listener = MagicMock()
    listener._active_mode = CHAT_MODE_PPT_MASTER
    listener._mode_topic = "topic"
    clear_ppt_master_session(listener)
    assert listener._active_mode == ""
    assert listener._mode_topic == ""


def test_clear_librarian_session_resets_flag_only():
    listener = MagicMock()
    listener._active_mode = CHAT_MODE_LIBRARIAN
    listener._mode_topic = "preserved topic"
    clear_librarian_session(listener)
    assert listener._active_mode == ""
    assert listener._mode_topic == "preserved topic"


def test_librarian_default_mode_first_then_chat():
    with patch("plugin.framework.config.get_config_bool_safe", return_value=False):
        assert librarian_default_mode() == CHAT_MODE_LIBRARIAN
    with patch("plugin.framework.config.get_config_bool_safe", return_value=True):
        assert librarian_default_mode() == CHAT_MODE_CHAT
    with patch("plugin.framework.config.set_config") as mock_set:
        mark_librarian_invoked()
        mock_set.assert_called_once_with("chatbot.librarian_invoked", True)


def test_librarian_default_mode_existing_profile_skips_and_sets_flag():
    with patch("plugin.framework.config.get_config_bool_safe", return_value=False), patch(
        "plugin.chatbot.memory.user_profile_exists", return_value=True
    ), patch("plugin.chatbot.chat_sidebar_mode.mark_librarian_invoked") as mock_mark:
        assert librarian_default_mode(ctx=object()) == CHAT_MODE_CHAT
        mock_mark.assert_called_once()


def test_librarian_history_session_id_is_global():
    assert LIBRARIAN_HISTORY_SESSION_ID == "writeragent_librarian"
    assert "_web" not in LIBRARIAN_HISTORY_SESSION_ID


def test_set_selector_mode_librarian_is_last_index():
    ctrl = MagicMock()
    flags = SidebarModeFlags(include_brainstorming=True, include_writing_plan=True)
    labels = get_mode_labels(flags)
    set_selector_mode_with_flags(ctrl, CHAT_MODE_LIBRARIAN, flags)
    ctrl.selectItemPos.assert_called_once_with(len(labels) - 1, True)


def test_mode_from_label_dropped_from_check_all_fqns():
    """mode_from_label is omitted from cover_fqns_for_module."""
    from pathlib import Path

    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(Path("plugin/chatbot/chat_sidebar_mode.py"), require_deal=True)
    assert not any(f.endswith(".mode_from_label") for f in fqns)
    assert any(f.endswith(".get_mode_labels") for f in fqns)


def test_sidebar_mode_controller_brainstorming_finished():
    panel = MagicMock()
    panel.sidebar_mode_flags = SidebarModeFlags(include_brainstorming=True, include_writing_plan=True)
    panel.chat_mode_selector = MagicMock()
    panel._apply_sidebar_mode_fn = MagicMock()
    panel._mode_topic = "Kitchen remodel"
    panel._active_mode = CHAT_MODE_BRAINSTORMING

    controller = SidebarModeController(panel)
    with patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags") as mock_set:
        controller.on_brainstorming_session_finished(spec_saved=True)
    assert panel._mode_topic == "Implement the saved spec: Kitchen remodel"
    assert panel._active_mode == CHAT_MODE_WRITING_PLAN
    assert mock_set.call_args[0][1] == CHAT_MODE_WRITING_PLAN
    panel._apply_sidebar_mode_fn.assert_called_once_with(CHAT_MODE_WRITING_PLAN)

    # Without spec saved
    panel._mode_topic = "Kitchen remodel"
    panel._active_mode = CHAT_MODE_BRAINSTORMING
    panel._apply_sidebar_mode_fn.reset_mock()
    with patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags"):
        controller.on_brainstorming_session_finished(spec_saved=False)
    panel._apply_sidebar_mode_fn.assert_called_once_with(CHAT_MODE_CHAT)


def test_sidebar_mode_controller_other_modes_finished():
    for meth_name in ("on_librarian_session_finished", "on_writing_plan_session_finished", "on_ppt_master_session_finished"):
        panel = MagicMock()
        panel.sidebar_mode_flags = SidebarModeFlags(include_brainstorming=True, include_writing_plan=True, include_ppt_master=True)
        panel.chat_mode_selector = MagicMock()
        panel._apply_sidebar_mode_fn = MagicMock()
        panel._active_mode = "something"

        controller = SidebarModeController(panel)
        with patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags"):
            getattr(controller, meth_name)()
        panel._apply_sidebar_mode_fn.assert_called_once_with(CHAT_MODE_CHAT)


def test_greeting_for_sidebar_mode():
    from plugin.chatbot.chat_sidebar_mode import (
        CHAT_MODE_BRAINSTORMING,
        CHAT_MODE_CHAT,
        CHAT_MODE_DEEP_RESEARCH,
        CHAT_MODE_LIBRARIAN,
        CHAT_MODE_PPT_MASTER,
        CHAT_MODE_WEB_RESEARCH,
        CHAT_MODE_WRITING_PLAN,
        greeting_for_sidebar_mode,
    )

    fake_model = MagicMock()

    assert "web research" in greeting_for_sidebar_mode(CHAT_MODE_WEB_RESEARCH, fake_model).lower()
    assert "deep research" in greeting_for_sidebar_mode(CHAT_MODE_DEEP_RESEARCH, fake_model).lower()
    assert "design your idea" in greeting_for_sidebar_mode(CHAT_MODE_BRAINSTORMING, fake_model).lower()
    assert "writing plan" in greeting_for_sidebar_mode(CHAT_MODE_WRITING_PLAN, fake_model).lower()
    assert "ppt-master" in greeting_for_sidebar_mode(CHAT_MODE_PPT_MASTER, fake_model).lower()
    assert "librarian" in greeting_for_sidebar_mode(CHAT_MODE_LIBRARIAN, fake_model).lower()

    with patch("plugin.framework.prompts.get_greeting_for_document", return_value="Default doc greeting") as mock_fallback:
        res = greeting_for_sidebar_mode(CHAT_MODE_CHAT, fake_model)
        assert res == "Default doc greeting"
        mock_fallback.assert_called_once_with(fake_model)

