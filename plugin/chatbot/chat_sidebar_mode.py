# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Sidebar chat mode dropdown: Chat, Image, Web Research, Brainstorming."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.i18n import _

log = logging.getLogger(__name__)

CHAT_MODE_CHAT = "chat"
CHAT_MODE_IMAGE = "image"
CHAT_MODE_WEB_RESEARCH = "web_research"
CHAT_MODE_BRAINSTORMING = "brainstorming"
CHAT_MODE_WRITING_PLAN = "writing_plan"

_VALID_MODES = frozenset({CHAT_MODE_CHAT, CHAT_MODE_IMAGE, CHAT_MODE_WEB_RESEARCH, CHAT_MODE_BRAINSTORMING, CHAT_MODE_WRITING_PLAN})


def _label_chat() -> str:
    return _("Chat")


def _label_image() -> str:
    return _("Use Image model")


def _label_web_research() -> str:
    return _("Web Research")


def _label_brainstorming() -> str:
    return _("Brainstorming")


def _label_writing_plan() -> str:
    return _("Writing Plan")


def get_mode_labels(*, include_brainstorming: bool = True) -> tuple[str, ...]:
    """Translated combobox labels in display order."""
    labels = (_label_chat(), _label_image(), _label_web_research())
    if include_brainstorming:
        return labels + (_label_brainstorming(), _label_writing_plan())
    return labels + (_label_writing_plan(),)


def mode_from_label(label: str, *, include_brainstorming: bool = True) -> str:
    """Map a combobox display label to a mode constant."""
    text = str(label or "").strip()
    for mode, item_label in zip(_modes_for(include_brainstorming), get_mode_labels(include_brainstorming=include_brainstorming)):
        if text == item_label:
            return mode
    return CHAT_MODE_CHAT


def _modes_for(include_brainstorming: bool) -> tuple[str, ...]:
    modes = (CHAT_MODE_CHAT, CHAT_MODE_IMAGE, CHAT_MODE_WEB_RESEARCH)
    if include_brainstorming:
        return modes + (CHAT_MODE_BRAINSTORMING, CHAT_MODE_WRITING_PLAN)
    return modes + (CHAT_MODE_WRITING_PLAN,)


def mode_from_selector(ctrl: Any, *, include_brainstorming: bool = True) -> str:
    """Read the selected sidebar mode from a combobox control."""
    if not ctrl:
        return CHAT_MODE_CHAT
    try:
        if hasattr(ctrl, "getText"):
            return mode_from_label(ctrl.getText(), include_brainstorming=include_brainstorming)
    except Exception:
        pass
    return CHAT_MODE_CHAT


def _set_combobox_items(ctrl: Any, labels: tuple[str, ...]) -> None:
    """Populate a sidebar ComboBox the same way as other working selectors (model StringItemList + addItems)."""
    model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
    if model is not None and hasattr(model, "StringItemList"):
        try:
            model.StringItemList = labels
        except Exception as e:
            log.debug("chat_mode_selector: model.StringItemList failed: %s", e)

    if hasattr(ctrl, "setStringItemList"):
        try:
            ctrl.setStringItemList(labels)
        except Exception as e:
            log.debug("chat_mode_selector: setStringItemList failed: %s", e)

    try:
        if hasattr(ctrl, "getItemCount") and hasattr(ctrl, "removeItems"):
            count = ctrl.getItemCount()
            if count:
                ctrl.removeItems(0, count)
    except Exception as e:
        log.debug("chat_mode_selector: removeItems failed: %s", e)

    if hasattr(ctrl, "addItems") and labels:
        try:
            ctrl.addItems(labels, 0)
            return
        except Exception as e:
            log.debug("chat_mode_selector: addItems failed: %s", e)

    if hasattr(ctrl, "addItem") and labels:
        try:
            for label in reversed(labels):
                ctrl.addItem(label, 0)
        except Exception as e:
            log.debug("chat_mode_selector: addItem failed: %s", e)


def _configure_mode_selector_model(ctrl: Any) -> None:
    """Show dropdown arrow; do not set ReadOnly (breaks item list on some LO builds)."""
    if not hasattr(ctrl, "getModel"):
        return
    try:
        model = ctrl.getModel()
        if model is None:
            return
        if hasattr(model, "Dropdown"):
            model.Dropdown = True
    except Exception as e:
        log.debug("chat_mode_selector: configure model failed: %s", e)


def populate_mode_selector(ctrl: Any, *, include_brainstorming: bool = True) -> None:
    """Fill the mode combobox with translated items."""
    if not ctrl:
        return
    labels = tuple(str(x) for x in get_mode_labels(include_brainstorming=include_brainstorming))
    _configure_mode_selector_model(ctrl)
    _set_combobox_items(ctrl, labels)


def set_selector_mode(ctrl: Any, mode: str, *, include_brainstorming: bool = True) -> None:
    """Set combobox selection by mode constant."""
    if not ctrl or mode not in _VALID_MODES:
        return
    labels = get_mode_labels(include_brainstorming=include_brainstorming)
    modes = _modes_for(include_brainstorming)
    if mode not in modes:
        mode = CHAT_MODE_CHAT
    idx = modes.index(mode)
    label = labels[idx]
    try:
        if hasattr(ctrl, "selectItemPos"):
            ctrl.selectItemPos(idx, True)
        elif hasattr(ctrl, "setText"):
            ctrl.setText(label)
    except Exception as e:
        log.debug("chat_mode_selector: set selection failed: %s", e)


def clear_brainstorming_session(send_listener: Any) -> None:
    """Drop in-progress brainstorming state (dropdown change or normal exit)."""
    send_listener._in_brainstorming_mode = False
    send_listener._brainstorming_topic = ""


def is_image_mode(mode: str) -> bool:
    return mode == CHAT_MODE_IMAGE


def is_web_research_mode(mode: str) -> bool:
    return mode == CHAT_MODE_WEB_RESEARCH


def is_brainstorming_mode(mode: str) -> bool:
    return mode == CHAT_MODE_BRAINSTORMING


def is_writing_plan_mode(mode: str) -> bool:
    return mode == CHAT_MODE_WRITING_PLAN


def clear_writing_plan_session(send_listener: Any) -> None:
    """Drop in-progress writing plan state."""
    send_listener._in_writing_plan_mode = False
    send_listener._writing_plan_topic = ""
