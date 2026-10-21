# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Sidebar chat mode dropdown: Chat, Image, Web Research, Deep Research, Brainstorming, Writing Plan, PPT-Master, Librarian."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_TOKEN, str_bounded, deal
from plugin.framework.i18n import _

CHAT_MODE_CHAT = "chat"
CHAT_MODE_IMAGE = "image"
CHAT_MODE_WEB_RESEARCH = "web_research"
CHAT_MODE_DEEP_RESEARCH = "deep_research"
CHAT_MODE_BRAINSTORMING = "brainstorming"
CHAT_MODE_WRITING_PLAN = "writing_plan"
CHAT_MODE_PPT_MASTER = "ppt-master"
CHAT_MODE_LIBRARIAN = "librarian"

# Fixed history_db session id: one Librarian transcript per LibreOffice user
# profile, not per document (unlike doc_session / web_session).
LIBRARIAN_HISTORY_SESSION_ID = "writeragent_librarian"


@dataclass(frozen=True)
class SidebarModeFlags:
    """Which optional sidebar modes appear for the current document type."""

    include_brainstorming: bool = False
    include_writing_plan: bool = True
    include_ppt_master: bool = False


@deal.pre(lambda doc_type_label: str_bounded(doc_type_label, DEAL_MAX_TOKEN))
@deal.post(lambda result: isinstance(result, SidebarModeFlags))
def sidebar_mode_flags_for_doc_type(doc_type_label: str) -> SidebarModeFlags:
    """Writer gets brainstorming and writing plan; Draw and Impress get PPT-Master; Calc and every other label get the shared modes only (Chat, Image, Web Research, Deep Research, Librarian)."""
    if doc_type_label == "writer":
        return SidebarModeFlags(include_brainstorming=True, include_writing_plan=True, include_ppt_master=False)
    if doc_type_label in ("draw", "impress"):
        return SidebarModeFlags(include_brainstorming=False, include_writing_plan=False, include_ppt_master=True)
    return SidebarModeFlags(include_brainstorming=False, include_writing_plan=False, include_ppt_master=False)


def _label_chat() -> str:
    return _("Chat")


def _label_image() -> str:
    return _("Image")


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


def _label_librarian() -> str:
    return _("Librarian")


@dataclass(frozen=True)
class _ModeSpec:
    key: str
    label_fn: Any
    flag_attr: str | None = None
    pinned_last: bool = False


_MODE_SPECS: tuple[_ModeSpec, ...] = (
    _ModeSpec(CHAT_MODE_CHAT, _label_chat),
    _ModeSpec(CHAT_MODE_IMAGE, _label_image),
    _ModeSpec(CHAT_MODE_WEB_RESEARCH, _label_web_research),
    _ModeSpec(CHAT_MODE_DEEP_RESEARCH, _label_deep_research),
    _ModeSpec(CHAT_MODE_BRAINSTORMING, _label_brainstorming, flag_attr="include_brainstorming"),
    _ModeSpec(CHAT_MODE_WRITING_PLAN, _label_writing_plan, flag_attr="include_writing_plan"),
    _ModeSpec(CHAT_MODE_PPT_MASTER, _label_ppt_master, flag_attr="include_ppt_master"),
    _ModeSpec(CHAT_MODE_LIBRARIAN, _label_librarian, pinned_last=True),
)

_SPEC_BY_KEY: dict[str, _ModeSpec] = {spec.key: spec for spec in _MODE_SPECS}
_VALID_MODES: frozenset[str] = frozenset(_SPEC_BY_KEY)


def _modes_for(flags: SidebarModeFlags) -> tuple[str, ...]:
    modes: list[str] = []
    pinned: list[str] = []
    for spec in _MODE_SPECS:
        if spec.flag_attr and not getattr(flags, spec.flag_attr):
            continue
        if spec.pinned_last:
            pinned.append(spec.key)
        else:
            modes.append(spec.key)
    return tuple(modes + pinned)


@deal.post(lambda result: isinstance(result, tuple) and len(result) >= 5)
def get_mode_labels(flags: SidebarModeFlags = SidebarModeFlags()) -> tuple[str, ...]:
    """Translated combobox labels in the same order as ``_modes_for``."""
    return tuple(_SPEC_BY_KEY[mode].label_fn() for mode in _modes_for(flags))


@deal.pre(lambda label, flags=SidebarModeFlags(): str_bounded(label, DEAL_MAX_TOKEN))
@deal.post(lambda result: result in _VALID_MODES)
def mode_from_label(label: str, flags: SidebarModeFlags = SidebarModeFlags()) -> str:
    """Map a combobox display label to a mode constant."""
    # crosshair: off
    text = str(label or "").strip()
    for mode, item_label in zip(_modes_for(flags), get_mode_labels(flags)):
        if text == item_label:
            return mode
    return CHAT_MODE_CHAT


def mode_from_selector_with_flags(ctrl: Any, flags: SidebarModeFlags) -> str:
    """Read the selected sidebar mode from a combobox control.

    Prefer ``getSelectedItemPos`` over ``getText``. The ChatPanel ComboBox is
    ``spin=true``; the edit field can keep the XDL default ``Chat`` while the
    selected index is Librarian (last ``addItems`` entry). Send then ran the
    librarian path despite the visible label.
    """
    # crosshair: off
    if not ctrl:
        return CHAT_MODE_CHAT
    modes = _modes_for(flags)
    if hasattr(ctrl, "getSelectedItemPos"):
        idx = int(ctrl.getSelectedItemPos())
        if 0 <= idx < len(modes):
            return modes[idx]
    if hasattr(ctrl, "getText"):
        return mode_from_label(ctrl.getText(), flags)
    return CHAT_MODE_CHAT


def _set_combobox_items(ctrl: Any, labels: tuple[str, ...]) -> None:
    """Populate a sidebar ComboBox the same way as other working selectors (model StringItemList + addItems)."""
    # crosshair: off
    model = ctrl.getModel() if hasattr(ctrl, "getModel") else None
    if model is not None and hasattr(model, "StringItemList"):
        model.StringItemList = labels

    if hasattr(ctrl, "setStringItemList"):
        ctrl.setStringItemList(labels)

    if hasattr(ctrl, "getItemCount") and hasattr(ctrl, "removeItems"):
        count = ctrl.getItemCount()
        if count:
            ctrl.removeItems(0, count)

    if hasattr(ctrl, "addItems") and labels:
        ctrl.addItems(labels, 0)
        return

    if hasattr(ctrl, "addItem") and labels:
        for label in reversed(labels):
            ctrl.addItem(label, 0)


def _configure_mode_selector_model(ctrl: Any) -> None:
    """Show dropdown arrow; do not set ReadOnly (breaks item list on some LO builds)."""
    if not hasattr(ctrl, "getModel"):
        return
    model = ctrl.getModel()
    if model is None:
        return
    if hasattr(model, "Dropdown"):
        model.Dropdown = True


def populate_mode_selector_with_flags(ctrl: Any, flags: SidebarModeFlags) -> None:
    """Fill the mode combobox with translated items."""
    if not ctrl:
        return
    labels = tuple(str(x) for x in get_mode_labels(flags))
    _configure_mode_selector_model(ctrl)
    _set_combobox_items(ctrl, labels)


def set_selector_mode_with_flags(ctrl: Any, mode: str, flags: SidebarModeFlags) -> None:
    """Set combobox selection by mode constant."""
    # crosshair: off
    if not ctrl or mode not in _VALID_MODES:
        return
    modes = _modes_for(flags)
    if mode not in modes:
        mode = CHAT_MODE_CHAT
    labels = get_mode_labels(flags)
    idx = modes.index(mode)
    label = labels[idx]
    if hasattr(ctrl, "selectItemPos"):
        ctrl.selectItemPos(idx, True)
    if hasattr(ctrl, "setText"):
        ctrl.setText(label)


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


def librarian_default_mode(ctx: Any = None) -> str:
    """Librarian once on first sidebar open; later opens start on Chat even if USER.md is empty.

    USER.md is not the ongoing gate. If a profile already exists (pre-flag installs),
    treat Librarian as already offered so upgrades do not re-open onboarding.
    """
    from plugin.framework.config import get_config_bool_safe

    if get_config_bool_safe("chatbot.librarian_invoked"):
        return CHAT_MODE_CHAT
    if ctx is not None:
        from plugin.chatbot.memory import user_profile_exists

        if user_profile_exists():
            mark_librarian_invoked()
            return CHAT_MODE_CHAT
    return CHAT_MODE_LIBRARIAN


def mark_librarian_invoked() -> None:
    """Record that Librarian was shown or used so it is not the default next time."""
    from plugin.framework.config import set_config
    from plugin.framework.errors import best_effort

    best_effort(lambda: set_config("chatbot.librarian_invoked", True), "mark_librarian_invoked: persist flag")


def clear_librarian_session(send_listener: Any) -> None:
    """Drop sticky librarian flag only — do not wipe librarian ChatSession history."""
    send_listener._in_librarian_mode = False


def clear_writing_plan_session(send_listener: Any) -> None:
    """Drop in-progress writing plan state."""
    send_listener._in_writing_plan_mode = False
    send_listener._writing_plan_topic = ""
