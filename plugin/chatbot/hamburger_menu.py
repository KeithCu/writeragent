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
from plugin.framework.errors import best_effort
from plugin.framework.i18n import _
from plugin.framework.main_shared import get_action_handler
from plugin.framework.menu_icon_dpi import menu_icon_filename
from plugin.framework.thread_guard import main_thread_only

log = logging.getLogger("writeragent.hamburger_menu")


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
        pos_box = [0]

        def add_item(menu: Any, label: str, action: str, box: list[int], icon_filename: str | None = None) -> None:
            n_id = next_id[0]
            next_id[0] += 1
            menu.insertItem(n_id, label, 0, box[0])
            box[0] += 1
            item_actions[n_id] = action
            if hasattr(menu, "setCommand"):
                menu.setCommand(n_id, "org.extension.writeragent:" + action)
            if icon_filename:
                from plugin.librepy.sidebar_menus import load_menu_graphic

                g = load_menu_graphic(ctx, icon_filename)
                if g is not None:
                    if hasattr(menu, "setItemImage"):
                        best_effort(lambda: menu.setItemImage(n_id, g, False), f"setItemImage failed for {icon_filename}")
                    elif hasattr(menu, "setItemGraphic"):
                        best_effort(lambda: menu.setItemGraphic(n_id, g), f"setItemGraphic failed for {icon_filename}")

        def add_separator(menu: Any, box: list[int]) -> None:
            menu.insertSeparator(box[0])
            box[0] += 1

        def add_submenu(menu: Any, label: str, submenu: Any, box: list[int]) -> None:
            sub_id = next_id[0]
            next_id[0] += 1
            menu.insertItem(sub_id, label, 0, box[0])
            menu.setPopupMenu(sub_id, submenu)
            box[0] += 1

        # 1. Edit / Extend selection
        add_item(popup, _("Extend Selection"), "chatbot.extend_selection", pos_box)
        add_item(popup, _("Edit Selection"), "chatbot.edit_selection", pos_box)

        # 2. Separator
        add_separator(popup, pos_box)

        # 3. Main feature actions
        add_item(popup, _("Run Python Script..."), "scripting.run_python_dialog", pos_box, menu_icon_filename("python", ctx=ctx))

        if is_calc_doc:
            add_item(popup, _("Edit Python in Cell..."), "scripting.edit_python_cell", pos_box, menu_icon_filename("python_cell", ctx=ctx))
            add_item(popup, _("Convert Sheet to Python..."), "calc.convert_spreadsheet_to_python", pos_box)

        add_item(popup, _("Search Nearby Files..."), "embeddings.search_dialog", pos_box, menu_icon_filename("search", ctx=ctx))

        if is_writer_doc:
            add_item(popup, _("Insert LaTeX Math..."), "writer.insert_latex_dialog", pos_box, menu_icon_filename("latex", ctx=ctx))
            add_item(popup, _("Text Analytics..."), "textanalytics.open_dialog", pos_box)

        # 4. Separator
        add_separator(popup, pos_box)

        # 5. Settings & Servers with Dynamic MCP Status
        add_item(popup, _("Settings"), "main.settings", pos_box, menu_icon_filename("gear", ctx=ctx))
        add_item(popup, _("Vision OCR Settings..."), "vision.open_settings", pos_box)

        from plugin.framework.main_shared import tests_bundled
        from plugin.main import _get_menu_icon, get_menu_text

        mcp_toggle_text = best_effort(lambda: get_menu_text("mcp.toggle_server"), "mcp toggle menu text") or _("Toggle MCP Server")
        add_item(popup, mcp_toggle_text, "mcp.toggle_server", pos_box)

        mcp_running = best_effort(lambda: _get_menu_icon("mcp.server_status"), "mcp status menu icon") == "running"
        mcp_status_icon = menu_icon_filename(
            "running" if mcp_running else "stopped", ctx=ctx
        )
        mcp_status_text = _("MCP Server (Running)") if mcp_running else _("MCP Server (Stopped)")
        add_item(popup, mcp_status_text, "mcp.server_status", pos_box, mcp_status_icon)

        add_item(popup, _("Reset Python Session"), "scripting.reset_python_session", pos_box)
        # Not every Writer file: only an imported notebook that still has code cells.
        # Addons.xcu cannot express that, so these stay hamburger-only.
        from plugin.librepy.sidebar_menus import document_has_notebook_registry

        if is_writer_doc and document_has_notebook_registry(model):
            add_item(popup, _("Run All"), "notebook.run_all", pos_box)
            add_item(popup, _("Run From Here"), "notebook.run_from_here", pos_box)
            add_item(popup, _("Stop"), "notebook.stop", pos_box)

        # Release builds strip Debug from Addons.xcu; omit commands when tests are unbundled.
        if tests_bundled():
            # 6. Separator
            add_separator(popup, pos_box)

            # 7. Debug Submenu
            debug_popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
            if debug_popup is not None:
                d_box = [0]
                if is_writer_doc:
                    add_item(debug_popup, _("Run format tests"), "main.RunFormatTests", d_box)
                if is_calc_doc:
                    add_item(debug_popup, _("Run calc tests"), "main.RunCalcTests", d_box)
                    add_item(debug_popup, _("Run Calc API integration tests"), "main.RunCalcIntegrationTests", d_box)
                if is_draw_doc:
                    add_item(debug_popup, _("Run draw tests"), "main.RunDrawTests", d_box)
                add_item(debug_popup, _("Evaluation Dashboard"), "main.EvaluationDashboard", d_box)

                add_submenu(popup, _("Debug"), debug_popup, pos_box)

        # 8. Report bug
        add_item(popup, _("Report bug..."), "main.report_bug", pos_box)

        from plugin.librepy.sidebar_menus import execute_popup_under_button, invoke_action_handler

        chosen_id = execute_popup_under_button(popup, button_ctrl)
        if chosen_id in item_actions:
            action_name = item_actions[chosen_id]
            log.info("Hamburger menu selected: %s", action_name)
            handler = get_action_handler(action_name)
            if handler is not None:
                invoke_action_handler(handler, frame, command=action_name)
            else:
                # Toggle MCP Server and MCP Server Status are built here and
                # never registered (mcp.toggle_server, mcp.server_status), so
                # get_action_handler is None. The toolbar reaches
                # McpModule.on_action through _dispatch_command; use that path
                # when the registry has no handler. Registered items still
                # get the sidebar frame.
                from plugin.main import _dispatch_command

                _dispatch_command(action_name)
    except Exception as e:
        log.exception("show_hamburger_menu failed: %s", e)
