# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sidebar hamburger popup menu controller.

Opens a native LibreOffice PopupMenu with the full suite of WriterAgent actions
when the hamburger button in the sidebar panel header is clicked.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.doc.doc_type import is_calc, is_draw, is_writer
from plugin.framework.i18n import _
from plugin.framework.main_shared import get_action_handler
from plugin.framework.thread_guard import main_thread_only

log = logging.getLogger("writeragent.hamburger_menu")


def _load_graphic(ctx: Any, icon_filename: str) -> Any:
    """Load a PNG icon from extension assets/ as XGraphic."""
    from plugin.librepy.sidebar_menus import load_menu_graphic

    return load_menu_graphic(ctx, icon_filename)


@main_thread_only
def show_hamburger_menu(ctx: Any, frame: Any, button_ctrl: Any) -> None:
    """Build and execute the hamburger popup menu directly beneath button_ctrl."""
    if ctx is None or button_ctrl is None:
        return

    try:
        model = None
        if frame and hasattr(frame, "getController"):
            ctrl = frame.getController()
            if ctrl and hasattr(ctrl, "getModel"):
                model = ctrl.getModel()

        is_calc_doc = is_calc(model)
        is_writer_doc = is_writer(model)
        is_draw_doc = is_draw(model)

        smgr = getattr(ctx, "getServiceManager", lambda: None)()
        if smgr is None:
            return

        popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
        if popup is None:
            return

        item_actions: dict[int, str] = {}
        next_id = [1]

        def add_item(menu: Any, label: str, action: str, pos: int, icon_filename: str | None = None) -> None:
            n_id = next_id[0]
            next_id[0] += 1
            menu.insertItem(n_id, label, 0, pos)
            item_actions[n_id] = action
            if hasattr(menu, "setCommand"):
                try:
                    menu.setCommand(n_id, "org.extension.writeragent:" + action)
                except Exception:
                    pass
            if icon_filename:
                g = _load_graphic(ctx, icon_filename)
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

        pos = 0

        # 1. Edit / Extend selection
        add_item(popup, _("Extend Selection"), "chatbot.extend_selection", pos)
        pos += 1
        add_item(popup, _("Edit Selection"), "chatbot.edit_selection", pos)
        pos += 1

        # 2. Separator
        popup.insertSeparator(pos)
        pos += 1

        # 3. Main feature actions
        add_item(popup, _("Run Python Script..."), "scripting.run_python_dialog", pos, "python_32.png")
        pos += 1

        if is_calc_doc:
            add_item(popup, _("Edit Python in Cell..."), "scripting.edit_python_cell", pos, "python_cell_32.png")
            pos += 1
            add_item(popup, _("Convert Sheet to Python..."), "calc.convert_spreadsheet_to_python", pos)
            pos += 1

        add_item(popup, _("Search Nearby Files..."), "embeddings.search_dialog", pos, "search_32.png")
        pos += 1

        if is_writer_doc:
            add_item(popup, _("Insert LaTeX Math..."), "writer.insert_latex_dialog", pos, "latex_32.png")
            pos += 1
            add_item(popup, _("Text Analytics..."), "textanalytics.open_dialog", pos)
            pos += 1
            add_item(popup, _("Import Jupyter Notebook..."), "scripting.import_ipynb", pos, "jupyter_32.png")
            pos += 1

        # 4. Separator
        popup.insertSeparator(pos)
        pos += 1

        # 5. Settings & Servers with Dynamic MCP Status
        add_item(popup, _("Settings"), "main.settings", pos)
        pos += 1
        add_item(popup, _("Vision OCR Settings..."), "vision.open_settings", pos)
        pos += 1

        from plugin.main import _get_menu_icon, get_menu_text

        mcp_toggle_text = get_menu_text("mcp.toggle_server") or _("Toggle MCP Server")
        add_item(popup, mcp_toggle_text, "mcp.toggle_server", pos)
        pos += 1

        mcp_running = _get_menu_icon("mcp.server_status") == "running"
        mcp_status_icon = "running_16.png" if mcp_running else "stopped_16.png"
        mcp_status_text = _("MCP Server (Running)") if mcp_running else _("MCP Server (Stopped)")
        add_item(popup, mcp_status_text, "mcp.server_status", pos, mcp_status_icon)
        pos += 1

        add_item(popup, _("Reset Python Session"), "scripting.reset_python_session", pos)
        pos += 1

        # 6. Separator
        popup.insertSeparator(pos)
        pos += 1

        # 7. Debug Submenu
        debug_popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
        if debug_popup is not None:
            d_pos = 0
            if is_writer_doc:
                add_item(debug_popup, _("Run format tests"), "main.RunFormatTests", d_pos)
                d_pos += 1
            if is_calc_doc:
                add_item(debug_popup, _("Run calc tests"), "main.RunCalcTests", d_pos)
                d_pos += 1
                add_item(debug_popup, _("Run Calc API integration tests"), "main.RunCalcIntegrationTests", d_pos)
                d_pos += 1
            if is_draw_doc:
                add_item(debug_popup, _("Run draw tests"), "main.RunDrawTests", d_pos)
                d_pos += 1
            add_item(debug_popup, _("Evaluation Dashboard"), "main.EvaluationDashboard", d_pos)
            d_pos += 1

            debug_id = next_id[0]
            next_id[0] += 1
            popup.insertItem(debug_id, _("Debug"), 0, pos)
            popup.setPopupMenu(debug_id, debug_popup)
            pos += 1

        # 8. Report bug
        add_item(popup, _("Report bug..."), "main.report_bug", pos)
        pos += 1

        from plugin.librepy.sidebar_menus import execute_popup_under_button, invoke_action_handler

        chosen_id = execute_popup_under_button(popup, button_ctrl)
        if chosen_id in item_actions:
            action_name = item_actions[chosen_id]
            log.info("Hamburger menu selected: %s", action_name)
            invoke_action_handler(get_action_handler(action_name), frame)
    except Exception as e:
        log.exception("show_hamburger_menu failed: %s", e)
