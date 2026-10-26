# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Undo and redo tool exposure. No LibreOffice."""
from unittest.mock import MagicMock


# ---- 7) undo/redo exposed --------------------------------------------------------

def test_undo_redo_are_real_core_tools():
    from plugin.framework.tool import ToolBase
    from plugin.doc.undo import Redo, Undo

    for cls in (Undo, Redo):
        assert issubclass(cls, ToolBase)
        assert cls.tier == "core"
        assert cls.is_mutation is True
        assert "user" in cls.description.lower()  # the shared-stack caution must be in the description


def test_undo_counts_steps_and_reports_stack_state():
    from plugin.doc.undo import Undo

    um = MagicMock()
    um.isUndoPossible.side_effect = [True, True, False, False]
    um.isRedoPossible.return_value = True
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um
    res = Undo().execute(ctx, steps=3)
    assert res["status"] == "ok" and res["undone"] == 2
    assert "can_undo" in res and res["can_redo"] is True  # promised by the tool description


def test_redo_counts_steps():
    from plugin.doc.undo import Redo

    um = MagicMock()
    um.isRedoPossible.side_effect = [True, False, True]
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um
    res = Redo().execute(ctx, steps=2)
    assert res["status"] == "ok" and res["redone"] == 1


def test_undo_huge_steps_clamped():
    from plugin.doc.undo import Undo

    um = MagicMock()
    um.isUndoPossible.return_value = True
    um.isRedoPossible.return_value = True
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um
    res = Undo().execute(ctx, steps=10000)
    assert res["status"] == "ok" and res["undone"] == 10
    assert um.undo.call_count == 10


def test_redo_huge_steps_clamped():
    from plugin.doc.undo import Redo

    um = MagicMock()
    um.isRedoPossible.return_value = True
    um.isUndoPossible.return_value = True
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um
    res = Redo().execute(ctx, steps=10000)
    assert res["status"] == "ok" and res["redone"] == 10
    assert um.redo.call_count == 10


def test_commit_or_rollback_success():
    from plugin.doc.undo import commit_or_rollback

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    called = []
    res = commit_or_rollback(doc, "Title", lambda: called.append(1))
    assert res is None
    assert called == [1]
    mgr.enterUndoContext.assert_called_once_with("Title")
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_not_called()


def test_commit_or_rollback_locked_or_missing_raises_undo_unavailable():
    import pytest
    from plugin.doc.undo import UndoUnavailable, commit_or_rollback

    # Missing
    doc1 = MagicMock()
    doc1.getUndoManager.return_value = None
    with pytest.raises(UndoUnavailable):
        commit_or_rollback(doc1, "Title", lambda: None)

    # Locked
    doc2 = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = True
    doc2.getUndoManager.return_value = mgr
    with pytest.raises(UndoUnavailable):
        commit_or_rollback(doc2, "Title", lambda: None)
    mgr.enterUndoContext.assert_not_called()


def test_commit_or_rollback_error_dict_rolls_back():
    from plugin.doc.undo import commit_or_rollback

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("Title",)]
    doc.getUndoManager.return_value = mgr

    err = {"status": "error", "message": "boom"}
    res = commit_or_rollback(doc, "Title", lambda: err)
    assert res == err
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_called_once()


def test_commit_or_rollback_exception_rolls_back_and_reraises():
    import pytest
    from plugin.doc.undo import commit_or_rollback

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("Title",)]
    doc.getUndoManager.return_value = mgr

    def mutate():
        raise ValueError("mutation error")

    with pytest.raises(ValueError, match="mutation error"):
        commit_or_rollback(doc, "Title", mutate)
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_called_once()


def test_commit_or_rollback_undo_failure_keeps_cause():
    from plugin.doc.undo import commit_or_rollback

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("Title",)]
    mgr.undo.side_effect = RuntimeError("undo failed")
    doc.getUndoManager.return_value = mgr

    def mutate():
        raise ValueError("original cause")

    res = commit_or_rollback(doc, "Title", mutate)
    assert res["status"] == "error"
    assert res["code"] == "ROLLBACK_FAILED"
    assert "undo failed" in res["message"]
    assert "original cause" in res["message"]


def test_commit_or_rollback_does_not_undo_previous_same_title_action():
    import pytest
    from plugin.doc.undo import commit_or_rollback

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [("Title",), ("Title",)]
    doc.getUndoManager.return_value = mgr

    def mutate():
        raise ValueError("failed before write")

    with pytest.raises(ValueError, match="failed before write"):
        commit_or_rollback(doc, "Title", mutate)
    mgr.leaveUndoContext.assert_called_once()
    mgr.undo.assert_not_called()


def test_commit_or_rollback_failed_leave_after_success_is_an_error():
    from plugin.doc.undo import commit_or_rollback

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.leaveUndoContext.side_effect = RuntimeError("leave failed")
    doc.getUndoManager.return_value = mgr

    res = commit_or_rollback(doc, "Title", lambda: None)
    assert res is not None
    assert res["status"] == "error"
    assert res["code"] == "UNDO_CONTEXT_LEAK"
    mgr.undo.assert_not_called()


def test_commit_or_rollback_disposal_skips_rollback():
    import pytest
    from plugin.doc.undo import commit_or_rollback
    from plugin.framework.errors import DocumentDisposedError

    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    def mutate():
        raise DocumentDisposedError("gone")

    with pytest.raises(DocumentDisposedError):
        commit_or_rollback(doc, "Title", mutate)
    mgr.undo.assert_not_called()
    mgr.leaveUndoContext.assert_called_once()


def test_commit_or_rollback_mutation_passes_extra_error_once():
    from plugin.doc.undo import commit_or_rollback_mutation

    tool = MagicMock()
    tool._tool_error.side_effect = lambda msg, **kw: {"status": "error", "message": msg, **kw}
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("Title",)]
    doc.getUndoManager.return_value = mgr

    def mutate():
        raise RuntimeError("boom")

    res = commit_or_rollback_mutation(
        tool, doc, "Title", mutate, "Undo unavail", "Rollback failed", extra_key="extra_val"
    )
    assert res is not None
    assert res["status"] == "error"
    assert res["extra_key"] == "extra_val"
    tool._tool_error.assert_called_once()


def test_commit_or_rollback_mutation_success():
    from plugin.doc.undo import commit_or_rollback_mutation

    tool = MagicMock()
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    doc.getUndoManager.return_value = mgr

    res = commit_or_rollback_mutation(
        tool, doc, "Title", lambda: None, "Undo unavail", "Rollback failed"
    )
    assert res is None


def test_commit_or_rollback_mutation_undo_unavailable():
    from plugin.doc.undo import commit_or_rollback_mutation

    tool = MagicMock()
    tool._tool_error.side_effect = lambda msg, **kw: {"status": "error", "message": msg, **kw}
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = True
    doc.getUndoManager.return_value = mgr

    res = commit_or_rollback_mutation(
        tool, doc, "Title", lambda: None, "Undo unavail", "Rollback failed"
    )
    assert res is not None
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"


def test_commit_or_rollback_mutation_exception_reports_error():
    from plugin.doc.undo import commit_or_rollback_mutation

    tool = MagicMock()
    tool._tool_error.side_effect = lambda msg, **kw: {"status": "error", "message": msg, **kw}
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("Title",)]
    doc.getUndoManager.return_value = mgr

    def mutate():
        raise RuntimeError("boom")

    res = commit_or_rollback_mutation(
        tool, doc, "Title", mutate, "Undo unavail", "Rollback failed"
    )
    assert res is not None
    assert res["status"] == "error"
    assert "Rollback failed" in res["message"]


def test_undo_tolerates_null_and_non_numeric_steps():
    from plugin.doc.undo import Undo

    um = MagicMock()
    um.isUndoPossible.return_value = True
    um.isRedoPossible.return_value = True
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um

    res = Undo().execute(ctx, steps=None)
    assert res["status"] == "ok"
    assert res["undone"] == 1

    um.reset_mock()
    um.isUndoPossible.return_value = True
    um.isRedoPossible.return_value = True

    res2 = Undo().execute(ctx, steps="abc")
    assert res2["status"] == "ok"
    assert res2["undone"] == 1


def test_undo_tool_propagates_disposal():
    import pytest
    from plugin.doc.undo import Undo
    from plugin.framework.errors import DocumentDisposedError

    ctx = MagicMock()
    ctx.doc.getUndoManager.side_effect = DocumentDisposedError("gone")

    with pytest.raises(DocumentDisposedError):
        Undo().execute(ctx)


