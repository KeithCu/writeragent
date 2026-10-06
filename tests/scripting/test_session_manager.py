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

    with (
        patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
        patch("plugin.scripting.session_manager.is_calc", return_value=True),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
    ):
        sid = session_manager.workbook_session_id(ctx, doc=mock_doc)
        assert sid == "calc:file:///custom_sheet.ods"


def test_workbook_session_id_without_doc_is_none() -> None:
    """No caller document means no shared session; the front window is not consulted."""
    from unittest.mock import MagicMock, patch

    with (
        patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
        patch("plugin.scripting.session_manager.get_desktop") as get_desktop,
    ):
        assert session_manager.workbook_session_id(MagicMock(), doc=None) is None
    get_desktop.assert_not_called()


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


def test_document_for_script_session_returns_pinned_doc_not_desktop() -> None:
    from unittest.mock import MagicMock, patch

    deck = MagicMock(name="deck")
    token = session_manager.pin_script_document(deck)
    assert token is not None and token.startswith("doc:")
    try:
        with patch("plugin.scripting.session_manager.get_desktop") as mock_desktop:
            found = session_manager.document_for_script_session(MagicMock(), token)
        mock_desktop.assert_not_called()
        assert found is deck
    finally:
        session_manager.release_script_document(token)
    with patch("plugin.scripting.session_manager.get_desktop") as mock_desktop:
        missing = session_manager.document_for_script_session(MagicMock(), token)
    mock_desktop.assert_not_called()
    assert missing is None
    assert session_manager.pin_script_document(None) is None


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


def test_document_for_script_session_wraps_model() -> None:
    from unittest.mock import MagicMock, patch

    deck = MagicMock()
    deck.getURL.return_value = "file:///deck.odp"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = deck
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop = MagicMock()
    desktop.getComponents.return_value = comps
    wrapped = MagicMock(name="wrapped")
    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.framework.thread_guard.guard_uno", return_value=wrapped) as mock_guard,
    ):
        found = session_manager.document_for_script_session(MagicMock(), "ppt_master:file:///deck.odp")
    mock_guard.assert_called_once_with(deck)
    assert found is wrapped


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
        patch("plugin.scripting.venv_worker.run_code_in_user_venv", return_value={"status": "error", "message": "init boom"}),
        patch("plugin.scripting.session_manager._msgbox") as msgbox,
    ):
        from plugin.scripting.session_manager import _reset_calc_python_sessions

        _reset_calc_python_sessions(ctx, doc)
    msgbox.assert_called_once()
    assert "init boom" in msgbox.call_args[0][1]


def test_workbook_session_id_resilient_when_is_calc_fails() -> None:
    """If is_calc throws, workbook_session_id falls back to doc URL directly."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    mock_doc = MagicMock()
    mock_doc.getURL.return_value = "file:///fallback_sheet.ods"

    with (
        patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
        patch("plugin.scripting.session_manager.is_calc", side_effect=RuntimeError("UNO thread error")),
    ):
        sid = session_manager.workbook_session_id(ctx, doc=mock_doc)
        assert sid == "calc:file:///fallback_sheet.ods"


def test_workbook_session_id_non_calc_does_not_record() -> None:
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    writer = MagicMock()
    with (
        patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
        patch("plugin.scripting.session_manager.is_calc", return_value=False),
        patch("plugin.scripting.session_manager.calc_workbook_base_session_id") as mint,
    ):
        assert session_manager.workbook_session_id(ctx, doc=writer) is None
    mint.assert_not_called()


def test_workbook_session_key_unsaved_when_property_does_not_stick() -> None:
    """A silent no-op write must not return a minted id that the next read misses."""
    from unittest.mock import MagicMock, patch

    mock_doc = MagicMock()
    mock_doc.getURL.return_value = ""
    with (
        patch("plugin.scripting.session_manager.get_document_property", return_value=""),
        patch("plugin.scripting.session_manager.set_document_property", return_value=None),
    ):
        key = session_manager._workbook_session_key(mock_doc)
    assert key.startswith("unsaved:")


def test_workbook_session_key_returns_id_when_property_sticks() -> None:
    from unittest.mock import MagicMock, patch
    import uuid as uuid_mod

    mock_doc = MagicMock()
    mock_doc.getURL.return_value = ""
    stored: dict[str, str] = {}

    def _get(_doc, name, default=None):
        return stored.get(name, default)

    def _set(_doc, name, value):
        stored[name] = str(value)

    with (
        patch("plugin.scripting.session_manager.get_document_property", side_effect=_get),
        patch("plugin.scripting.session_manager.set_document_property", side_effect=_set),
    ):
        key = session_manager._workbook_session_key(mock_doc)
    uuid_mod.UUID(key.replace("unsaved:", ""))
    assert stored[session_manager.PYTHON_WORKBOOK_SESSION_PROP] == key


def test_find_document_headless_controller_none_still_matches() -> None:
    """Headless getCurrentController() is None; that must not hide the model."""
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    doc = MagicMock()
    doc.getCurrentController.return_value = None
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.scripting.session_manager.is_calc", return_value=True),
    ):
        found = session_manager._calc_document(ctx)
    from plugin.framework.thread_guard import _unwrap_uno

    assert _unwrap_uno(found) is doc


def test_find_document_skips_controller_without_frame() -> None:
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    doc = MagicMock()
    ctrl = MagicMock()
    ctrl.getFrame.return_value = None
    doc.getCurrentController.return_value = ctrl
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    desktop.getComponents.return_value = None
    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.scripting.session_manager.is_calc", return_value=True),
    ):
        assert session_manager._calc_document(ctx) is None


def test_find_document_enumeration_accepts_headless_controller_none() -> None:
    from unittest.mock import MagicMock, patch

    ctx = MagicMock()
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    elem = MagicMock()
    elem.getCurrentController.return_value = None
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = elem
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop.getComponents.return_value = comps
    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.scripting.session_manager.is_calc", return_value=True),
    ):
        from plugin.framework.thread_guard import _unwrap_uno

        assert _unwrap_uno(session_manager._calc_document(ctx)) is elem


def test_document_for_script_session_stops_on_magicmock_enum() -> None:
    from unittest.mock import MagicMock, patch

    enum = MagicMock()
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop = MagicMock()
    desktop.getComponents.return_value = comps
    with patch("plugin.scripting.session_manager.get_desktop", return_value=desktop):
        found = session_manager.document_for_script_session(MagicMock(), "calc:file:///never.ods")
    assert found is None
    enum.nextElement.assert_not_called()


def test_document_for_script_session_stops_at_enum_cap(caplog) -> None:
    import logging
    from unittest.mock import MagicMock, patch

    enum = MagicMock()
    enum.hasMoreElements.return_value = True
    missed = MagicMock()
    missed.getURL.return_value = "file:///other.ods"
    enum.nextElement.return_value = missed
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop = MagicMock()
    desktop.getComponents.return_value = comps
    with (
        patch("plugin.scripting.session_manager.get_desktop", return_value=desktop),
        patch("plugin.scripting.session_manager._ENUM_CAP", 2),
        caplog.at_level(logging.ERROR, logger="plugin.scripting.session_manager"),
    ):
        found = session_manager.document_for_script_session(MagicMock(), "calc:file:///wanted.ods")
    assert found is None
    assert enum.nextElement.call_count == 2
    assert "hit cap=2" in caplog.text







def test_unsaved_session_id_starts_with_unsaved():
    from unittest.mock import MagicMock, patch
    from plugin.scripting.session_manager import _workbook_session_key
    doc = MagicMock()
    doc.getURL = lambda: ""
    doc.getPropertyValue.side_effect = Exception("No props")
    with patch("plugin.scripting.session_manager.get_document_property", return_value=None):
        with patch("plugin.scripting.session_manager.set_document_property"):
            key = _workbook_session_key(doc)
            assert key.startswith("unsaved:")

def test_ppt_master_unsaved_decks_unique_session_id() -> None:
    from unittest.mock import MagicMock
    from plugin.ppt_master.venv.host import ppt_master_session_id

    deck1 = MagicMock()
    deck1.getURL.return_value = ""
    # Simulate missing/failing UserDefinedProperties for unsaved document
    def prop_err(*args, **kwargs):
        raise Exception()
    deck1.getPropertyValue.side_effect = prop_err
    deck1.getDocumentProperties.side_effect = prop_err

    deck2 = MagicMock()
    deck2.getURL.return_value = ""
    deck2.getPropertyValue.side_effect = prop_err
    deck2.getDocumentProperties.side_effect = prop_err

    id1 = ppt_master_session_id(deck1)
    id2 = ppt_master_session_id(deck2)

    assert id1 != id2
    assert id1.startswith("ppt_master:unsaved:")
    assert id2.startswith("ppt_master:unsaved:")


