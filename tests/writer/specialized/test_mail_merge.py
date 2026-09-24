# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock


from plugin.writer.specialized.mail_merge import (
    ListDataSources,
    RegisterDataSource,
    InsertField,
    ListFields,
    RunMerge,
)


def _create_mock_ctx():
    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc
    comp_ctx = MagicMock()
    ctx.get_ctx.return_value = comp_ctx
    smgr = MagicMock()
    comp_ctx.getServiceManager.return_value = smgr
    return ctx, doc, comp_ctx, smgr


def test_list_data_sources_basic():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.getElementNames.return_value = ("Bibliography", "Customers")

    tool = ListDataSources()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    assert res["count"] == 2
    assert res["registered_names"] == ["Bibliography", "Customers"]
    assert res["data_sources"][0]["name"] == "Bibliography"
    assert res["data_sources"][1]["name"] == "Customers"


def test_list_data_sources_with_tables():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.getElementNames.return_value = ("Customers",)

    ds = MagicMock()
    db_ctx.getByName.return_value = ds
    conn = MagicMock()
    ds.getConnection.return_value = conn
    tables_container = MagicMock()
    conn.getTables.return_value = tables_container
    tables_container.getElementNames.return_value = ("Contacts",)

    table_obj = MagicMock()
    tables_container.getByName.return_value = table_obj
    cols_container = MagicMock()
    table_obj.getColumns.return_value = cols_container
    cols_container.getElementNames.return_value = ("FirstName", "LastName", "Email")

    tool = ListDataSources()
    res = tool.execute(ctx, include_tables=True)

    assert res["status"] == "ok"
    assert res["count"] == 1
    source_entry = res["data_sources"][0]
    assert source_entry["name"] == "Customers"
    assert len(source_entry["tables"]) == 1
    assert source_entry["tables"][0]["table"] == "Contacts"
    assert source_entry["tables"][0]["columns"] == ["FirstName", "LastName", "Email"]
    conn.close.assert_called_once()


def test_list_data_sources_specific_name_not_found():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.getElementNames.return_value = ("Bibliography",)

    tool = ListDataSources()
    res = tool.execute(ctx, data_source_name="NonExistent")

    assert res["status"] == "error"
    assert "not registered" in res["message"]


def test_register_data_source():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.hasByName.return_value = False

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", file_path="/tmp/clients.ods", action="register")

    assert res["status"] == "ok"
    assert res["name"] == "Clients"
    assert "file://" in res["file_url"]
    db_ctx.registerObject.assert_called_once()


def test_unregister_data_source():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.hasByName.return_value = True

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", action="unregister")

    assert res["status"] == "ok"
    assert "unregistered successfully" in res["message"]
    db_ctx.revokeObject.assert_called_with("Clients")


def test_insert_field_basic():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()

    cursor_mock = MagicMock()
    doc.getText().createTextCursor.return_value = cursor_mock
    doc.getTextFieldMasters.return_value = MagicMock(hasByName=lambda name: False)

    master_mock = MagicMock()
    field_mock = MagicMock()
    doc.createInstance.side_effect = [master_mock, field_mock]

    tool = InsertField()
    res = tool.execute(
        ctx,
        column_name="FirstName",
        data_source_name="Clients",
        table_name="Sheet1",
        target="beginning",
    )

    assert res["status"] == "ok"
    assert res["field"]["column_name"] == "FirstName"
    master_mock.setPropertyValue.assert_any_call("DataBaseName", "Clients")
    master_mock.setPropertyValue.assert_any_call("DataTableName", "Sheet1")
    master_mock.setPropertyValue.assert_any_call("DataColumnName", "FirstName")
    field_mock.attachTextFieldMaster.assert_called_with(master_mock)

    text_mock = cursor_mock.getText()
    text_mock.insertTextContent.assert_called_with(cursor_mock, field_mock, False)


def test_list_fields():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()

    fields_container = MagicMock()
    doc.getTextFields.return_value = fields_container
    enum_mock = MagicMock()
    fields_container.createEnumeration.return_value = enum_mock

    field1 = MagicMock()
    field1.supportsService.return_value = True
    info1 = MagicMock()
    info1.hasPropertyByName.side_effect = lambda name: name in ("DataColumnName", "DataBaseName", "DataTableName")
    field1.getPropertySetInfo.return_value = info1
    field1.getPropertyValue.side_effect = lambda name: {
        "DataColumnName": "FirstName",
        "DataBaseName": "Clients",
        "DataTableName": "Sheet1",
    }.get(name)
    field1.getPresentation.side_effect = ["<FirstName>", "John"]

    enum_mock.hasMoreElements.side_effect = [True, False]
    enum_mock.nextElement.return_value = field1

    tool = ListFields()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["fields"][0]["column_name"] == "FirstName"
    assert res["fields"][0]["data_source_name"] == "Clients"
    assert res["fields"][0]["table_name"] == "Sheet1"
    assert res["fields"][0]["presentation"] == "<FirstName>"
    assert res["fields"][0]["content"] == "John"


def test_run_merge_file_output():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.getURL.return_value = "file:///tmp/template.odt"

    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock

    tool = RunMerge()
    res = tool.execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        output_type="file",
        output_path="/tmp/output_docs",
        save_as_single_file=True,
        file_format="pdf",
        filter="City = 'London'",
    )

    assert res["status"] == "ok"
    assert res["save_as_single_file"] is True
    assert res["file_format"] == "pdf"
    assert mail_merge_mock.DocumentURL == "file:///tmp/template.odt"
    assert mail_merge_mock.DataSourceName == "Clients"
    assert mail_merge_mock.Command == "Sheet1"
    assert mail_merge_mock.OutputType == 2
    assert mail_merge_mock.SaveFilter == "writer_pdf_export"
    assert mail_merge_mock.SaveAsSingleFile is True
    assert mail_merge_mock.Filter == "City = 'London'"
    mail_merge_mock.execute.assert_called_once_with([])
