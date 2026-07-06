# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Sidebar chat mode dropdown: Chat, Image, Web Research, Deep Research, Brainstorming, Writing Plan, PPT-Master."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from plugin.framework.i18n import _

log = logging.getLogger(__name__)

CHAT_MODE_CHAT = "chat"
CHAT_MODE_IMAGE = "image"
CHAT_MODE_WEB_RESEARCH = "web_research"
CHAT_MODE_DEEP_RESEARCH = "deep_research"
CHAT_MODE_BRAINSTORMING = "brainstorming"
CHAT_MODE_WRITING_PLAN = "writing_plan"
CHAT_MODE_PPT_MASTER = "ppt-master"

_VALID_MODES = frozenset(
    {
        CHAT_MODE_CHAT,
        CHAT_MODE_IMAGE,
        CHAT_MODE_WEB_RESEARCH,
        CHAT_MODE_DEEP_RESEARCH,
        CHAT_MODE_BRAINSTORMING,
        CHAT_MODE_WRITING_PLAN,
        CHAT_MODE_PPT_MASTER,
    }
)


@dataclass(frozen=True)
class SidebarModeFlags:
    """Which optional sidebar modes appear for the current document type."""

    include_brainstorming: bool = False
    include_writing_plan: bool = True
    include_ppt_master: bool = False


def sidebar_mode_flags_for_doc_type(doc_type_label: str) -> SidebarModeFlags:
    """Writer: brainstorming + writing plan. Draw/Impress: PPT-Master. Calc: writing plan only."""
    if doc_type_label == "writer":
        return SidebarModeFlags(include_brainstorming=True, include_writing_plan=True, include_ppt_master=False)
    if doc_type_label in ("draw", "impress"):
        return SidebarModeFlags(include_brainstorming=False, include_writing_plan=False, include_ppt_master=True)
    return SidebarModeFlags(include_brainstorming=False, include_writing_plan=True, include_ppt_master=False)


def _label_chat() -> str:
    return _("Chat")


def _label_image() -> str:
    return _("Image model")


def _label_web_research() -> str:
    return _("Web Research")


def _label_deep_research() -> str:
    return _("Deep Research")


def _label_brainstorming() -> str:
    return _("Brainstorming")


def _label_writing_plan() -> str:
    return _("Writing Plan")


def _label_ppt_master() -> str:
    return _("PPT-Master")


def _modes_for(flags: SidebarModeFlags) -> tuple[str, ...]:
    modes: list[str] = [CHAT_MODE_CHAT, CHAT_MODE_IMAGE, CHAT_MODE_WEB_RESEARCH, CHAT_MODE_DEEP_RESEARCH]
    if flags.include_brainstorming:
        modes.append(CHAT_MODE_BRAINSTORMING)
    if flags.include_writing_plan:
        modes.append(CHAT_MODE_WRITING_PLAN)
    if flags.include_ppt_master:
        modes.append(CHAT_MODE_PPT_MASTER)
    return tuple(modes)


def get_mode_labels(*, include_brainstorming: bool = False, include_writing_plan: bool = True, include_ppt_master: bool = False) -> tuple[str, ...]:
    """Translated combobox labels in display order."""
    flags = SidebarModeFlags(
        include_brainstorming=include_brainstorming,
        include_writing_plan=include_writing_plan,
        include_ppt_master=include_ppt_master,
    )
    labels = [_label_chat(), _label_image(), _label_web_research(), _label_deep_research()]
    if flags.include_brainstorming:
        labels.append(_label_brainstorming())
    if flags.include_writing_plan:
        labels.append(_label_writing_plan())
    if flags.include_ppt_master:
        labels.append(_label_ppt_master())
    return tuple(labels)


def mode_from_label(label: str, *, include_brainstorming: bool = False, include_writing_plan: bool = True, include_ppt_master: bool = False) -> str:
    """Map a combobox display label to a mode constant."""
    flags = SidebarModeFlags(
        include_brainstorming=include_brainstorming,
        include_writing_plan=include_writing_plan,
        include_ppt_master=include_ppt_master,
    )
    text = str(label or "").strip()
    for mode, item_label in zip(_modes_for(flags), get_mode_labels(**flags.__dict__)):
        if text == item_label:
            return mode
    return CHAT_MODE_CHAT


def mode_from_selector(ctrl: Any, *, include_brainstorming: bool = False, include_writing_plan: bool = True, include_ppt_master: bool = False) -> str:
    """Read the selected sidebar mode from a combobox control."""
    if not ctrl:
        return CHAT_MODE_CHAT
    try:
        if hasattr(ctrl, "getText"):
            return mode_from_label(
                ctrl.getText(),
                include_brainstorming=include_brainstorming,
                include_writing_plan=include_writing_plan,
                include_ppt_master=include_ppt_master,
            )
    except Exception:
        pass
    return CHAT_MODE_CHAT


def mode_from_selector_with_flags(ctrl: Any, flags: SidebarModeFlags) -> str:
    return mode_from_selector(
        ctrl,
        include_brainstorming=flags.include_brainstorming,
        include_writing_plan=flags.include_writing_plan,
        include_ppt_master=flags.include_ppt_master,
    )


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


def populate_mode_selector(ctrl: Any, *, include_brainstorming: bool = False, include_writing_plan: bool = True, include_ppt_master: bool = False) -> None:
    """Fill the mode combobox with translated items."""
    if not ctrl:
        return
    labels = get_mode_labels(
        include_brainstorming=include_brainstorming,
        include_writing_plan=include_writing_plan,
        include_ppt_master=include_ppt_master,
    )
    labels = tuple(str(x) for x in labels)
    _configure_mode_selector_model(ctrl)
    _set_combobox_items(ctrl, labels)


def populate_mode_selector_with_flags(ctrl: Any, flags: SidebarModeFlags) -> None:
    populate_mode_selector(
        ctrl,
        include_brainstorming=flags.include_brainstorming,
        include_writing_plan=flags.include_writing_plan,
        include_ppt_master=flags.include_ppt_master,
    )


def set_selector_mode(ctrl: Any, mode: str, *, include_brainstorming: bool = False, include_writing_plan: bool = True, include_ppt_master: bool = False) -> None:
    """Set combobox selection by mode constant."""
    if not ctrl or mode not in _VALID_MODES:
        return
    flags = SidebarModeFlags(
        include_brainstorming=include_brainstorming,
        include_writing_plan=include_writing_plan,
        include_ppt_master=include_ppt_master,
    )
    modes = _modes_for(flags)
    if mode not in modes:
        mode = CHAT_MODE_CHAT
    labels = get_mode_labels(**flags.__dict__)
    idx = modes.index(mode)
    label = labels[idx]
    try:
        if hasattr(ctrl, "selectItemPos"):
            ctrl.selectItemPos(idx, True)
        elif hasattr(ctrl, "setText"):
            ctrl.setText(label)
    except Exception as e:
        log.debug("chat_mode_selector: set selection failed: %s", e)


def set_selector_mode_with_flags(ctrl: Any, mode: str, flags: SidebarModeFlags) -> None:
    set_selector_mode(
        ctrl,
        mode,
        include_brainstorming=flags.include_brainstorming,
        include_writing_plan=flags.include_writing_plan,
        include_ppt_master=flags.include_ppt_master,
    )


def clear_brainstorming_session(send_listener: Any) -> None:
    """Drop in-progress brainstorming state (dropdown change or normal exit)."""
    send_listener._in_brainstorming_mode = False
    send_listener._brainstorming_topic = ""


def clear_ppt_master_session(send_listener: Any) -> None:
    """Drop in-progress PPT-Master state."""
    send_listener._in_ppt_master_mode = False
    send_listener._ppt_master_topic = ""


def is_image_mode(mode: str) -> bool:
    return mode == CHAT_MODE_IMAGE


def is_web_research_mode(mode: str) -> bool:
    return mode == CHAT_MODE_WEB_RESEARCH


def is_deep_research_mode(mode: str) -> bool:
    return mode == CHAT_MODE_DEEP_RESEARCH


def is_brainstorming_mode(mode: str) -> bool:
    return mode == CHAT_MODE_BRAINSTORMING


def is_writing_plan_mode(mode: str) -> bool:
    return mode == CHAT_MODE_WRITING_PLAN


def is_ppt_master_mode(mode: str) -> bool:
    return mode == CHAT_MODE_PPT_MASTER


def clear_writing_plan_session(send_listener: Any) -> None:
    """Drop in-progress writing plan state."""
    send_listener._in_writing_plan_mode = False
    send_listener._writing_plan_topic = ""
