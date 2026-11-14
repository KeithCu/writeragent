# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Undo and redo tool exposure. No LibreOffice."""
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import make_tool_error


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


@pytest.mark.parametrize(
    ("tool_name", "count_key", "method_name"),
    [
        ("Undo", "undone", "undo"),
        ("Redo", "redone", "redo"),
    ],
)
def test_undo_redo_huge_steps_clamped(tool_name, count_key, method_name):
    import plugin.doc.undo as undo_mod

    um = MagicMock()
    um.isUndoPossible.return_value = True
    um.isRedoPossible.return_value = True
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value = um
    res = getattr(undo_mod, tool_name)().execute(ctx, steps=10000)
    assert res["status"] == "ok" and res[count_key] == 10
    assert getattr(um, method_name).call_count == 10


def _failing_undo_doc():
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    mgr.getAllUndoActionTitles.side_effect = [(), ("Title",)]
    doc.getUndoManager.return_value = mgr
    return doc


@pytest.mark.parametrize(
    ("use_real_tool", "extra_kwargs", "expected_code", "expected_extras"),
    [
        pytest.param(
            False,
            {"extra_key": "extra_val"},
            "TOOL_EXECUTION_ERROR",
            {"extra_key": "extra_val"},
            id="mock_tool_extra_key",
        ),
        pytest.param(
            False,
            {"code": "INSERT_FRAME_FAILED", "frame": "F"},
            "INSERT_FRAME_FAILED",
            {"frame": "F"},
            id="mock_tool_extra_code",
        ),
        pytest.param(
            True,
            {"code": "INSERT_FRAME_FAILED"},
            "INSERT_FRAME_FAILED",
            {},
            id="real_tool_custom_code",
        ),
        pytest.param(
            True,
            {"deleted": 0},
            "TOOL_EXECUTION_ERROR",
            {"deleted": 0},
            id="real_tool_extra_int",
        ),
        pytest.param(
            True,
            {},
            "TOOL_EXECUTION_ERROR",
            {},
            id="real_tool_default_code",
        ),
    ],
)
def test_commit_or_rollback_mutation_error_shape_and_extras(
    use_real_tool, extra_kwargs, expected_code, expected_extras
):
    from plugin.doc.undo import Undo, commit_or_rollback_mutation

    if use_real_tool:
        tool = Undo()
    else:
        tool = MagicMock()
        tool._tool_error.side_effect = make_tool_error

    def mutate():
        raise RuntimeError("boom")

    res = commit_or_rollback_mutation(
        tool, _failing_undo_doc(), "Title", mutate, "Undo unavail", "Rollback failed", **extra_kwargs
    )
    assert res is not None
    assert res["status"] == "error"
    assert res["code"] == expected_code
    assert "details" not in res
    for k, v in expected_extras.items():
        assert res[k] == v
    if not use_real_tool:
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
    tool._tool_error.side_effect = make_tool_error
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
    tool._tool_error.side_effect = make_tool_error

    def mutate():
        raise RuntimeError("boom")

    res = commit_or_rollback_mutation(
        tool, _failing_undo_doc(), "Title", mutate, "Undo unavail", "Rollback failed"
    )
    assert res is not None
    assert res["status"] == "error"
    assert "Rollback failed" in res["message"]


def test_commit_or_rollback_mutation_surfaces_wrapper_errors():
    from plugin.doc.undo import commit_or_rollback_mutation

    tool = MagicMock()
    tool._tool_error.side_effect = make_tool_error
    doc = MagicMock()
    with patch("plugin.doc.undo.commit_or_rollback", side_effect=RuntimeError("wrapper")):
        res = commit_or_rollback_mutation(
            tool,
            doc,
            "Title",
            lambda: None,
            "Undo unavail",
            "Rollback failed",
            code="WRAPPER",
        )
    assert res is not None
    assert "wrapper" in res["message"]
    assert res.get("code") == "WRAPPER"


def test_undo_null_steps_is_one_and_non_integer_steps_is_rejected_by_registry():
    from plugin.framework.tool import ToolContext, ToolRegistry
    from plugin.doc.undo import Undo

    doc = MagicMock()
    um = MagicMock()
    um.isUndoPossible.return_value = True
    um.isRedoPossible.return_value = True
    doc.getUndoManager.return_value = um

    reg = ToolRegistry(services={})
    reg.register(Undo())
    ctx = ToolContext(doc, MagicMock(), "writer", reg)

    res = reg.execute("undo", ctx, steps=None)
    assert res["status"] == "ok"
    assert res["undone"] == 1

    um.reset_mock()
    err = reg.execute("undo", ctx, steps="abc")
    assert err["status"] == "error"
    assert err["code"] == "VALIDATION_ERROR"
    um.undo.assert_not_called()


def test_undo_tool_propagates_disposal():
    from plugin.doc.undo import Undo
    from plugin.framework.errors import DocumentDisposedError

    ctx = MagicMock()
    ctx.doc.getUndoManager.side_effect = DocumentDisposedError("gone")

    with pytest.raises(DocumentDisposedError):
        Undo().execute(ctx)


def test_undo_without_undo_manager_reports_error():
    from types import SimpleNamespace
    from plugin.doc.undo import Undo

    res = Undo().execute(SimpleNamespace(doc=object()))
    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
