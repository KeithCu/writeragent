# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy-safe sidebar header buttons and hamburger popup.

Must not import plugin.main, llm_client, embeddings, or MCP. WriterAgent
chat hamburger keeps those extras in hamburger_menu.py.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from plugin.doc.doc_type import is_calc, is_draw, is_writer
from plugin.framework.i18n import _
from plugin.framework.main_shared import get_action_handler
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import BaseActionListener

log = logging.getLogger("writeragent.librepy.sidebar_menus")

# Header icon row in PythonSidebarDialog.xdl (not stretched by layout).
HEADER_BUTTON_IDS = (
    "btn_hdr_settings",
    "btn_python",
    "btn_latex",
    "btn_hamburger",
)

def _librepy_hamburger_specs() -> tuple[tuple[str, str, str | None, str], ...]:
    # Labels at call time so gettext sees the current locale.
    # Icon field is a menu_icon_filename *prefix* (resolved at build time), or None.
    return (
        (_("Run Python Script..."), "scripting.run_python_dialog", "python", "always"),
        (_("Edit Python in Cell..."), "scripting.edit_python_cell", "python_cell", "calc"),
        (_("Insert LaTeX Math..."), "writer.insert_latex_dialog", "latex", "writer"),
        (_("Text Analytics..."), "textanalytics.open_dialog", None, "writer"),
        (_("Settings"), "main.settings", "gear", "always"),
        (_("Vision OCR Settings..."), "vision.open_settings", None, "always"),
        (_("Reset Python Session"), "scripting.reset_python_session", None, "always"),
        (_("Run All"), "notebook.run_all", None, "notebook"),
        (_("Run From Here"), "notebook.run_from_here", None, "notebook"),
        (_("Stop"), "notebook.stop", None, "notebook"),
        (_("Report bug..."), "main.report_bug", None, "always"),
    )


def load_menu_graphic(ctx: Any, icon_filename: str) -> Any:
    """Load a PNG icon from extension assets/ as XGraphic."""
    try:
        from com.sun.star.beans import PropertyValue
        from plugin.framework.uno_context import (
            get_extension_url,
            menu_icon_asset_url,
            menu_icon_filesystem_paths,
        )

        clean_name = icon_filename.replace("assets/", "").lstrip("/")
        smgr = getattr(ctx, "getServiceManager", lambda: None)()
        if smgr is None:
            return None
        gp = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        if gp is None:
            return None

        def _from_url(url: str) -> Any:
            pv = PropertyValue()
            pv.Name = "URL"
            pv.Value = url
            return gp.queryGraphic((pv,))

        # Package URL is a vnd.sun.star.extension:// fallback when the OXT is
        # not installed (testing_runner uses a fresh user profile). queryGraphic
        # then throws; keep going so the filesystem paths below can load the PNG.
        ext_url = get_extension_url(ctx)
        if ext_url:
            try:
                graphic = _from_url(menu_icon_asset_url(ext_url, clean_name))
                if graphic is not None:
                    return graphic
            except Exception:
                log.debug("load_menu_graphic package URL failed for %s", icon_filename, exc_info=True)

        import os
        import uno

        for path in menu_icon_filesystem_paths(clean_name):
            if os.path.isfile(path):
                graphic = _from_url(uno.systemPathToFileUrl(path))
                if graphic is not None:
                    return graphic

        return None
    except Exception:
        log.debug("load_menu_graphic failed for %s", icon_filename, exc_info=True)
        return None


def invoke_action_handler(handler: Callable[..., Any] | None, frame: Any = None) -> None:
    """Call a registered action; most LibrePy handlers take no frame argument."""
    if handler is None:
        return
    wants_frame = False
    try:
        sig = inspect.signature(handler)
        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                wants_frame = frame is not None
                break
            if param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                if param.default is inspect.Parameter.empty:
                    wants_frame = True
                    break
    except (TypeError, ValueError):
        wants_frame = False
    if wants_frame:
        handler(frame)
    else:
        handler()


def command_prefix_for_ctx(ctx: Any) -> str:
    from plugin.framework.uno_context import resolve_package_extension_id

    return resolve_package_extension_id(ctx) + ":"


def document_has_notebook_registry(doc: Any) -> bool:
    """True when File → Open wrote ``WriterAgentNotebookJson`` on *doc*.

    Addons.xcu ``Context=TextDocument`` is every Writer file. These commands
    must not appear there — only on an imported notebook.
    """
    if doc is None:
        return False
    try:
        from plugin.notebook.cell_registry import load_registry

        return load_registry(doc) is not None
    except Exception:
        log.debug("notebook registry check failed", exc_info=True)
        return False


def librepy_hamburger_actions(
    *,
    is_calc_doc: bool,
    is_writer_doc: bool,
    is_draw_doc: bool,
    handler_lookup: Callable[[str], Any] | None = None,
    doc: Any | None = None,
) -> list[tuple[str, str, str | None]]:
    """Return (label, action, icon) rows that have a registered handler."""
    lookup = handler_lookup or get_action_handler
    out: list[tuple[str, str, str | None]] = []
    for label, action, icon, when in _librepy_hamburger_specs():
        if when == "calc" and not is_calc_doc:
            continue
        if when == "writer" and not is_writer_doc:
            continue
        if when == "draw" and not is_draw_doc:
            continue
        if when == "notebook":
            if not is_writer_doc or not document_has_notebook_registry(doc):
                continue
        if lookup(action) is None:
            continue
        filename = None
        if icon:
            from plugin.framework.menu_icon_dpi import menu_icon_filename

            filename = menu_icon_filename(icon)
        out.append((label, action, filename))
    return out


def add_popup_item(
    menu: Any,
    label: str,
    action: str,
    pos: int,
    item_actions: dict[int, str],
    next_id: list[int],
    ctx: Any,
    command_prefix: str,
    icon_filename: str | None = None,
) -> None:
    n_id = next_id[0]
    next_id[0] += 1
    menu.insertItem(n_id, label, 0, pos)
    item_actions[n_id] = action
    if hasattr(menu, "setCommand"):
        try:
            menu.setCommand(n_id, command_prefix + action)
        except Exception:
            pass
    if icon_filename:
        g = load_menu_graphic(ctx, icon_filename)
        if g is not None:
            if hasattr(menu, "setItemImage"):
                try:
                    menu.setItemImage(n_id, g, False)
                except Exception as e:
                    log.debug("setItemImage failed for %s: %s", icon_filename, e)
            elif hasattr(menu, "setItemGraphic"):
                try:
                    menu.setItemGraphic(n_id, g)
                except Exception as e:
                    log.debug("setItemGraphic failed for %s: %s", icon_filename, e)


def execute_popup_under_button(popup: Any, button_ctrl: Any) -> int:
    from com.sun.star.awt import Rectangle

    rect = Rectangle()
    peer = None
    if hasattr(button_ctrl, "getPeer"):
        peer = button_ctrl.getPeer()
    if peer is None and hasattr(button_ctrl, "getContext"):
        peer = button_ctrl.getContext()

    if hasattr(button_ctrl, "getPosSize"):
        ps = button_ctrl.getPosSize()
        rect.X = int(ps.X)
        rect.Y = int(ps.Y + ps.Height)
        rect.Width = int(ps.Width)
        rect.Height = 0

    return int(popup.execute(peer, rect, 0))


def show_python_sidebar_hamburger(ctx: Any, frame: Any, button_ctrl: Any) -> None:
    """WriterAgent: same popup as chat. LibrePy: registered-actions subset only."""
    from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
    from plugin.framework.uno_context import resolve_package_extension_id

    if resolve_package_extension_id(ctx) == EXTENSION_ID_WRITERAGENT:
        try:
            from plugin.chatbot.hamburger_menu import show_hamburger_menu

            show_hamburger_menu(ctx, frame, button_ctrl)
            return
        except ImportError:
            log.debug("chat hamburger unavailable; using LibrePy menu", exc_info=True)
    show_librepy_hamburger_menu(ctx, frame, button_ctrl)


@main_thread_only
def show_librepy_hamburger_menu(ctx: Any, frame: Any, button_ctrl: Any) -> None:
    """Popup of LibrePy-registered actions beneath the hamburger button."""
    if ctx is None or button_ctrl is None:
        return
    try:
        model = None
        if frame and hasattr(frame, "getController"):
            ctrl = frame.getController()
            if ctrl and hasattr(ctrl, "getModel"):
                model = ctrl.getModel()

        smgr = getattr(ctx, "getServiceManager", lambda: None)()
        if smgr is None:
            return
        popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
        if popup is None:
            return

        prefix = command_prefix_for_ctx(ctx)
        item_actions: dict[int, str] = {}
        next_id = [1]
        pos = 0
        for label, action, icon in librepy_hamburger_actions(
            is_calc_doc=is_calc(model),
            is_writer_doc=is_writer(model),
            is_draw_doc=is_draw(model),
            doc=model,
        ):
            add_popup_item(
                popup, label, action, pos, item_actions, next_id, ctx, prefix, icon
            )
            pos += 1

        chosen_id = execute_popup_under_button(popup, button_ctrl)
        if chosen_id in item_actions:
            action_name = item_actions[chosen_id]
            log.info("LibrePy hamburger selected: %s", action_name)
            invoke_action_handler(get_action_handler(action_name), frame)
    except Exception:
        log.exception("show_librepy_hamburger_menu failed")


class _DispatchActionListener(BaseActionListener):
    def __init__(self, action: str, frame: Any = None) -> None:
        self._action = action
        self._frame = frame

    def on_action_performed(self, rEvent) -> None:
        invoke_action_handler(get_action_handler(self._action), self._frame)


class _HamburgerListener(BaseActionListener):
    def __init__(self, ctx: Any, frame: Any) -> None:
        self.ctx = ctx
        self._frame = frame

    def on_action_performed(self, rEvent) -> None:
        button_ctrl = getattr(rEvent, "Source", None)
        show_python_sidebar_hamburger(self.ctx, self._frame, button_ctrl)


def wire_sidebar_header_buttons(
    ctx: Any,
    frame: Any,
    controls: dict[str, Any],
    *,
    calc_doc: bool,
) -> None:
    """Wire Settings / Python / cell-or-LaTeX / hamburger; no Search (embeddings)."""
    from plugin.framework.uno_context import get_extension_url

    ext_url = get_extension_url(ctx)
    from plugin.framework.menu_icon_dpi import menu_icon_asset_rel

    if calc_doc:
        third_action = "scripting.edit_python_cell"
        third_tip = _("Edit Python in Cell...")
        third_icon = menu_icon_asset_rel("python_cell", ctx=ctx)
        third_label = ""
    else:
        third_action = "writer.insert_latex_dialog"
        third_tip = _("Insert LaTeX Math...")
        # Keep √x glyph (not an emoji asset); only PNG buttons use the DPI resolver.
        third_icon = None
        third_label = "√x"

    specs: list[tuple[str, str, str, str | None, str | None]] = [
        ("btn_hdr_settings", "main.settings", _("Settings"), menu_icon_asset_rel("gear", ctx=ctx), ""),
        ("btn_python", "scripting.run_python_dialog", _("Run Python Script..."), menu_icon_asset_rel("python", ctx=ctx), ""),
        ("btn_latex", third_action, third_tip, third_icon, third_label),
    ]
    for btn_id, action, tooltip, icon_rel, label_text in specs:
        btn_ctrl = controls.get(btn_id)
        if not btn_ctrl:
            continue
        try:
            if hasattr(btn_ctrl, "getModel"):
                btn_m = btn_ctrl.getModel()
                if btn_m:
                    if hasattr(btn_m, "HelpText"):
                        btn_m.HelpText = tooltip
                    if label_text is not None and hasattr(btn_m, "Label"):
                        btn_m.Label = label_text
                    if icon_rel and ext_url and hasattr(btn_m, "ImageURL"):
                        btn_m.ImageURL = ext_url.rstrip("/") + "/" + icon_rel
            btn_ctrl.addActionListener(_DispatchActionListener(action, frame))
        except Exception:
            log.exception("Header button %s wiring error", btn_id)

    ham = controls.get("btn_hamburger")
    if ham:
        try:
            if hasattr(ham, "getModel"):
                btn_m = ham.getModel()
                if btn_m and hasattr(btn_m, "HelpText"):
                    btn_m.HelpText = _("More actions...")
            ham.addActionListener(_HamburgerListener(ctx, frame))
        except Exception:
            log.exception("Hamburger button wiring error")
