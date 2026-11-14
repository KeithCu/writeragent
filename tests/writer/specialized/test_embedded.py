"""Unit tests for Writer embedded OLE insert (embedded domain)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import plugin.writer.specialized.embedded as embedded_mod
from plugin.app import get_tools
from plugin.tests.testing_utils import WriterDocStub
from plugin.writer.specialized.embedded import EmbeddedEdit, EmbeddedInsert


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


def test_embedded_insert_rejects_bad_object_type_and_bool_cell():
    from plugin.framework.tool import prepare_tool_call

    ctx = MagicMock()
    ctx.doc_type = "writer"
    tool = EmbeddedInsert()
    bad_type = prepare_tool_call(tool, ctx, "embedded_insert", {"object_type": "chart"})
    assert bad_type.error is not None
    assert bad_type.error["code"] == "VALIDATION_ERROR"

    bad_data = prepare_tool_call(tool, ctx, "embedded_insert", {"object_type": "spreadsheet", "data": "x"})
    assert bad_data.error is not None
    assert bad_data.error["code"] == "VALIDATION_ERROR"

    bad_width = prepare_tool_call(
        tool, ctx, "embedded_insert", {"object_type": "spreadsheet", "width": True, "height": 5}
    )
    assert bad_width.error is not None
    assert bad_width.error["code"] == "VALIDATION_ERROR"

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


def _commit_mutate(_tool, _doc, _title, mutate, *_args, **_kwargs):
    return mutate()


def _make_embed_ctx(
    clsid="abc",
    *,
    has_by_name=False,
    element_names=None,
    emb=None,
):
    doc = MagicMock()
    if emb is not None:
        doc.createInstance.return_value = emb
    container = MagicMock()
    container.hasByName.return_value = has_by_name
    if element_names is not None:
        container.getElementNames.side_effect = element_names
    doc.getEmbeddedObjects.return_value = container
    ctx = MagicMock(
        doc=doc,
        ctx=MagicMock(
            getServiceManager=lambda: _fake_config(
                clsid,
                factory=embedded_mod._EMBED_FACTORY,
                doc_service=embedded_mod._SPREADSHEET_DOC_SERVICE,
            )
        ),
    )
    return ctx, doc


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
    cursor = MagicMock()
    host_text = MagicMock()
    cursor.getText.return_value = host_text

    emb = MagicMock()
    inner = MagicMock()
    inner.supportsService.return_value = True
    sheet = MagicMock()
    inner.getSheets.return_value.getByIndex.return_value = sheet
    emb.getEmbeddedObject.return_value = inner

    xe = MagicMock()
    va = MagicMock(Width=9033, Height=2260)
    xe.getVisualAreaSize.return_value = va
    emb.getExtendedControlOverEmbeddedObject.return_value = xe

    cells: dict[tuple[int, int], MagicMock] = {}

    def _cell(c, r):
        key = (c, r)
        if key not in cells:
            cells[key] = MagicMock()
        return cells[key]

    sheet.getCellByPosition.side_effect = _cell

    ctx, unused_doc = _make_embed_ctx("abc-def", element_names=[(), ("SheetEmbed",)], emb=emb)

    call_order: list[str] = []

    def _track_set(name):
        def _setter(_self, value):
            call_order.append(name)

        return property(lambda _self: None, _setter)

    type(emb).CLSID = _track_set("CLSID")
    type(emb).AnchorType = _track_set("AnchorType")

    data = [["Label", 1], ["=SUM(B2:B3)", 2], [3, None]]
    expected_size = embedded_mod._embed_size_for_used_range(len(data), max(len(row) for row in data))
    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit_mutate),
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


def test_embedded_insert_explicit_size_reported():
    cursor = MagicMock()
    host_text = MagicMock()
    cursor.getText.return_value = host_text

    emb = MagicMock()
    inner = MagicMock()
    inner.supportsService.return_value = True
    sheet = MagicMock()
    inner.getSheets.return_value.getByIndex.return_value = sheet
    emb.getEmbeddedObject.return_value = inner

    ctx, unused_doc = _make_embed_ctx("abc-def", element_names=[(), ("SheetEmbed",)], emb=emb)

    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit_mutate),
    ):
        res = EmbeddedInsert().execute(
            ctx,
            object_type="spreadsheet",
            target="end",
            width=4000,
            height=1500,
            data=[["a"]],
        )

    assert res["status"] == "ok"
    assert emb.Width == 4000
    assert emb.Height == 1500
    assert res["width"] == 4000
    assert res["height"] == 1500


def test_embedded_insert_name_unchanged_skips_get_embedded_object():
    cursor = MagicMock()
    cursor.getText.return_value = MagicMock()
    emb = MagicMock()
    ctx, unused_doc = _make_embed_ctx("abc", element_names=[(), ()], emb=emb)

    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit_mutate),
    ):
        res = EmbeddedInsert().execute(ctx, object_type="spreadsheet", target="end")

    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert "not installed" in res["message"]
    emb.getEmbeddedObject.assert_not_called()


def test_embedded_insert_duplicate_name_rejected_before_undo_step():
    ctx, doc = _make_embed_ctx("abc", has_by_name=True)

    mock_resolve = MagicMock()
    mock_commit = MagicMock()

    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", mock_resolve),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", mock_commit),
    ):
        res = EmbeddedInsert().execute(
            ctx,
            object_type="spreadsheet",
            target="end",
            name="Sales",
        )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert "already exists" in res["message"]
    doc.createInstance.assert_not_called()
    mock_resolve.assert_not_called()
    mock_commit.assert_not_called()


def test_embedded_insert_get_embedded_object_failure():
    cursor = MagicMock()
    cursor.getText.return_value = MagicMock()
    emb = MagicMock()
    emb.getEmbeddedObject.side_effect = RuntimeError("broken ole")
    ctx, unused_doc = _make_embed_ctx("abc", element_names=[(), ("Obj1",)], emb=emb)

    with (
        patch("plugin.writer.target_resolver.resolve_target_cursor", return_value=cursor),
        patch("plugin.writer.target_resolver.commit_or_rollback_mutation", side_effect=_commit_mutate),
    ):
        res = EmbeddedInsert().execute(ctx, object_type="spreadsheet", target="end")

    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert "broken ole" in res["message"]
