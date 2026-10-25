"""Embedded OLE tools are parked stubs (B3-A3): not registered, honest error."""
from unittest.mock import MagicMock

import plugin.writer.specialized.embedded as embedded_mod
from plugin.framework.service import iter_named_concrete_subclasses
from plugin.framework.tool import ToolBase, ToolBaseDummy
from plugin.writer.specialized.embedded import EmbeddedEdit, EmbeddedInsert


def test_embedded_tools_are_parked_dummies():
    assert issubclass(EmbeddedInsert, ToolBaseDummy)
    assert issubclass(EmbeddedEdit, ToolBaseDummy)


def test_embedded_tools_are_not_discovered_as_tools():
    assert list(iter_named_concrete_subclasses(embedded_mod, ToolBase, ToolBaseDummy)) == []


def test_embedded_insert_fails_immediately_without_target_resolution():
    ctx = MagicMock()
    res = EmbeddedInsert().execute(ctx, target="search", old_content="")
    assert res["status"] == "error"
    assert "not implemented" in res["message"]
    assert ctx.mock_calls == []


def test_embedded_edit_fails_immediately():
    res = EmbeddedEdit().execute(MagicMock(), name="anything")
    assert res["status"] == "error"
    assert "not implemented" in res["message"]
