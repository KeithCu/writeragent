"""Import-graph ownership tests for the sidebar factory (no UNO import)."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FACTORY = _REPO_ROOT / "plugin" / "chatbot" / "panel_factory.py"
_DIALOG_VIEWS = _REPO_ROOT / "plugin" / "chatbot" / "dialog_views.py"
_SEND_HANDLERS = _REPO_ROOT / "plugin" / "chatbot" / "send_handlers.py"
_TOOL_LOOP = _REPO_ROOT / "plugin" / "chatbot" / "tool_loop.py"
_TOOL_LOOP_ACTIONS = _REPO_ROOT / "plugin" / "chatbot" / "tool_loop_actions.py"


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            for alias in node.names:
                names.add(alias.name)
                if module:
                    names.add("%s.%s" % (module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def _calls_name(path: Path, func_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == func_name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == func_name:
            return True
    return False


def test_panel_factory_does_not_import_or_call_chat_context_builder():
    """Factory wires XDL/controls. Document context is ChatSession / send / tool_loop."""
    imported = _imported_names(_FACTORY)
    assert "get_document_context_for_chat" not in imported
    assert "plugin.doc.document_helpers.get_document_context_for_chat" not in imported
    assert not _calls_name(_FACTORY, "get_document_context_for_chat")


def test_panel_factory_mode_switch_delegates_context_refresh_to_session():
    src = _FACTORY.read_text(encoding="utf-8")
    assert "session.refresh_document_context(model, self.ctx)" in src
    assert "_refresh_doc_session_context" not in src


def test_dialog_views_does_not_import_tool_loop():
    imported = _imported_names(_DIALOG_VIEWS)
    assert "plugin.chatbot.tool_loop" not in imported
    assert "tool_loop" not in imported


def test_send_and_tool_loop_do_not_import_panel_factory():
    for path in (_SEND_HANDLERS, _TOOL_LOOP, _TOOL_LOOP_ACTIONS):
        imported = _imported_names(path)
        assert "plugin.chatbot.panel_factory" not in imported
        assert "panel_factory" not in imported


def test_send_and_tool_loop_refresh_via_session_not_builder():
    """Send / mid-loop refresh go through ChatSession.refresh_document_context."""
    for path in (_SEND_HANDLERS, _TOOL_LOOP, _TOOL_LOOP_ACTIONS):
        imported = _imported_names(path)
        assert "get_document_context_for_chat" not in imported
        assert "plugin.doc.document_helpers.get_document_context_for_chat" not in imported
        src = path.read_text(encoding="utf-8")
        assert "refresh_document_context" in src
