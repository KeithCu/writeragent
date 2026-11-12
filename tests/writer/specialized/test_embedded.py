"""Unit tests for Writer embedded OLE insert (embedded domain)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import plugin.writer.specialized.embedded as embedded_mod
from plugin.app import get_tools
from plugin.framework.service import iter_named_concrete_subclasses
from plugin.framework.tool import ToolBase, ToolBaseDummy
from plugin.tests.testing_utils import WriterDocStub, setup_uno_mocks
from plugin.writer.specialized.embedded import EmbeddedEdit, EmbeddedInsert

setup_uno_mocks()


def test_embedded_edit_stays_dummy_not_discovered():
    assert issubclass(EmbeddedEdit, ToolBaseDummy)
    names = [cls.name for cls in iter_named_concrete_subclasses(embedded_mod, ToolBase, ToolBaseDummy)]
    assert names == ["embedded_insert"]


def test_embedded_insert_in_delegate_catalog_not_default():
    from tests.tool_registry_filters import register_prompt_catalog_for_tests

    register_prompt_catalog_for_tests()
    registry = get_tools()
    doc = WriterDocStub()
    default_names = {t.name for t in registry.get_tools(doc=doc)}
    assert "embedded_insert" not in default_names
    assert "embedded_edit" not in default_names
    embedded_names = {t.name for t in registry.get_tools(doc=doc, active_domain="embedded", exclude_tiers=())}
    assert "embedded_insert" in embedded_names
    assert "embedded_edit" not in embedded_names


def test_embedded_edit_fails_immediately():
    res = EmbeddedEdit().execute(MagicMock(), name="anything")
    assert res["status"] == "error"
    assert "not implemented" in res["message"]


def test_embedded_insert_rejects_bad_object_type_and_bool_cell():
    ctx = MagicMock()
    tool = EmbeddedInsert()
    bad_type = tool.execute(ctx, object_type="chart")
    assert bad_type["status"] == "error"
    assert ctx.doc.createInstance.call_count == 0

    bad_bool = tool.execute(
        ctx,
        object_type="spreadsheet",
        data=[[True]],
    )
    assert bad_bool["status"] == "error"
    assert ctx.doc.createInstance.call_count == 0


def test_embedded_insert_rejects_width_without_height():
    ctx = MagicMock()
    res = EmbeddedInsert().execute(ctx, object_type="spreadsheet", width=1000)
    assert res["status"] == "error"
    assert ctx.doc.createInstance.call_count == 0


def _fake_config(clsid: str, *, factory: str, doc_service: str):
    calc_entry = MagicMock(ClassID=clsid)
    names_access = MagicMock()
    names_access.getByName.return_value = calc_entry

    object_entry = MagicMock(ObjectFactory=factory, ObjectDocumentServiceName=doc_service)
    objects_access = MagicMock()
    objects_access.getElementNames.return_value = (clsid.lower(),)
    objects_access.getByName.return_value = object_entry

    provider = MagicMock()

    def _access(service, args):
        node = args[0].Value
        if node.endswith("ObjectNames"):
            return names_access
        return objects_access

    provider.createInstanceWithArguments.side_effect = _access
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = provider
    return smgr


def test_spreadsheet_clsid_from_registry():
    ctx = MagicMock()
    ctx.getServiceManager.return_value = _fake_config(
        "AbC",
        factory=embedded_mod._EMBED_FACTORY,
        doc_service=embedded_mod._SPREADSHEET_DOC_SERVICE,
    )
    assert embedded_mod._spreadsheet_clsid(ctx) == "AbC"


def test_spreadsheet_clsid_rejects_wrong_factory():
    ctx = MagicMock()
    ctx.getServiceManager.return_value = _fake_config(
        "AbC",
        factory="com.sun.star.embed.WrongFactory",
        doc_service=embedded_mod._SPREADSHEET_DOC_SERVICE,
    )
    try:
        embedded_mod._spreadsheet_clsid(ctx)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not registered" in str(exc)


def test_embedded_insert_sequence_and_cell_writes():
    doc = MagicMock()
    cursor = MagicMock()
    host_text = MagicMock()
    cursor.getText.return_value = host_text

    emb = MagicMock()
    doc.createInstance.return_value = emb

    inner = MagicMock()
    inner.supportsService.return_value = True
    sheet = MagicMock()
    inner.getSheets.return_value.getByIndex.return_value = sheet
    emb.getEmbeddedObject.return_value = inner

    xe = MagicMock()
    va = MagicMock(Width=9033, Height=2260)
    xe.getVisualAreaSize.return_value = va
    emb.getExtendedControlOverEmbeddedObject.return_value = xe

    container = MagicMock()
    container.getElementNames.side_effect = [(), ("SheetEmbed",)]
    container.hasByName.return_value = False
    doc.getEmbeddedObjects.return_value = container

    cells: dict[tuple[int, int], MagicMock] = {}

    def _cell(c, r):
        key = (c, r)
        if key not in cells:
            cells[key] = MagicMock()
        return cells[key]

    sheet.getCellByPosition.side_effect = _cell

    ctx = MagicMock(doc=doc, ctx=MagicMock(getServiceManager=lambda: _fake_config(
        "abc-def",
        factory=embedded_mod._EMBED_FACTORY,
        doc_service=embedded_mod._SPREADSHEET_DOC_SERVICE,
    )))

    call_order: list[str] = []

    def _track_set(name):
        def _setter(_self, value):
            call_order.append(name)

        return property(lambda _self: None, _setter)

    type(emb).CLSID = _track_set("CLSID")
    type(emb).AnchorType = _track_set("AnchorType")

    def _commit(_tool, _doc, _title, mutate, *_args, **_kwargs):
        mutate()
        return None

    data = [["Label", 1], ["=SUM(B2:B3)", 2], [3, None]]
    expected_size = embedded_mod._embed_size_for_used_range(len(data), max(len(row) for row in data))
    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit),
    ):
        res = EmbeddedInsert().execute(
            ctx,
            object_type="spreadsheet",
            target="end",
            data=data,
        )

    assert res["status"] == "ok"
    assert call_order == ["CLSID", "AnchorType"]
    host_text.insertTextContent.assert_called_once_with(cursor, emb, False)
    cells[(0, 0)].setString.assert_called_once_with("Label")
    cells[(1, 0)].setValue.assert_called_once_with(1.0)
    cells[(0, 1)].setFormula.assert_called_once_with("=SUM(B2:B3)")
    cells[(1, 1)].setValue.assert_called_once_with(2.0)
    cells[(0, 2)].setValue.assert_called_once_with(3.0)
    assert emb.Width == expected_size.width
    assert emb.Height == expected_size.height
    emb.getExtendedControlOverEmbeddedObject.assert_not_called()


def test_embedded_insert_name_unchanged_skips_get_embedded_object():
    doc = MagicMock()
    cursor = MagicMock()
    cursor.getText.return_value = MagicMock()
    emb = MagicMock()
    doc.createInstance.return_value = emb
    container = MagicMock()
    container.getElementNames.side_effect = [(), ()]
    container.hasByName.return_value = False
    doc.getEmbeddedObjects.return_value = container
    ctx = MagicMock(doc=doc, ctx=MagicMock(getServiceManager=lambda: _fake_config(
        "abc",
        factory=embedded_mod._EMBED_FACTORY,
        doc_service=embedded_mod._SPREADSHEET_DOC_SERVICE,
    )))

    def _commit(_tool, _doc, _title, mutate, *_args, **_kwargs):
        err = mutate()
        return err

    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit),
    ):
        res = EmbeddedInsert().execute(ctx, object_type="spreadsheet", target="end")

    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert "not installed" in res["message"]
    emb.getEmbeddedObject.assert_not_called()


def test_embedded_insert_duplicate_name_before_create():
    doc = MagicMock()
    cursor = MagicMock()
    cursor.getText.return_value = MagicMock()
    container = MagicMock()
    container.getElementNames.return_value = ("Sales",)
    container.hasByName.return_value = True
    doc.getEmbeddedObjects.return_value = container
    ctx = MagicMock(doc=doc, ctx=MagicMock(getServiceManager=lambda: _fake_config(
        "abc",
        factory=embedded_mod._EMBED_FACTORY,
        doc_service=embedded_mod._SPREADSHEET_DOC_SERVICE,
    )))

    def _commit(_tool, _doc, _title, mutate, *_args, **_kwargs):
        return mutate()

    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit),
    ):
        res = EmbeddedInsert().execute(
            ctx,
            object_type="spreadsheet",
            target="end",
            name="Sales",
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    doc.createInstance.assert_not_called()


def test_embedded_insert_get_embedded_object_failure():
    doc = MagicMock()
    cursor = MagicMock()
    cursor.getText.return_value = MagicMock()
    emb = MagicMock()
    emb.getEmbeddedObject.side_effect = RuntimeError("broken ole")
    doc.createInstance.return_value = emb
    container = MagicMock()
    container.getElementNames.side_effect = [(), ("Obj1",)]
    container.hasByName.return_value = False
    doc.getEmbeddedObjects.return_value = container
    ctx = MagicMock(doc=doc, ctx=MagicMock(getServiceManager=lambda: _fake_config(
        "abc",
        factory=embedded_mod._EMBED_FACTORY,
        doc_service=embedded_mod._SPREADSHEET_DOC_SERVICE,
    )))

    def _commit(_tool, _doc, _title, mutate, *_args, **_kwargs):
        return mutate()

    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit),
    ):
        res = EmbeddedInsert().execute(ctx, object_type="spreadsheet", target="end")

    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert "broken ole" in res["message"]
