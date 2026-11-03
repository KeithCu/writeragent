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
from typing import Any

from plugin.chatbot.dialogs import set_combo_items
from plugin.framework.deal_shim import DEAL_MAX_TOKEN, str_bounded, deal
from plugin.framework.i18n import N_, _

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


@dataclass(frozen=True)
class SidebarMode:
    key: str
    kind: str = "session"  # "chat" | "image" | "session"
    label_fn: Any = None
    greeting_fn: Any = None
    flag_attr: str | None = None
    capture_topic: bool = False
    topic_policy: str = ""  # "" | "switch" | "fill"
    allowed_docs: tuple[str, ...] | None = None
    dispatch_log: str = ""
    tool_name: str = ""
    tool_label: str = ""
    error_tag: str = ""
    default_error: str = ""
    finish_msgid: str | None = None
    leave_flag: str | None = None
    leave_status: str | None = None  # None | "finished" | "switch_mode"
    tracks_spec_saved: bool = False
    reply_description: str = ""
    handoff_topic_prefix: str = ""
    next_mode_if_spec_saved: str | None = None
    prefetch_suggested_name: bool = False
    set_active_mode: bool = True
    prepend_web_cache: bool = False
    persist_across_dropdown: bool = False
    extra_kwarg: str | None = None
    extra_attr: str | None = None

    def __post_init__(self) -> None:
        if self.leave_flag is not None:
            assert self.leave_flag, "leave_flag must be non-empty"
            reserved = {"answer", "message", "result", "status"}
            assert self.leave_flag not in reserved, f"leave_flag {self.leave_flag!r} collision with reserved field"


# Literals inside _() so xgettext extracts them (_(CONST) is invisible).
_MODES: tuple[SidebarMode, ...] = (
    SidebarMode(
        key=CHAT_MODE_CHAT,
        kind="chat",
        label_fn=lambda: _("Chat"),
    ),
    SidebarMode(
        key=CHAT_MODE_IMAGE,
        kind="image",
        label_fn=lambda: _("Image"),
    ),
    SidebarMode(
        key=CHAT_MODE_WEB_RESEARCH,
        kind="session",
        label_fn=lambda: _("Web Research"),
        greeting_fn=lambda: _("AI: I can do web research to answer any question, or summarize a web page, without seeing or changing your document. Let's chat."),
        dispatch_log="_do_send: using web research sub-agent — skip chat model and direct image",
        tool_name="web_research",
        tool_label="web search",
        error_tag="Research",
        default_error=_("Unknown research error."),
        set_active_mode=False,
        prepend_web_cache=True,
    ),
    SidebarMode(
        key=CHAT_MODE_DEEP_RESEARCH,
        kind="session",
        label_fn=lambda: _("Deep Research"),
        greeting_fn=lambda: _("AI: Deep Research mode runs a multi-step web investigation (planning, several searches, synthesis) and can insert a formatted report into your document. It takes longer but produces more thorough results."),
        flag_attr="include_deep_research",
        dispatch_log="_do_send: using deep web research sub-agent — skip chat model and direct image",
        tool_name="deep_research_session",
        tool_label="deep research",
        error_tag="Deep research",
        default_error=_("Unknown deep research error."),
    ),
    SidebarMode(
        key=CHAT_MODE_BRAINSTORMING,
        kind="session",
        label_fn=lambda: _("Brainstorming"),
        greeting_fn=lambda: _("AI: Let's explore and design your idea together. I'll ask questions, suggest approaches, and help you build an approved spec in your document when you're ready."),
        flag_attr="include_brainstorming",
        capture_topic=True,
        topic_policy="switch",
        allowed_docs=("writer",),
        dispatch_log="_do_send: using brainstorming sub-agent",
        tool_name="brainstorming_session",
        tool_label="brainstorming",
        error_tag="Brainstorming",
        default_error=_("Unknown brainstorming error."),
        finish_msgid=N_("Brainstorming complete."),
        leave_flag="brainstorming_finished",
        leave_status="finished",
        tracks_spec_saved=True,
        reply_description=(
            "Reply to the user in the sidebar (HTML). Set brainstorming_finished true "
            "to end brainstorming after the spec is saved and reviewed. spec_saved is "
            "recorded from a successful save_design_spec this turn."
        ),
        handoff_topic_prefix="Implement the saved spec: ",
        next_mode_if_spec_saved=CHAT_MODE_WRITING_PLAN,
        extra_kwarg="topic",
        extra_attr="_mode_topic",
    ),
    # Writing-plan tools are TextDocument-only. Non-Writer sidebars do not list this mode.
    SidebarMode(
        key=CHAT_MODE_WRITING_PLAN,
        kind="session",
        label_fn=lambda: _("Writing Plan"),
        greeting_fn=lambda: _("AI: Let's draft your document section-by-section. I'll help you create a writing plan outline, and then implement it incrementally with your approval."),
        flag_attr="include_writing_plan",
        capture_topic=True,
        topic_policy="fill",
        allowed_docs=("writer",),
        dispatch_log="_do_send: using writing plan sub-agent",
        tool_name="writing_plan_session",
        tool_label="writing plan",
        error_tag="Writing plan",
        default_error=_("Unknown writing plan error."),
        finish_msgid=N_("Writing plan complete."),
        leave_flag="writing_plan_finished",
        leave_status="finished",
        persist_across_dropdown=True,
        reply_description=(
            "Reply to the user in the sidebar (HTML). Set writing_plan_finished true "
            "when the writing plan is done."
        ),
        extra_kwarg="topic",
        extra_attr="_mode_topic",
    ),
    SidebarMode(
        key=CHAT_MODE_PPT_MASTER,
        kind="session",
        label_fn=lambda: _("PPT-Master"),
        greeting_fn=lambda: _("AI: PPT-Master mode — I'll run the ppt-master workflow in your configured Python venv (scripts + export to Impress). Describe your topic or point me at a project folder."),
        flag_attr="include_ppt_master",
        capture_topic=True,
        topic_policy="switch",
        allowed_docs=("draw", "impress"),
        dispatch_log="_do_send: using PPT-Master sub-agent",
        tool_name="ppt_master_session",
        tool_label="PPT-Master",
        error_tag="PPT-Master",
        default_error=_("Unknown PPT-Master error."),
        finish_msgid=N_("PPT-Master session complete."),
        leave_flag=None,
        leave_status="finished",
        extra_kwarg="topic",
        extra_attr="_mode_topic",
    ),
    SidebarMode(
        key=CHAT_MODE_LIBRARIAN,
        kind="session",
        label_fn=lambda: _("Librarian"),
        greeting_fn=lambda: _("AI: I'm the WriterAgent Librarian — a host who can learn your name, favorite colors, and give a short tour. Pick Chat in the dropdown whenever you want to work on the document."),
        dispatch_log="_do_send: using librarian onboarding agent",
        tool_name="librarian_onboarding",
        tool_label="librarian",
        error_tag="Librarian",
        default_error=_("Unknown librarian error."),
        finish_msgid=N_("Perfect! I'm switching you to the main assistant now."),
        leave_flag="switch_to_document_mode",
        leave_status="switch_mode",
        prefetch_suggested_name=True,
        reply_description=(
            "Reply to the user in the sidebar. Set switch_to_document_mode true when "
            "onboarding is done or they want document work (switches the dropdown to Chat)."
        ),
        extra_kwarg="suggested_user_name",
        extra_attr="_librarian_suggested_user_name",
    ),
)

assert len(_MODES) == len({s.key for s in _MODES}), "Duplicate mode keys in _MODES"
assert all(s.flag_attr is None or s.flag_attr in SidebarModeFlags.__dataclass_fields__ for s in _MODES), "Invalid flag_attr in _MODES"

_MODE_BY_KEY: dict[str, SidebarMode] = {mode.key: mode for mode in _MODES}


def sidebar_mode(key: str) -> SidebarMode | None:
    return _MODE_BY_KEY.get(key)


def require_sidebar_mode(key: str) -> SidebarMode:
    return _MODE_BY_KEY[key]


# Tuple order is combobox order; Librarian stays last.
def _modes_for(flags: SidebarModeFlags) -> tuple[str, ...]:
    return tuple(s.key for s in _MODES if not s.flag_attr or getattr(flags, s.flag_attr))


@deal.post(lambda result: isinstance(result, tuple) and len(result) >= 4)
def get_mode_labels(flags: SidebarModeFlags = SidebarModeFlags()) -> tuple[str, ...]:
    """Translated combobox labels in the same order as ``_modes_for``."""
    return tuple(_MODE_BY_KEY[mode].label_fn() for mode in _modes_for(flags))


@deal.pre(lambda label, flags=SidebarModeFlags(): str_bounded(label, DEAL_MAX_TOKEN))
@deal.post(lambda result: result in _MODE_BY_KEY)
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
        from plugin.framework.errors import is_disposal

        if is_disposal(e):
            raise
        log.warning("Failed to read mode from selector: %s", e)
        return CHAT_MODE_CHAT
    return CHAT_MODE_CHAT


def populate_mode_selector_with_flags(ctrl: Any, flags: SidebarModeFlags) -> None:
    """Fill the mode combobox with translated items."""
    if not ctrl:
        return
    # Show dropdown arrow; do not set ReadOnly (breaks item list on some LO builds).
    if hasattr(ctrl, "getModel"):
        model = ctrl.getModel()
        if model is not None and hasattr(model, "Dropdown"):
            model.Dropdown = True
    set_combo_items(ctrl, get_mode_labels(flags))


def set_selector_mode_with_flags(ctrl: Any, mode: str, flags: SidebarModeFlags) -> None:
    """Set combobox selection by mode constant."""
    # crosshair: off
    if not ctrl or mode not in _MODE_BY_KEY:
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


def _clear_mode_session(listener: Any, key: str) -> None:
    if getattr(listener, "_active_mode", "") == key:
        listener._active_mode = ""
        mode = sidebar_mode(key)
        if mode is not None and mode.capture_topic:
            listener._mode_topic = ""


def release_sticky_session(listener: Any, new_mode: str) -> None:
    """Clear active session when leaving a sticky mode, except across dropdowns that persist."""
    active = getattr(listener, "_active_mode", "")
    if not active or active == new_mode:
        return
    mode = sidebar_mode(active)
    if mode is None or mode.persist_across_dropdown:
        return
    _clear_mode_session(listener, active)


def is_image_mode(mode: str) -> bool:
    return mode == CHAT_MODE_IMAGE


def greeting_for_sidebar_mode(mode: str, model: Any) -> str:
    spec = _MODE_BY_KEY.get(mode)
    if spec is not None and spec.greeting_fn is not None:
        return spec.greeting_fn()
    from plugin.framework.prompts import get_greeting_for_document

    return get_greeting_for_document(model)


def librarian_default_mode() -> str:
    """Librarian once on first sidebar open; later opens start on Chat even if USER.md is empty.

    USER.md is not the ongoing gate. If a profile already exists (pre-flag installs),
    treat Librarian as already offered so upgrades do not re-open onboarding.
    """
    from plugin.framework.config import get_config

    if get_config("chatbot.librarian_invoked"):
        return CHAT_MODE_CHAT
    from plugin.chatbot.memory import user_profile_exists

    profile_exists = user_profile_exists()
    mark_librarian_invoked()
    return CHAT_MODE_CHAT if profile_exists else CHAT_MODE_LIBRARIAN


def mark_librarian_invoked() -> None:
    """Record that Librarian was shown or used so it is not the default next time."""
    from plugin.framework.config import save
    from plugin.framework.errors import best_effort

    best_effort(lambda: save({"chatbot.librarian_invoked": True}), "mark_librarian_invoked: persist flag")


class SidebarModeController:
    _panel: Any

    def __init__(self, panel: Any) -> None:
        self._panel = panel

    def _show_mode(self, mode: str) -> None:
        if self._panel.chat_mode_selector:
            set_selector_mode_with_flags(self._panel.chat_mode_selector, mode, self._panel.sidebar_mode_flags)
        # Combo change does not reliably fire ChatModeListener; apply the mode here.
        self._panel._apply_sidebar_mode_fn(mode)

    def on_mode_finished(self, key: str, payload: dict[str, Any]) -> None:
        """Reset sidebar or transition to next mode when a specialized session finishes."""
        mode = sidebar_mode(key)
        if mode is None or mode.leave_status is None:
            return
        topic = getattr(self._panel, "_mode_topic", "") or ""
        _clear_mode_session(self._panel, key)
        if mode.tracks_spec_saved and bool(payload.get("spec_saved", False)) and mode.next_mode_if_spec_saved:
            self._panel._active_mode = mode.next_mode_if_spec_saved
            self._panel._mode_topic = f"{mode.handoff_topic_prefix}{topic}"
            self._show_mode(mode.next_mode_if_spec_saved)
        else:
            self._show_mode(CHAT_MODE_CHAT)
