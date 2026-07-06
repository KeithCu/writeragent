# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.chatbot.chat_sidebar_mode import (
    CHAT_MODE_BRAINSTORMING,
    CHAT_MODE_CHAT,
    CHAT_MODE_DEEP_RESEARCH,
    CHAT_MODE_IMAGE,
    CHAT_MODE_WEB_RESEARCH,
    CHAT_MODE_WRITING_PLAN,
    clear_brainstorming_session,
    clear_writing_plan_session,
    get_mode_labels,
    mode_from_label,
    mode_from_selector,
    populate_mode_selector,
    set_selector_mode,
)


def test_mode_labels_include_brainstorming_when_writer():
    labels = get_mode_labels(include_brainstorming=True, include_writing_plan=True)
    assert len(labels) == 6
    assert mode_from_label(labels[0], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_CHAT
    assert mode_from_label(labels[1], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_IMAGE
    assert mode_from_label(labels[2], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_WEB_RESEARCH
    assert mode_from_label(labels[3], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_DEEP_RESEARCH
    assert mode_from_label(labels[4], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_BRAINSTORMING
    assert mode_from_label(labels[5], include_brainstorming=True, include_writing_plan=True) == CHAT_MODE_WRITING_PLAN


def test_mode_labels_omit_brainstorming_for_calc():
    labels = get_mode_labels(include_brainstorming=False, include_writing_plan=True, include_ppt_master=False)
    assert len(labels) == 5
    assert mode_from_label(labels[4], include_brainstorming=False, include_writing_plan=True) == CHAT_MODE_WRITING_PLAN


def test_mode_labels_ppt_master_for_impress():
    from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_PPT_MASTER, sidebar_mode_flags_for_doc_type

    flags = sidebar_mode_flags_for_doc_type("impress")
    labels = get_mode_labels(**flags.__dict__)
    assert len(labels) == 5
    assert mode_from_label(labels[4], **flags.__dict__) == CHAT_MODE_PPT_MASTER


def test_mode_from_selector_reads_combobox_text():
    ctrl = MagicMock()
    labels = get_mode_labels(include_brainstorming=True)
    ctrl.getText.return_value = labels[1]
    assert mode_from_selector(ctrl, include_brainstorming=True) == CHAT_MODE_IMAGE


def test_set_selector_mode_selects_by_index():
    ctrl = MagicMock()
    set_selector_mode(ctrl, CHAT_MODE_WEB_RESEARCH, include_brainstorming=True)
    ctrl.selectItemPos.assert_called_once_with(2, True)


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
