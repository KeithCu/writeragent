# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the hamburger popup menu."""

import contextlib
from typing import Any
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from plugin.chatbot.hamburger_menu import show_hamburger_menu
from plugin.notebook.cell_registry import NotebookDocState, new_code_cell_entry


@contextlib.contextmanager
def _doc_flags(*, writer: bool = False, calc: bool = False, draw: bool = False):
    with (
        patch("plugin.chatbot.hamburger_menu.is_writer", return_value=writer),
        patch("plugin.chatbot.hamburger_menu.is_calc", return_value=calc),
        patch("plugin.chatbot.hamburger_menu.is_draw", return_value=draw),
    ):
        yield


@pytest.fixture
def popup_menu():
    ctx = MagicMock()
    smgr = MagicMock()
    popup = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = popup
    popup.execute.return_value = 0
    button_ctrl = MagicMock()
    button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
    button_ctrl.getPeer.return_value = MagicMock()
    return ctx, popup, button_ctrl


class TestHamburgerMenu:
    def test_show_hamburger_menu_none_ctx(self):
        # Should cleanly do nothing when ctx is None
        show_hamburger_menu(None, None, MagicMock())

    def test_show_hamburger_menu_executes_selected_action(self, popup_menu):
        ctx, popup, button_ctrl = popup_menu
        # Simulate user selecting item ID 1 (Extend Selection)
        popup.execute.return_value = 1

        frame = MagicMock()
        mock_handler = create_autospec(lambda frame=None: None)

        with (
            patch("plugin.chatbot.hamburger_menu.get_action_handler", return_value=mock_handler) as mock_get_handler,
            _doc_flags(writer=True),
            patch("plugin.app.dispatch_command") as dispatch,
        ):
            show_hamburger_menu(ctx, frame, button_ctrl)
        mock_get_handler.assert_called_with("chatbot.extend_selection")
        mock_handler.assert_called_once_with(frame)
        dispatch.assert_not_called()

    @pytest.mark.parametrize("action", ["mcp.toggle_server", "mcp.server_status"])
    def test_unregistered_mcp_items_use_command_dispatch(self, popup_menu, action):
        """Toggle MCP and MCP status have no register_action_handler.

        get_action_handler returned None and invoke_action_handler returned
        without calling anything. The toolbar reaches McpModule.on_action
        through _dispatch_command. These items must use that path.
        """
        ctx, popup, button_ctrl = popup_menu
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
            _doc_flags(writer=True),
            patch("plugin.app.dispatch_command") as dispatch,
            patch("plugin.librepy.sidebar_menus.invoke_action_handler") as invoke,
        ):
            show_hamburger_menu(ctx, frame, button_ctrl)
        get_handler.assert_called_once_with(action)
        dispatch.assert_called_once_with(action)
        invoke.assert_not_called()

    def test_plain_writer_hamburger_has_gear_icon_and_no_notebook_items(self, popup_menu):
        ctx, popup, button_ctrl = popup_menu
        graphic = MagicMock()

        with (
            _doc_flags(writer=True),
            patch("plugin.librepy.sidebar_menus.load_menu_graphic", return_value=graphic) as load_g,
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

    def test_writer_hamburger_with_notebook_registry_shows_run_actions(self, popup_menu):
        ctx, popup, button_ctrl = popup_menu

        with (
            _doc_flags(writer=True),
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

    def test_calc_hamburger_omits_notebook_run_actions(self, popup_menu):
        ctx, popup, button_ctrl = popup_menu

        with (
            _doc_flags(calc=True),
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

    def test_debug_submenu_omitted_when_tests_unbundled(self, popup_menu):
        ctx, popup, button_ctrl = popup_menu

        with (
            patch("plugin.app.tests_bundled", return_value=False),
            _doc_flags(writer=True),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Debug" not in labels
        assert "Run format tests" not in labels
        assert "Evaluation Dashboard" not in labels

    def test_debug_submenu_included_when_tests_bundled(self, popup_menu):
        ctx, popup, button_ctrl = popup_menu

        with (
            patch("plugin.app.tests_bundled", return_value=True),
            _doc_flags(writer=True),
        ):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        labels = [c.args[1] for c in popup.insertItem.call_args_list]
        assert "Debug" in labels
        assert "Run format tests" in labels

    @pytest.mark.parametrize("doc_kind", ["draw", "writer"])
    def test_hamburger_includes_extend_and_edit_selection(self, popup_menu, doc_kind: str):
        ctx, popup, button_ctrl = popup_menu
        commands: set[str] = set()
        popup.setCommand.side_effect = lambda _nid, cmd: commands.add(cmd)

        with _doc_flags(writer=(doc_kind == "writer"), draw=(doc_kind == "draw")):
            show_hamburger_menu(ctx, MagicMock(), button_ctrl)

        assert "org.extension.writeragent:chatbot.extend_selection" in commands
        assert "org.extension.writeragent:chatbot.edit_selection" in commands

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

        flag_sets = (
            (True, False, False),
            (False, True, False),
            (False, False, True),
        )
        union_commands: set[str] = set()
        for is_writer_doc, is_calc_doc, is_draw_doc in flag_sets:
            commands.clear()
            with (
                patch("plugin.app.tests_bundled", return_value=True),
                patch("plugin.app.get_menu_text", return_value="Toggle MCP Server"),
                patch("plugin.app.get_menu_icon", return_value="stopped"),
                patch("plugin.chatbot.hamburger_menu.is_writer", return_value=is_writer_doc),
                patch("plugin.chatbot.hamburger_menu.is_calc", return_value=is_calc_doc),
                patch("plugin.chatbot.hamburger_menu.is_draw", return_value=is_draw_doc),
                patch("plugin.librepy.sidebar_menus.document_has_notebook_registry", return_value=is_writer_doc),
            ):
                show_hamburger_menu(ctx, MagicMock(), button_ctrl)
            if is_draw_doc and not is_writer_doc and not is_calc_doc:
                assert "org.extension.writeragent:chatbot.extend_selection" in commands
                assert "org.extension.writeragent:chatbot.edit_selection" in commands
            union_commands |= commands

        commands = union_commands

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


def _make_hamburger_mocks() -> tuple[MagicMock, MagicMock, list[MagicMock]]:
    ctx = MagicMock()
    smgr = MagicMock()
    created_menus: list[MagicMock] = []

    def _create_popup(*_args: Any) -> MagicMock:
        menu = MagicMock()
        if not created_menus:
            menu.execute.return_value = 0
        created_menus.append(menu)
        return menu

    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.side_effect = _create_popup
    button_ctrl = MagicMock()
    button_ctrl.getPosSize.return_value = MagicMock(X=76, Y=2, Width=16, Height=12)
    button_ctrl.getPeer.return_value = MagicMock()
    return ctx, button_ctrl, created_menus


def test_mcp_status_failure_still_builds_and_executes_hamburger():
    ctx, button_ctrl, created_menus = _make_hamburger_mocks()

    with (
        patch("plugin.app.get_menu_text", side_effect=RuntimeError("mcp text failure")),
        patch("plugin.app.get_menu_icon", side_effect=RuntimeError("mcp icon failure")),
        patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
        patch("plugin.chatbot.hamburger_menu.is_calc", return_value=False),
        patch("plugin.chatbot.hamburger_menu.is_draw", return_value=False),
    ):
        show_hamburger_menu(ctx, MagicMock(), button_ctrl)

    popup = created_menus[0]
    inserted_labels = [call.args[1] for call in popup.insertItem.call_args_list]
    assert "Settings" in inserted_labels
    assert "Report bug..." in inserted_labels
    popup.execute.assert_called_once()


def test_static_menu_titles_match_hamburger_and_addons_xcu():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from plugin._manifest import MENU_TITLES, MODULES
    from plugin.librepy.sidebar_menus import _librepy_hamburger_specs

    manifest_titles: dict[str, str] = dict(MENU_TITLES)
    for mod in MODULES:
        mod_name = mod.get("name", "")
        for act, title in mod.get("action_titles", {}).items():
            manifest_titles[f"{mod_name}.{act}"] = title

    root_dir = Path(__file__).resolve().parents[2]
    addons_path = root_dir / "extension" / "Addons.xcu"
    if not addons_path.exists():
        # The release_verify bundle flattens extension/ into the bundle root.
        addons_path = root_dir / "Addons.xcu"
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

    # Collect static items directly from show_hamburger_menu (excluding dynamic MCP items).
    ctx, button_ctrl, created_menus = _make_hamburger_mocks()

    with (
        patch("plugin.chatbot.hamburger_menu.is_writer", return_value=True),
        patch("plugin.chatbot.hamburger_menu.is_calc", return_value=True),
        patch("plugin.chatbot.hamburger_menu.is_draw", return_value=True),
        patch("plugin.librepy.sidebar_menus.document_has_notebook_registry", return_value=True),
        patch("plugin.app.tests_bundled", return_value=True),
        patch("plugin.app.get_menu_text", return_value="Toggle MCP Server"),
        patch("plugin.app.get_menu_icon", return_value="stopped"),
    ):
        show_hamburger_menu(ctx, MagicMock(), button_ctrl)

    assert created_menus and created_menus[0].execute.called

    hamburger_static_items: dict[str, str] = {}
    for menu in created_menus:
        labels_by_id = {call.args[0]: call.args[1] for call in menu.insertItem.call_args_list}
        for call in menu.setCommand.call_args_list:
            item_id, command = call.args[0], call.args[1]
            if command.startswith("org.extension.writeragent:"):
                action = command.split("org.extension.writeragent:", 1)[1]
                if action not in ("mcp.toggle_server", "mcp.server_status"):
                    hamburger_static_items[action] = labels_by_id[item_id]

    assert hamburger_static_items

    for action, msgid in hamburger_static_items.items():
        assert msgid == manifest_titles[action]
        if action not in ("notebook.run_all", "notebook.run_from_here", "notebook.stop"):
            assert msgid == addons_titles[action]

    for label, action, _icon, _when in _librepy_hamburger_specs():
        assert label == manifest_titles[action]
        if action not in ("notebook.run_all", "notebook.run_from_here", "notebook.stop"):
            assert label == addons_titles[action]



