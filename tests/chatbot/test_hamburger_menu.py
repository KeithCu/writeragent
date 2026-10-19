# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the hamburger popup menu."""

from typing import Any
from unittest.mock import MagicMock, patch

from plugin.chatbot.hamburger_menu import show_hamburger_menu
from plugin.notebook.cell_registry import NotebookDocState, new_code_cell_entry


class TestHamburgerMenu:
    def test_show_hamburger_menu_none_ctx(self):
        # Should cleanly do nothing when ctx is None
        show_hamburger_menu(None, None, MagicMock())

    def test_show_hamburger_menu_executes_selected_action(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup

        # Simulate user selecting item ID 1 (Extend Selection)
        popup.execute.return_value = 1

        button_ctrl = MagicMock()
        pos_size = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPosSize.return_value = pos_size
        button_ctrl.getPeer.return_value = MagicMock()

        frame = MagicMock()
        mock_handler = MagicMock()

        with patch("plugin.chatbot.hamburger_menu.get_action_handler", return_value=mock_handler) as mock_get_handler:
            with patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True), patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False), patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False):
                with patch("plugin.main._dispatch_command") as dispatch:
                    show_hamburger_menu(ctx, frame, button_ctrl)
                mock_get_handler.assert_called_with("chatbot.extend_selection")
                mock_handler.assert_called_once_with(frame)
                dispatch.assert_not_called()

    def test_unregistered_mcp_items_use_command_dispatch(self):
        """Toggle MCP and MCP status have no register_action_handler.

        get_action_handler returned None and invoke_action_handler returned
        without calling anything. The toolbar reaches McpModule.on_action
        through _dispatch_command. These items must use that path.
        """
        for action in ("mcp.toggle_server", "mcp.server_status"):
            ctx = MagicMock()
            smgr = MagicMock()
            popup = MagicMock()
            ctx.getServiceManager.return_value = smgr
            smgr.createInstanceWithContext.return_value = popup
            button_ctrl = MagicMock()
            button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
            button_ctrl.getPeer.return_value = MagicMock()
            frame = MagicMock()

            def _choose(*_args, expected=action, menu=popup):
                suffix = ":" + expected
                for call in menu.setCommand.call_args_list:
                    command = call.args[1]
                    if isinstance(command, str) and command.endswith(suffix):
                        return call.args[0]
                raise AssertionError(expected)

            popup.execute.side_effect = _choose
            with (
                patch("plugin.chatbot.hamburger_menu.get_action_handler", return_value=None) as get_handler,
                patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
                patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
                patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
                patch("plugin.main._dispatch_command") as dispatch,
                patch("plugin.librepy.sidebar_menus.invoke_action_handler") as invoke,
            ):
                show_hamburger_menu(ctx, frame, button_ctrl)
            get_handler.assert_called_once_with(action)
            dispatch.assert_called_once_with(action)
            invoke.assert_not_called()

    def test_plain_writer_hamburger_has_gear_icon_and_no_notebook_items(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()
        graphic = MagicMock()

        with (
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
            patch("plugin.chatbot.hamburger_menu._load_graphic", return_value=graphic) as load_g,
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        assert any(
            isinstance(c.args[-1], str)
            and c.args[-1].startswith("gear_")
            and c.args[-1].endswith(".png")
            for c in load_g.call_args_list
        )
        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert not any("Jupyter" in str(label) for label in labels)
        # Plain Writer (no WriterAgentNotebookJson): specialized notebook chrome stays off.
        assert "Run All" not in labels
        assert "Run From Here" not in labels
        assert "Stop" not in labels
        assert "Reset Python Session" in labels
        assert "Insert LaTeX Math..." in labels

    def test_writer_hamburger_with_notebook_registry_shows_run_actions(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        with (
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
            patch(
                "plugin.notebook.cell_registry.load_registry",
                return_value=NotebookDocState(code_cells=[new_code_cell_entry(0, None, "nb_cell_0_code")]),
            ),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Run All" in labels
        assert "Run From Here" in labels
        assert "Stop" in labels
        assert labels.index("Reset Python Session") < labels.index("Run All")

    def test_calc_hamburger_omits_notebook_run_actions(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        with (
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
            patch(
                "plugin.notebook.cell_registry.load_registry",
                return_value=NotebookDocState(code_cells=[new_code_cell_entry(0, None, "nb_cell_0_code")]),
            ),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Run All" not in labels
        assert "Run From Here" not in labels
        assert "Stop" not in labels

    def test_show_hamburger_menu_calc_includes_calc_items(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0  # Cancelled

        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        frame = MagicMock()

        with patch("plugin.chatbot.hamburger_menu.is_writer", return_value=False), patch("plugin.chatbot.hamburger_menu.is_calc", return_value=True), patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False):
            show_hamburger_menu(ctx, frame, button_ctrl)
            # Verify insertItem was called on popup
            assert popup.insertItem.call_count > 5

    def test_debug_submenu_omitted_when_tests_unbundled(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        with (
            patch("plugin.main._tests_bundled", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Debug" not in labels
        assert "Run format tests" not in labels
        assert "Evaluation Dashboard" not in labels

    def test_debug_submenu_included_when_tests_bundled(self):
        ctx = MagicMock()
        smgr = MagicMock()
        popup = MagicMock()
        ctx.getServiceManager.return_value = smgr
        smgr.createInstanceWithContext.return_value = popup
        popup.execute.return_value = 0
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        with (
            patch("plugin.main._tests_bundled", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Debug" in labels
        assert "Run format tests" in labels

    def test_hamburger_menu_commands_parity_with_addons_xcu(self):
        import xml.etree.ElementTree as ET
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        addons_xcu = repo_root / "extension" / "Addons.xcu"
        root = ET.parse(addons_xcu).getroot()
        oor_name = "{http://openoffice.org/2001/registry}name"

        menubar_urls = set()
        for node in root.iter("node"):
            if node.get(oor_name) == "org.extension.writeragent.menubar":
                for item in node.iter("node"):
                    for prop in item.findall("prop"):
                        if prop.get(oor_name) == "URL":
                            val = prop.find("value")
                            if val is not None and val.text:
                                u = val.text.strip()
                                if u != "private:separator" and u != "org.extension.writeragent:main.NoOp":
                                    menubar_urls.add(u)

        ctx = MagicMock()
        smgr = MagicMock()
        ctx.getServiceManager.return_value = smgr
        commands: set[str] = set()

        def create_instance(name: str, *args: Any) -> Any:
            if name == "com.sun.star.awt.PopupMenu":
                m = MagicMock()
                m.execute.return_value = 0
                m.setCommand.side_effect = lambda _nid, cmd: commands.add(cmd)
                return m
            return MagicMock()

        smgr.createInstanceWithContext.side_effect = create_instance
        button_ctrl = MagicMock()
        button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
        button_ctrl.getPeer.return_value = MagicMock()

        with (
            patch("plugin.main._tests_bundled", return_value=True),
            patch("plugin.main.get_menu_text", return_value="Toggle MCP Server"),
            patch("plugin.main._get_menu_icon", return_value="stopped"),
            patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_calc", return_value=True),
            patch("plugin.chatbot.hamburger_menu.is_draw", return_value=True),
            patch("plugin.librepy.sidebar_menus.document_has_notebook_registry", return_value=True),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        # Every menubar URL in extension/Addons.xcu except main.NoOp is in that set.
        assert menubar_urls.issubset(commands)

        # Every collected command is either a menubar URL or one of notebook.run_all, notebook.run_from_here, notebook.stop.
        notebook_cmds = {
            "org.extension.writeragent:notebook.run_all",
            "org.extension.writeragent:notebook.run_from_here",
            "org.extension.writeragent:notebook.stop",
        }
        assert commands == menubar_urls | notebook_cmds

        # Toolbar-only review commands are not in the popup.
        for review_cmd in (
            "org.extension.writeragent:writer.review_prev",
            "org.extension.writeragent:writer.review_next",
            "org.extension.writeragent:writer.review_accept_all",
            "org.extension.writeragent:writer.review_reject_all",
        ):
            assert review_cmd not in commands


def test_mcp_status_failure_still_builds_and_executes_hamburger():
    ctx = MagicMock()
    smgr = MagicMock()
    popup = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = popup
    popup.execute.return_value = 0
    button_ctrl = MagicMock()
    button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
    button_ctrl.getPeer.return_value = MagicMock()

    with (
        patch("plugin.main.get_menu_text", side_effect=RuntimeError("mcp text failure")),
        patch("plugin.main._get_menu_icon", side_effect=RuntimeError("mcp icon failure")),
        patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
        patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
        patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
    ):
        show_hamburger_menu(ctx, MagicMock(), button_ctrl)

    inserted_labels = [call.args[1] for call in popup.insertItem.call_args_list]
    assert "Settings" in inserted_labels
    assert "Report bug..." in inserted_labels
    popup.execute.assert_called_once()


def test_static_menu_titles_match_hamburger_and_addons_xcu():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from plugin.main import _STATIC_MENU_TITLES
    from plugin.librepy.sidebar_menus import _librepy_hamburger_specs

    addons_path = Path(__file__).resolve().parents[2] / "extension" / "Addons.xcu"
    tree = ET.parse(addons_path)
    root = tree.getroot()
    oor_name = "{http://openoffice.org/2001/registry}name"

    addons_titles: dict[str, str] = {}
    for node in root.iter("node"):
        url_elem = None
        title_elem = None
        for prop in node.findall("prop"):
            prop_name = prop.get(oor_name)
            if prop_name == "URL":
                val = prop.find("value")
                if val is not None and val.text:
                    url_elem = val.text.strip()
            elif prop_name == "Title":
                val = prop.find("value")
                if val is not None and val.text:
                    title_elem = val.text.strip()
        if url_elem and title_elem and url_elem.startswith("org.extension.writeragent:"):
            action = url_elem.split("org.extension.writeragent:", 1)[1]
            addons_titles[action] = title_elem

    # Static actions in hamburger_menu.py and their msgids (excluding MCP dynamic popup strings)
    hamburger_static_items = {
        "chatbot.extend_selection": "Extend Selection",
        "chatbot.edit_selection": "Edit Selection",
        "scripting.run_python_dialog": "Run Python Script...",
        "scripting.edit_python_cell": "Edit Python in Cell...",
        "calc.convert_spreadsheet_to_python": "Convert Sheet to Python...",
        "embeddings.search_dialog": "Search Nearby Files...",
        "writer.insert_latex_dialog": "Insert LaTeX Math...",
        "textanalytics.open_dialog": "Text Analytics...",
        "main.settings": "Settings",
        "vision.open_settings": "Vision OCR Settings...",
        "scripting.reset_python_session": "Reset Python Session",
        "notebook.run_all": "Run All",
        "notebook.run_from_here": "Run From Here",
        "notebook.stop": "Stop",
        "main.RunFormatTests": "Run format tests",
        "main.RunCalcTests": "Run calc tests",
        "main.RunCalcIntegrationTests": "Run Calc API integration tests",
        "main.RunDrawTests": "Run draw tests",
        "main.EvaluationDashboard": "Evaluation Dashboard",
        "main.report_bug": "Report bug...",
    }

    for action, msgid in hamburger_static_items.items():
        assert msgid == _STATIC_MENU_TITLES[action]
        if action not in ("notebook.run_all", "notebook.run_from_here", "notebook.stop"):
            assert msgid == addons_titles[action]

    for label, action, _icon, _when in _librepy_hamburger_specs():
        assert label == _STATIC_MENU_TITLES[action]
        if action not in ("notebook.run_all", "notebook.run_from_here", "notebook.stop"):
            assert label == addons_titles[action]



