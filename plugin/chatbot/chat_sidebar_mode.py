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
import logging
from typing import Any, Callable

from plugin.framework.deal_shim import DEAL_MAX_TOKEN, str_bounded, deal
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

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
    include_writing_plan: bool = False
    include_ppt_master: bool = False
    include_deep_research: bool = False


@deal.pre(lambda doc_type_label: str_bounded(doc_type_label, DEAL_MAX_TOKEN))
@deal.post(lambda result: isinstance(result, SidebarModeFlags))
def sidebar_mode_flags_for_doc_type(doc_type_label: str) -> SidebarModeFlags:
    """Writer gets brainstorming, writing plan, and deep research; Draw and Impress get PPT-Master; Calc and every other label get the shared modes only (Chat, Image, Web Research, Librarian)."""
    if doc_type_label == "writer":
        return SidebarModeFlags(include_brainstorming=True, include_writing_plan=True, include_ppt_master=False, include_deep_research=True)
    if doc_type_label in ("draw", "impress"):
        return SidebarModeFlags(include_brainstorming=False, include_writing_plan=False, include_ppt_master=True)
    return SidebarModeFlags()


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
    greeting_fn: Any = None
    clear_on_switch: bool = True
    doc_type_predicate: Callable[[str], bool] | None = None
    capture_topic: bool = False
    handler: str = ""
    flag_attr: str | None = None
    pinned_last: bool = False


_MODE_SPECS: tuple[_ModeSpec, ...] = (
    _ModeSpec(CHAT_MODE_CHAT, _label_chat),
    _ModeSpec(CHAT_MODE_IMAGE, _label_image, handler="image"),
    _ModeSpec(
        CHAT_MODE_WEB_RESEARCH,
        _label_web_research,
        greeting_fn=lambda _model: _("AI: I can do web research to answer any question, or summarize a web page, without seeing or changing your document. Let's chat."),
        handler="web_research",
    ),
    _ModeSpec(
        CHAT_MODE_DEEP_RESEARCH,
        _label_deep_research,
        greeting_fn=lambda _model: _("AI: Deep Research mode runs a multi-step web investigation (planning, several searches, synthesis) and can insert a formatted report into your document. It takes longer but produces more thorough results."),
        handler="deep_research",
        flag_attr="include_deep_research",
    ),
    _ModeSpec(
        CHAT_MODE_BRAINSTORMING,
        _label_brainstorming,
        greeting_fn=lambda _model: _("AI: Let's explore and design your idea together. I'll ask questions, suggest approaches, and help you build an approved spec in your document when you're ready."),
        doc_type_predicate=lambda d: d == "writer",
        capture_topic=True,
        handler="brainstorming",
        flag_attr="include_brainstorming",
    ),
    _ModeSpec(
        CHAT_MODE_WRITING_PLAN,
        _label_writing_plan,
        greeting_fn=lambda _model: _("AI: Let's draft your document section-by-section. I'll help you create a writing plan outline, and then implement it incrementally with your approval."),
        clear_on_switch=False,
        doc_type_predicate=lambda d: d == "writer",
        capture_topic=True,
        handler="writing_plan",
        flag_attr="include_writing_plan",
    ),
    _ModeSpec(
        CHAT_MODE_PPT_MASTER,
        _label_ppt_master,
        greeting_fn=lambda _model: _("AI: PPT-Master mode — I'll run the ppt-master workflow in your configured Python venv (scripts + export to Impress). Describe your topic or point me at a project folder."),
        doc_type_predicate=lambda d: d in ("draw", "impress"),
        capture_topic=True,
        handler="ppt_master",
        flag_attr="include_ppt_master",
    ),
    _ModeSpec(
        CHAT_MODE_LIBRARIAN,
        _label_librarian,
        greeting_fn=lambda _model: _("AI: I'm the WriterAgent Librarian — a host who can learn your name, favorite colors, and give a short tour. Pick Chat in the dropdown whenever you want to work on the document."),
        handler="librarian",
        pinned_last=True,
    ),
)

assert len(_MODE_SPECS) == len({s.key for s in _MODE_SPECS}), "Duplicate mode keys in _MODE_SPECS"
assert all(s.flag_attr is None or s.flag_attr in SidebarModeFlags.__dataclass_fields__ for s in _MODE_SPECS), "Invalid flag_attr in _MODE_SPECS"
_pinned_seen = False
for _s in _MODE_SPECS:
    if _s.pinned_last:
        _pinned_seen = True
    elif _pinned_seen:
        raise AssertionError("Pinned mode specs must be last")

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


@deal.post(lambda result: isinstance(result, tuple) and len(result) >= 4)
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
    try:
        if hasattr(ctrl, "getSelectedItemPos"):
            idx = int(ctrl.getSelectedItemPos())
            if 0 <= idx < len(modes):
                return modes[idx]
        if hasattr(ctrl, "getText"):
            return mode_from_label(ctrl.getText(), flags)
    except Exception as e:
        from plugin.framework.errors import is_disposed_exception

        if is_disposed_exception(e):
            raise
        log.warning("Failed to read mode from selector: %s", e)
        return CHAT_MODE_CHAT
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


def _clear_mode_topic_session(send_listener: Any, mode: str) -> None:
    if getattr(send_listener, "_active_mode", "") == mode:
        send_listener._active_mode = ""
        spec = _SPEC_BY_KEY.get(mode)
        if spec is not None and spec.capture_topic:
            send_listener._mode_topic = ""


def clear_brainstorming_session(send_listener: Any) -> None:
    """Drop in-progress brainstorming state (dropdown change or normal exit)."""
    # Writing Plan is left set across dropdown applies, so a clear for another mode must not blank this slot.
    _clear_mode_topic_session(send_listener, CHAT_MODE_BRAINSTORMING)


def clear_ppt_master_session(send_listener: Any) -> None:
    """Drop in-progress PPT-Master state."""
    _clear_mode_topic_session(send_listener, CHAT_MODE_PPT_MASTER)


def is_image_mode(mode: str) -> bool:
    return mode == CHAT_MODE_IMAGE


def greeting_for_sidebar_mode(mode: str, model: Any) -> str:
    spec = _SPEC_BY_KEY.get(mode)
    if spec is not None and spec.greeting_fn is not None:
        return spec.greeting_fn(model)
    from plugin.framework.prompts import get_greeting_for_document

    return get_greeting_for_document(model)


def select_librarian_default_mode(has_invoked_flag: bool, profile_exists: bool) -> str:
    """Pure selection of default mode: Librarian once on first open, Chat thereafter."""
    if has_invoked_flag:
        return CHAT_MODE_CHAT
    if profile_exists:
        return CHAT_MODE_CHAT
    return CHAT_MODE_LIBRARIAN


def librarian_default_mode(ctx: Any = None) -> str:
    """Librarian once on first sidebar open; later opens start on Chat even if USER.md is empty.

    USER.md is not the ongoing gate. If a profile already exists (pre-flag installs),
    treat Librarian as already offered so upgrades do not re-open onboarding.
    """
    from plugin.framework.config import get_config_bool_safe

    has_invoked = bool(get_config_bool_safe("chatbot.librarian_invoked"))
    profile_exists = False
    if ctx is not None:
        from plugin.chatbot.memory import user_profile_exists

        profile_exists = user_profile_exists()

    result = select_librarian_default_mode(has_invoked, profile_exists)
    if not has_invoked and (result == CHAT_MODE_LIBRARIAN or profile_exists):
        mark_librarian_invoked()
    return result


def mark_librarian_invoked() -> None:
    """Record that Librarian was shown or used so it is not the default next time."""
    from plugin.framework.config import set_config
    from plugin.framework.errors import best_effort

    best_effort(lambda: set_config("chatbot.librarian_invoked", True), "mark_librarian_invoked: persist flag")


def clear_librarian_session(send_listener: Any) -> None:
    """Drop sticky librarian flag only — do not wipe librarian ChatSession history."""
    _clear_mode_topic_session(send_listener, CHAT_MODE_LIBRARIAN)


def clear_writing_plan_session(send_listener: Any) -> None:
    """Drop in-progress writing plan state."""
    _clear_mode_topic_session(send_listener, CHAT_MODE_WRITING_PLAN)


class SidebarModeController:
    _panel: Any

    def __init__(self, panel: Any) -> None:
        self._panel = panel

    def on_brainstorming_session_finished(self, spec_saved: bool = False) -> None:
        """Reset sidebar after brainstorming_finished (dropdown transitions to Writing Plan or Chat)."""
        flags = getattr(self._panel, "sidebar_mode_flags", None)
        # Read _mode_topic before clear_brainstorming_session blanks
        # it, or the writing-plan handoff is always "Implement the saved spec: ".
        # selectItemPos often does not fire ChatModeListener, so the combo
        # moves and the sidebar stays in the old mode until the user touches it.
        # Apply the mode here.
        topic = getattr(self._panel, "_mode_topic", "") or ""
        clear_brainstorming_session(self._panel)
        apply_fn = getattr(self._panel, "_apply_sidebar_mode_fn", None)
        if spec_saved:
            self._panel._active_mode = CHAT_MODE_WRITING_PLAN
            self._panel._mode_topic = f"Implement the saved spec: {topic}"
            if self._panel.chat_mode_selector and flags:
                set_selector_mode_with_flags(self._panel.chat_mode_selector, CHAT_MODE_WRITING_PLAN, flags)
            if callable(apply_fn):
                apply_fn(CHAT_MODE_WRITING_PLAN)
        else:
            if self._panel.chat_mode_selector and flags:
                set_selector_mode_with_flags(self._panel.chat_mode_selector, CHAT_MODE_CHAT, flags)
            if callable(apply_fn):
                apply_fn(CHAT_MODE_CHAT)

    def on_librarian_session_finished(self) -> None:
        """Reset sidebar after switch_to_document_mode (dropdown returns to Chat). History is kept."""
        flags = getattr(self._panel, "sidebar_mode_flags", None)
        clear_librarian_session(self._panel)
        if self._panel.chat_mode_selector and flags:
            set_selector_mode_with_flags(self._panel.chat_mode_selector, CHAT_MODE_CHAT, flags)
        # Do not rely on ComboBox item-changed: swap to doc_session, re-render Chat
        # pane, and refresh [DOCUMENT CONTENT] for the next send.
        apply_fn = getattr(self._panel, "_apply_sidebar_mode_fn", None)
        if callable(apply_fn):
            apply_fn(CHAT_MODE_CHAT)

    def on_writing_plan_session_finished(self) -> None:
        """Reset sidebar after writing_plan_finished (dropdown returns to Chat)."""
        flags = getattr(self._panel, "sidebar_mode_flags", None)
        clear_writing_plan_session(self._panel)
        if self._panel.chat_mode_selector and flags:
            set_selector_mode_with_flags(self._panel.chat_mode_selector, CHAT_MODE_CHAT, flags)
        # Same as librarian: the combo change does not reliably run ChatModeListener.
        apply_fn = getattr(self._panel, "_apply_sidebar_mode_fn", None)
        if callable(apply_fn):
            apply_fn(CHAT_MODE_CHAT)

    def on_ppt_master_session_finished(self) -> None:
        """Reset sidebar after ppt_master_finished (dropdown returns to Chat)."""
        flags = getattr(self._panel, "sidebar_mode_flags", None)
        clear_ppt_master_session(self._panel)
        if self._panel.chat_mode_selector and flags:
            set_selector_mode_with_flags(self._panel.chat_mode_selector, CHAT_MODE_CHAT, flags)
        apply_fn = getattr(self._panel, "_apply_sidebar_mode_fn", None)
        if callable(apply_fn):
            apply_fn(CHAT_MODE_CHAT)
