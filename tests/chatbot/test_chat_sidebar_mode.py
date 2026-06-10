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
    CHAT_MODE_IMAGE,
    CHAT_MODE_WEB_RESEARCH,
    clear_brainstorming_session,
    get_mode_labels,
    mode_from_label,
    mode_from_selector,
    persist_mode_to_config,
    populate_mode_selector,
    resolve_initial_mode,
    set_selector_mode,
)


def test_mode_labels_include_brainstorming_when_writer():
    labels = get_mode_labels(include_brainstorming=True)
    assert len(labels) == 4
    assert mode_from_label(labels[0]) == CHAT_MODE_CHAT
    assert mode_from_label(labels[1]) == CHAT_MODE_IMAGE
    assert mode_from_label(labels[2]) == CHAT_MODE_WEB_RESEARCH
    assert mode_from_label(labels[3]) == CHAT_MODE_BRAINSTORMING


def test_mode_labels_omit_brainstorming_for_calc_draw():
    labels = get_mode_labels(include_brainstorming=False)
    assert len(labels) == 3
    assert mode_from_label(labels[-1], include_brainstorming=False) == CHAT_MODE_WEB_RESEARCH


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


def test_resolve_initial_mode_migrates_chat_direct_image():
    ctx = MagicMock()

    def fake_get_config(_ctx, key):
        if key == "chat_sidebar_mode":
            return "chat"
        return None

    with patch("plugin.framework.config.get_config", side_effect=fake_get_config):
        with patch("plugin.framework.config.get_config_bool", return_value=True):
            mode = resolve_initial_mode(ctx, include_brainstorming=True)
    assert mode == CHAT_MODE_IMAGE


def test_persist_mode_to_config_syncs_chat_direct_image():
    ctx = MagicMock()
    writes = {}

    def fake_set_config(_ctx, key, value):
        writes[key] = value

    with patch("plugin.framework.config.set_config", side_effect=fake_set_config):
        persist_mode_to_config(ctx, CHAT_MODE_IMAGE)
    assert writes["chat_sidebar_mode"] == CHAT_MODE_IMAGE
    assert writes["chat_direct_image"] is True

    writes.clear()
    with patch("plugin.framework.config.set_config", side_effect=fake_set_config):
        persist_mode_to_config(ctx, CHAT_MODE_CHAT)
    assert writes["chat_sidebar_mode"] == CHAT_MODE_CHAT
    assert writes["chat_direct_image"] is False


def test_clear_brainstorming_session_resets_flags():
    listener = MagicMock()
    listener._in_brainstorming_mode = True
    listener._brainstorming_topic = "topic"
    clear_brainstorming_session(listener)
    assert listener._in_brainstorming_mode is False
    assert listener._brainstorming_topic == ""
