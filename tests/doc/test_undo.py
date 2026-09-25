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
