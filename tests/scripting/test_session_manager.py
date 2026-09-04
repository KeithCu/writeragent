# SPDX-License-Identifier: GPL-3.0-or-later
"""Import-closure tests for plugin.scripting.session_manager."""

from __future__ import annotations

import ast
from pathlib import Path

import plugin.scripting.session_manager as session_manager


def test_session_manager_module_avoids_document_helpers_and_dialogs() -> None:
    """workbook_session_id is on the =PY() path; Reset Session may lazy-load dialogs."""
    tree = ast.parse(Path(session_manager.__file__).read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
    assert "plugin.doc.document_helpers" not in mods
    assert "plugin.calc.analyzer" not in mods
    assert "plugin.chatbot.dialogs" not in mods
    assert "plugin.doc.doc_type" in mods
    assert "plugin.doc.udprops" in mods


def test_msgbox_uses_product_display_name() -> None:
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    with (
        patch("plugin.framework.uno_context.product_display_name", return_value="LibrePy") as name,
        patch("plugin.chatbot.dialogs.msgbox") as box,
    ):
        session_manager._msgbox(ctx, "hello")
    name.assert_called_once_with(ctx)
    box.assert_called_once_with(ctx, "LibrePy", "hello")


def test_find_document_by_predicate_fallback() -> None:
    """When getCurrentComponent() is None (e.g. headless), fallback to getComponents enumeration."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    mock_desktop = MagicMock()
    mock_desktop.getCurrentComponent.return_value = None

    mock_calc_doc = MagicMock()
    mock_calc_doc.getSheets.return_value = MagicMock()

    mock_elem = MagicMock()
    mock_elem.getURL.return_value = "file:///test.ods"
    mock_elem.getSheets.return_value = MagicMock()

    mock_comps = MagicMock()
    mock_enum = MagicMock()
    mock_enum.hasMoreElements.side_effect = [True, False]
    mock_enum.nextElement.return_value = mock_elem
    mock_comps.createEnumeration.return_value = mock_enum
    mock_desktop.getComponents.return_value = mock_comps

    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=mock_desktop),
        patch("plugin.scripting.session_manager.is_calc", return_value=True),
    ):
        doc = session_manager._calc_document(ctx)
        assert doc is not None


def test_workbook_session_key_unsaved_uses_uuid_not_id() -> None:
    from unittest.mock import MagicMock, patch
    import uuid as uuid_mod

    mock_doc = MagicMock()
    mock_doc.getURL.return_value = ""

    with (
        patch("plugin.scripting.session_manager.get_document_property", return_value=""),
        patch("plugin.scripting.session_manager.set_document_property", side_effect=RuntimeError("no props")),
    ):
        key = session_manager._workbook_session_key(mock_doc)
    assert key.startswith("unsaved:")
    rest = key[len("unsaved:") :]
    uuid_mod.UUID(rest)
    assert rest != str(id(mock_doc))


def test_workbook_session_id_with_explicit_doc() -> None:
    """Explicit doc argument avoids desktop lookups and works regardless of thread affinity."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    mock_doc = MagicMock()
    mock_doc.getURL.return_value = "file:///custom_sheet.ods"

    try:
        with (
            patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
            patch("plugin.scripting.session_manager.is_calc", return_value=True),
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        ):
            sid = session_manager.workbook_session_id(ctx, doc=mock_doc)
            assert sid == "calc:file:///custom_sheet.ods"
    finally:
        session_manager.clear_active_calc_session()


def test_reset_workbook_python_session_prefers_calc() -> None:
    """When both Calc and Writer are open/available, reset routes to Calc first."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    mock_calc = MagicMock()
    mock_writer = MagicMock()

    with (
        patch("plugin.scripting.session_manager._calc_document", return_value=mock_calc),
        patch("plugin.scripting.session_manager._writer_document", return_value=mock_writer),
        patch("plugin.scripting.session_manager._reset_calc_python_sessions") as mock_reset_calc,
    ):
        session_manager.reset_workbook_python_session(ctx)
        mock_reset_calc.assert_called_once_with(ctx, mock_calc)


def test_workbook_session_id_off_main_ambiguous_when_two_workbooks() -> None:
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    session_manager.clear_active_calc_session()
    session_manager.record_active_calc_session("calc:file:///a.ods")
    session_manager.record_active_calc_session("calc:file:///b.ods")
    try:
        with (
            patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        ):
            assert session_manager.workbook_session_id(ctx, doc=None) is None
    finally:
        session_manager.clear_active_calc_session()


def test_workbook_session_id_uses_cached_session_off_main() -> None:
    """Off the main thread without explicit doc, workbook_session_id returns UI-cached session id."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    session_manager.clear_active_calc_session()
    session_manager.record_active_calc_session("calc:file:///cached_sheet.ods")

    try:
        with (
            patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        ):
            sid = session_manager.workbook_session_id(ctx, doc=None)
            assert sid == "calc:file:///cached_sheet.ods"
    finally:
        session_manager.clear_active_calc_session()


def test_record_active_calc_session_ignores_opencl_probe() -> None:
    """Headless soffice opens cl-test.ods — must not make leftover recorded=2."""
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session(
            "calc:file:///usr/lib/libreoffice/program/../program/opencl/cl-test.ods"
        )
        assert session_manager.recorded_calc_session_count() == 0
        session_manager.record_active_calc_session("calc:e058adf2-8a4e-4556-a488-589b4e93e983")
        session_manager.record_active_calc_session(
            "calc:file:///usr/lib/libreoffice/program/../program/opencl/cl-test.ods"
        )
        assert session_manager.recorded_calc_session_count() == 1
        assert session_manager.off_main_calc_session_is_unambiguous() is True
    finally:
        session_manager.clear_active_calc_session()


def test_record_active_calc_session_replaces_prior_unsaved() -> None:
    """Two unsaved: fallbacks must not leave recorded=2 (leftover Isolated)."""
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session("calc:unsaved:first")
        session_manager.record_active_calc_session("calc:unsaved:second")
        assert session_manager.recorded_calc_session_count() == 1
        assert session_manager.get_cached_calc_session_id() == "calc:unsaved:second"
        assert session_manager.off_main_calc_session_is_unambiguous() is True
        assert session_manager.recorded_calc_session_ids() == ("calc:unsaved:second",)
    finally:
        session_manager.clear_active_calc_session()


def test_record_active_calc_session_drops_ephemeral_unsaved_when_durable() -> None:
    """OnOpen unsaved: fallback must not poison unambiguous after UDProp sticks."""
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session("calc:unsaved:early-open")
        assert session_manager.recorded_calc_session_count() == 1
        assert session_manager.off_main_calc_session_is_unambiguous() is True

        session_manager.record_active_calc_session("calc:persisted-uuid")
        assert session_manager.recorded_calc_session_count() == 1
        assert session_manager.get_cached_calc_session_id() == "calc:persisted-uuid"
        assert session_manager.off_main_calc_session_is_unambiguous() is True
    finally:
        session_manager.clear_active_calc_session()


def test_workbook_session_id_resilient_when_is_calc_fails() -> None:
    """If is_calc throws, workbook_session_id falls back to doc URL directly."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    mock_doc = MagicMock()
    mock_doc.getURL.return_value = "file:///fallback_sheet.ods"

    try:
        with (
            patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
            patch("plugin.scripting.session_manager.is_calc", side_effect=RuntimeError("UNO thread error")),
        ):
            sid = session_manager.workbook_session_id(ctx, doc=mock_doc)
            assert sid == "calc:file:///fallback_sheet.ods"
    finally:
        session_manager.clear_active_calc_session()





