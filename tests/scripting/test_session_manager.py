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
    """With no focused window, reset still prefers an open Calc workbook."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    mock_calc = MagicMock()
    mock_writer = MagicMock()
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None

    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.scripting.session_manager._calc_document", return_value=mock_calc),
        patch("plugin.scripting.session_manager._writer_document", return_value=mock_writer),
        patch("plugin.scripting.session_manager._reset_calc_python_sessions") as mock_reset_calc,
    ):
        session_manager.reset_workbook_python_session(ctx)
        mock_reset_calc.assert_called_once_with(ctx, mock_calc)


def test_reset_workbook_python_session_uses_focused_writer() -> None:
    """Menubar reset follows the focused window instead of a background Calc workbook."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    writer = MagicMock()
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = writer

    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.scripting.session_manager.is_writer", side_effect=lambda doc: doc is writer),
        patch("plugin.scripting.session_manager.is_draw", return_value=False),
        patch("plugin.scripting.session_manager.is_calc", return_value=False),
        patch("plugin.scripting.session_manager._has_notebook_registry", return_value=False),
        patch("plugin.scripting.session_manager._reset_rps_python_session") as mock_rps,
        patch("plugin.scripting.session_manager._reset_calc_python_sessions") as mock_calc,
    ):
        session_manager.reset_workbook_python_session(ctx)
    mock_rps.assert_called_once_with(ctx, writer)
    mock_calc.assert_not_called()


def test_document_for_script_session_matches_ppt_master_url() -> None:
    from unittest.mock import MagicMock, patch

    focused = MagicMock()
    focused.getURL.return_value = "file:///focused.odp"
    deck = MagicMock()
    deck.getURL.return_value = "file:///deck-b.odp"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = [focused, deck]
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop = MagicMock()
    desktop.getComponents.return_value = comps
    focused_only = MagicMock()
    focused_only.hasMoreElements.side_effect = [True, False]
    focused_only.nextElement.return_value = focused
    focused_comps = MagicMock()
    focused_comps.createEnumeration.return_value = focused_only
    focused_desktop = MagicMock()
    focused_desktop.getComponents.return_value = focused_comps
    with patch("plugin.scripting.session_manager.get_desktop", return_value=desktop):
        found = session_manager.document_for_script_session(MagicMock(), "ppt_master:file:///deck-b.odp")
    with patch("plugin.scripting.session_manager.get_desktop", return_value=focused_desktop):
        missing = session_manager.document_for_script_session(MagicMock(), "ppt_master:active")
    assert found is deck
    assert missing is None


def test_document_for_script_session_matches_url_not_focused() -> None:
    from unittest.mock import MagicMock, patch

    focused = MagicMock()
    focused.getURL.return_value = "file:///focused.ods"
    other = MagicMock()
    other.getURL.return_value = "file:///other.ods"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = [focused, other]
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop = MagicMock()
    desktop.getComponents.return_value = comps
    with patch("plugin.scripting.session_manager.get_desktop", return_value=desktop):
        found = session_manager.document_for_script_session(MagicMock(), "calc:file:///other.ods")
    assert found is other


def test_reset_reports_init_reseed_failure() -> None:
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.scripting.session_manager._calc_document", return_value=doc),
        patch("plugin.scripting.session_manager.calc_workbook_base_session_id", return_value="calc:wb"),
        patch("plugin.scripting.session_manager.reset_python_session", return_value={"status": "ok"}),
        patch("plugin.scripting.document_scripts.build_python_eval_init_kwargs", return_value={"init_script": "FACTOR = 1", "init_session_id": "calc:wb:init", "init_script_hash": "h"}),
        patch("plugin.scripting.document_scripts.get_calc_init_script", return_value="FACTOR = 1"),
        patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
        patch("plugin.scripting.session_manager.record_active_calc_session"),
        patch("plugin.scripting.venv_worker.run_code_in_user_venv", return_value={"status": "error", "message": "init boom"}),
        patch("plugin.scripting.session_manager._msgbox") as msgbox,
    ):
        from plugin.scripting.session_manager import _reset_calc_python_sessions

        _reset_calc_python_sessions(ctx, doc)
    msgbox.assert_called_once()
    assert "init boom" in msgbox.call_args[0][1]


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


def test_cached_calc_document_unambiguous_and_cleared() -> None:
    """Off-main spill may pass through the UI-thread model when one session is recorded."""
    from plugin.tests.testing_utils import CalcDocStub

    session_manager.clear_active_calc_session()
    doc = CalcDocStub(url="file:///SpillTest.ods")
    try:
        session_manager.record_active_calc_session("calc:file:///SpillTest.ods", doc=doc)
        assert session_manager.get_cached_calc_document() is doc
        session_manager.record_active_calc_session("calc:file:///other.ods")
        assert session_manager.get_cached_calc_document() is None
    finally:
        session_manager.clear_active_calc_session()
    assert session_manager.get_cached_calc_document() is None


def test_cached_calc_document_isolated_zero_sessions() -> None:
    """Isolated (no recorded id) still returns the last UI-thread model."""
    from plugin.tests.testing_utils import CalcDocStub

    session_manager.clear_active_calc_session()
    doc = CalcDocStub(url="file:///isolated.ods")
    try:
        session_manager.record_active_calc_document(doc)
        assert session_manager.recorded_calc_session_count() == 0
        assert session_manager.get_cached_calc_document() is doc
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


def test_scoped_dir_from_calc_session_id_uses_file_url(tmp_path: Path) -> None:
    workbook = tmp_path / "python_showcase_demo.xlsx"
    workbook.write_bytes(b"pk")
    assert session_manager.scoped_dir_from_calc_session_id(f"calc:{workbook.as_uri()}") == str(
        tmp_path
    )
    assert session_manager.scoped_dir_from_calc_session_id("calc:unsaved:abc") is None
    assert session_manager.scoped_dir_from_calc_session_id(None) is None


def test_record_active_calc_session_caches_scoped_dir(tmp_path: Path) -> None:
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "demo.ods"
    second = second_dir / "other.ods"
    first.write_bytes(b"PK")
    second.write_bytes(b"PK")
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session(f"calc:{first.as_uri()}")
        assert session_manager.get_cached_calc_scoped_dir() == str(first_dir)
        session_manager.record_active_calc_session(f"calc:{second.as_uri()}")
        # Two recorded sessions: do not guess which folder belongs to =PY().
        assert session_manager.recorded_calc_session_count() == 2
        assert session_manager.get_cached_calc_scoped_dir() is None
    finally:
        session_manager.clear_active_calc_session()


def test_off_main_two_workbooks_do_not_share_a_kernel() -> None:
    from unittest.mock import MagicMock, patch

    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session("calc:file:///a.ods")
        session_manager.record_active_calc_session("calc:file:///b.ods")
        assert session_manager.off_main_calc_session_is_unambiguous() is False
        with (
            patch.object(session_manager, "python_session_mode", return_value="shared"),
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        ):
            assert session_manager.workbook_session_id(MagicMock(), doc=None) is None
    finally:
        session_manager.clear_active_calc_session()


def test_clear_active_calc_session_logs_addin_cache_failure(caplog) -> None:
    import logging
    from unittest.mock import patch

    with (
        patch(
            "plugin.calc.python.function.clear_python_addin_cache",
            side_effect=RuntimeError("cache"),
        ),
        caplog.at_level(logging.DEBUG, logger="plugin.scripting.session_manager"),
    ):
        session_manager.clear_active_calc_session()
    assert "clear_python_addin_cache failed" in caplog.text


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


def test_workbook_session_id_non_calc_does_not_record() -> None:
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    writer = MagicMock()
    session_manager.clear_active_calc_session()
    try:
        with (
            patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
            patch("plugin.scripting.session_manager.is_calc", return_value=False),
            patch("plugin.scripting.session_manager.calc_workbook_base_session_id") as mint,
        ):
            assert session_manager.workbook_session_id(ctx, doc=writer) is None
        mint.assert_not_called()
        assert session_manager.recorded_calc_session_count() == 0
    finally:
        session_manager.clear_active_calc_session()


def test_empty_init_kwargs_clears_cached_calc_init() -> None:
    """{} is a cleared workbook init. Omitting kwargs must leave the cache alone."""
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session(
            "calc:file:///a.ods", {"init_script": "A = 1"}
        )
        assert session_manager.get_cached_calc_init_kwargs().get("init_script") == "A = 1"
        session_manager.record_active_calc_session(None, {})
        assert session_manager.get_cached_calc_init_kwargs() == {}
        session_manager.record_active_calc_session(
            "calc:file:///a.ods", {"init_script": "A = 1"}
        )
        session_manager.record_active_calc_session("calc:file:///a.ods")
        assert session_manager.get_cached_calc_init_kwargs().get("init_script") == "A = 1"
    finally:
        session_manager.clear_active_calc_session()


def test_closing_one_workbook_keeps_the_other_document() -> None:
    from plugin.tests.testing_utils import CalcDocStub

    session_manager.clear_active_calc_session()
    first = CalcDocStub(url="file:///a.ods")
    second = CalcDocStub(url="file:///b.ods")
    try:
        session_manager.record_active_calc_session("calc:file:///a.ods", doc=first, init_kwargs={"init_script": "A = 1"})
        session_manager.record_active_calc_session("calc:file:///b.ods", doc=second, init_kwargs={"init_script": "B = 2"})
        session_manager.clear_active_calc_session("calc:file:///b.ods")
        assert session_manager.recorded_calc_session_count() == 1
        assert session_manager.get_cached_calc_session_id() == "calc:file:///a.ods"
        assert session_manager.get_cached_calc_document() is first
        assert session_manager.get_cached_calc_init_kwargs().get("init_script") == "A = 1"
    finally:
        session_manager.clear_active_calc_session()


def test_focus_switch_without_kwargs_does_not_keep_other_init() -> None:
    """calc_workbook_base_session_id records with init_kwargs=None."""
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session(
            "calc:file:///a.ods", {"init_script": "A = 1"}
        )
        session_manager.record_active_calc_session(
            "calc:file:///b.ods", {"init_script": "B = 2"}
        )
        session_manager.record_active_calc_session("calc:file:///a.ods")
        assert session_manager.get_cached_calc_init_kwargs().get("init_script") == "A = 1"
        # B becomes last-active the way workbook focus does: no kwargs.
        session_manager.record_active_calc_session("calc:file:///b.ods")
        assert session_manager.get_cached_calc_session_id() == "calc:file:///b.ods"
        assert session_manager.get_cached_calc_init_kwargs().get("init_script") == "B = 2"
        session_manager.clear_active_calc_session("calc:file:///a.ods")
        assert session_manager.recorded_calc_session_count() == 1
        assert session_manager.off_main_calc_session_is_unambiguous()
        assert session_manager.get_cached_calc_session_id() == "calc:file:///b.ods"
        assert session_manager.get_cached_calc_init_kwargs().get("init_script") == "B = 2"
    finally:
        session_manager.clear_active_calc_session()


def test_cleared_init_is_not_restored_on_focus_return() -> None:
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session(
            "calc:file:///a.ods", {"init_script": "A = 1"}
        )
        session_manager.record_active_calc_session(
            "calc:file:///b.ods", {"init_script": "B = 2"}
        )
        session_manager.record_active_calc_session("calc:file:///a.ods")
        session_manager.record_active_calc_session(None, {})
        session_manager.record_active_calc_session("calc:file:///b.ods")
        session_manager.record_active_calc_session("calc:file:///a.ods")
        assert session_manager.get_cached_calc_init_kwargs() == {}
    finally:
        session_manager.clear_active_calc_session()


def test_closing_other_workbook_drops_its_init_when_it_still_owns_the_cache() -> None:
    session_manager.clear_active_calc_session()
    try:
        session_manager.record_active_calc_session(
            "calc:file:///a.ods", {"init_script": "A = 1"}
        )
        session_manager.record_active_calc_session("calc:file:///b.ods")
        assert session_manager.get_cached_calc_init_kwargs() == {}
        session_manager.clear_active_calc_session("calc:file:///a.ods")
        assert session_manager.get_cached_calc_session_id() == "calc:file:///b.ods"
        assert session_manager.get_cached_calc_init_kwargs() == {}
    finally:
        session_manager.clear_active_calc_session()





