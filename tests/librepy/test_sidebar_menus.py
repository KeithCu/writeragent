# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for LibrePy sidebar header and hamburger (no embeddings/MCP)."""

from unittest.mock import MagicMock, patch

from plugin.framework.constants import EXTENSION_ID_LIBREPY
from plugin.librepy.sidebar_menus import (
    invoke_action_handler,
    librepy_hamburger_actions,
    show_librepy_hamburger_menu,
    show_python_sidebar_hamburger,
    wire_sidebar_header_buttons,
)

_CORE_ACTIONS = {
    "scripting.run_python_dialog",
    "scripting.edit_python_cell",
    "scripting.reset_python_session",
    "notebook.run_all",
    "notebook.run_from_here",
    "notebook.stop",
    "main.settings",
    "vision.open_settings",
    "main.report_bug",
}


def _lookup(action: str):
    if action in _CORE_ACTIONS:
        return lambda: None
    return None


def test_calc_hamburger_includes_python_and_cell_not_search():
    rows = librepy_hamburger_actions(
        is_calc_doc=True,
        is_writer_doc=False,
        is_draw_doc=False,
        handler_lookup=_lookup,
    )
    actions = [a for _label, a, _icon in rows]
    assert "scripting.run_python_dialog" in actions
    assert "scripting.edit_python_cell" in actions
    assert "scripting.reset_python_session" in actions
    assert "main.settings" in actions
    by_action = {a: icon for _label, a, icon in rows}
    assert by_action["main.settings"] is not None
    assert by_action["main.settings"].startswith("gear_")
    assert by_action["main.settings"].endswith(".png")
    assert "vision.open_settings" in actions
    assert "main.report_bug" in actions
    assert "embeddings.search_dialog" not in actions
    assert "mcp.toggle_server" not in actions
    assert "chatbot.extend_selection" not in actions
    assert "calc.convert_spreadsheet_to_python" not in actions
    assert "writer.insert_latex_dialog" not in actions
    assert "notebook.run_all" not in actions
    assert "notebook.stop" not in actions


def test_writer_hamburger_without_notebook_registry_omits_run_actions():
    rows = librepy_hamburger_actions(
        is_calc_doc=False,
        is_writer_doc=True,
        is_draw_doc=False,
        handler_lookup=_lookup,
        doc=object(),
    )
    actions = [a for _label, a, _icon in rows]
    assert "notebook.run_all" not in actions
    assert "notebook.run_from_here" not in actions
    assert "notebook.stop" not in actions
    assert "scripting.reset_python_session" in actions


def test_writer_hamburger_includes_notebook_run_actions():
    with patch("plugin.notebook.cell_registry.load_registry", return_value=object()):
        rows = librepy_hamburger_actions(
            is_calc_doc=False,
            is_writer_doc=True,
            is_draw_doc=False,
            handler_lookup=_lookup,
            doc=object(),
        )
    actions = [a for _label, a, _icon in rows]
    assert "notebook.run_all" in actions
    assert "notebook.run_from_here" in actions
    assert "notebook.stop" in actions
    assert actions.index("scripting.reset_python_session") < actions.index("notebook.run_all")


def test_calc_hamburger_omits_notebook_even_with_registry():
    with patch("plugin.notebook.cell_registry.load_registry", return_value=object()):
        rows = librepy_hamburger_actions(
            is_calc_doc=True,
            is_writer_doc=False,
            is_draw_doc=False,
            handler_lookup=_lookup,
            doc=object(),
        )
    actions = [a for _label, a, _icon in rows]
    assert "notebook.run_all" not in actions
    assert "notebook.run_from_here" not in actions
    assert "notebook.stop" not in actions


def test_skips_unregistered_handlers():
    rows = librepy_hamburger_actions(
        is_calc_doc=True,
        is_writer_doc=False,
        is_draw_doc=False,
        handler_lookup=lambda _a: None,
    )
    assert rows == []


def test_invoke_zero_arg_handler():
    called = []

    def handler():
        called.append(True)

    invoke_action_handler(handler, frame=object())
    assert called == [True]


def test_invoke_frame_handler():
    called = []

    def handler(frame):
        called.append(frame)

    frame = object()
    invoke_action_handler(handler, frame)
    assert called == [frame]


def test_show_hamburger_uses_librepy_command_prefix():
    ctx = MagicMock()
    smgr = MagicMock()
    popup = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = popup
    popup.execute.return_value = 0
    button_ctrl = MagicMock()
    button_ctrl.getPosSize.return_value = MagicMock(X=58, Y=2, Width=16, Height=12)
    button_ctrl.getPeer.return_value = MagicMock()
    frame = MagicMock()

    with (
        patch("plugin.librepy.sidebar_menus.is_calc", return_value=True),
        patch("plugin.librepy.sidebar_menus.is_writer", return_value=False),
        patch("plugin.librepy.sidebar_menus.is_draw", return_value=False),
        patch("plugin.librepy.sidebar_menus.get_action_handler", side_effect=_lookup),
        patch(
            "plugin.librepy.sidebar_menus.command_prefix_for_ctx",
            return_value=EXTENSION_ID_LIBREPY + ":",
        ),
    ):
        show_librepy_hamburger_menu(ctx, frame, button_ctrl)

    commands = [c.args[1] for c in popup.setCommand.call_args_list]
    assert commands
    assert all(c.startswith(EXTENSION_ID_LIBREPY + ":") for c in commands)
    assert not any("embeddings.search" in c for c in commands)


def test_writeragent_python_hamburger_uses_chat_menu():
    ctx = MagicMock()
    frame = MagicMock()
    button = MagicMock()
    with (
        patch(
            "plugin.framework.uno_context.resolve_package_extension_id",
            return_value="org.extension.writeragent",
        ),
        patch("plugin.chatbot.hamburger_menu.show_hamburger_menu") as chat_menu,
        patch("plugin.librepy.sidebar_menus.show_librepy_hamburger_menu") as core_menu,
    ):
        show_python_sidebar_hamburger(ctx, frame, button)
    chat_menu.assert_called_once_with(ctx, frame, button)
    core_menu.assert_not_called()


def test_librepy_python_hamburger_stays_subset():
    ctx = MagicMock()
    frame = MagicMock()
    button = MagicMock()
    with (
        patch(
            "plugin.framework.uno_context.resolve_package_extension_id",
            return_value="org.extension.librepy",
        ),
        patch("plugin.librepy.sidebar_menus.show_librepy_hamburger_menu") as core_menu,
    ):
        show_python_sidebar_hamburger(ctx, frame, button)
    core_menu.assert_called_once_with(ctx, frame, button)


def test_wire_header_no_search_control():
    ctx = MagicMock()
    frame = MagicMock()
    controls = {
        "btn_hdr_settings": MagicMock(),
        "btn_python": MagicMock(),
        "btn_latex": MagicMock(),
        "btn_hamburger": MagicMock(),
    }
    for ctrl in controls.values():
        ctrl.getModel.return_value = MagicMock()

    with patch("plugin.framework.uno_context.get_extension_url", return_value="file:///ext"):
        wire_sidebar_header_buttons(ctx, frame, controls, calc_doc=True)

    assert "btn_search" not in controls
    controls["btn_python"].addActionListener.assert_called()
    controls["btn_hamburger"].addActionListener.assert_called()
    latex_url = controls["btn_latex"].getModel.return_value.ImageURL
    assert "/assets/python_cell_" in latex_url
    assert latex_url.endswith(".png")
    settings_url = controls["btn_hdr_settings"].getModel.return_value.ImageURL
    assert "/assets/gear_" in settings_url


def test_menu_icon_filesystem_paths_include_oxt_and_checkout_layouts():
    from plugin.framework.uno_context import menu_icon_filesystem_paths

    paths = menu_icon_filesystem_paths("python_32.png")
    posix = [p.replace("\\", "/") for p in paths]
    assert posix[0].endswith("assets/python_32.png")
    assert "extension/assets" not in posix[0]
    assert posix[1].endswith("extension/assets/python_32.png")
