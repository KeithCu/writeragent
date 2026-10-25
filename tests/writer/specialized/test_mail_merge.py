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
    _OUTPUT_TYPES,
)


def _create_mock_ctx():
    ctx = MagicMock()
    doc = MagicMock()
    ctx.doc = doc
    comp_ctx = MagicMock()
    ctx.ctx = comp_ctx
    smgr = MagicMock()
    comp_ctx.ServiceManager = smgr
    return ctx, doc, comp_ctx, smgr


def test_list_data_sources_basic():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.getElementNames.return_value = ("Bibliography", "Customers")
    db_ctx.getDatabaseLocation.side_effect = lambda n: f"file:///tmp/{n}.odb"

    tool = ListDataSources()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    assert res["count"] == 2
    assert res["registered_names"] == ["Bibliography", "Customers"]
    assert res["data_sources"][0]["name"] == "Bibliography"
    assert res["data_sources"][1]["name"] == "Customers"
    assert res["data_sources"][1]["location"] == "file:///tmp/Customers.odb"


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


def test_list_data_sources_closes_connection_on_failure():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.getElementNames.return_value = ("Customers",)

    ds = MagicMock()
    db_ctx.getByName.return_value = ds
    conn = MagicMock()
    ds.getConnection.return_value = conn
    conn.getTables.side_effect = RuntimeError("no schema")

    tool = ListDataSources()
    res = tool.execute(ctx, include_tables=True)

    assert res["status"] == "ok"
    assert res["count"] == 1
    source_entry = res["data_sources"][0]
    assert "tables_error" in source_entry
    assert "no schema" in source_entry["tables_error"]
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


def test_register_data_source(tmp_path):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.hasByName.return_value = False

    odb_file = tmp_path / "clients.odb"
    odb_file.write_text("")

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", file_path=str(odb_file), action="register")

    assert res["status"] == "ok"
    assert res["name"] == "Clients"
    assert "file://" in res["file_url"]
    db_ctx.registerDatabaseLocation.assert_called_once_with("Clients", res["file_url"])
    db_ctx.registerObject.assert_not_called()
    db_ctx.revokeObject.assert_not_called()


def test_register_existing_source_changes_location(tmp_path):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.hasByName.return_value = True

    odb_file = tmp_path / "clients.odb"
    odb_file.write_text("")

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", file_path=str(odb_file), action="register")

    assert res["status"] == "ok"
    db_ctx.changeDatabaseLocation.assert_called_once_with("Clients", res["file_url"])
    db_ctx.registerDatabaseLocation.assert_not_called()
    db_ctx.revokeObject.assert_not_called()
    db_ctx.registerObject.assert_not_called()


def test_unregister_data_source():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.hasByName.return_value = True

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", action="unregister")

    assert res["status"] == "ok"
    assert "unregistered successfully" in res["message"]
    db_ctx.revokeDatabaseLocation.assert_called_with("Clients")
    db_ctx.revokeObject.assert_not_called()


def test_register_missing_file_rejected():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", file_path="/nonexistent/x.odb", action="register")

    assert res["status"] == "error"
    assert res["code"] == "FILE_NOT_FOUND"
    db_ctx.registerDatabaseLocation.assert_not_called()
    db_ctx.changeDatabaseLocation.assert_not_called()


def test_register_non_odb_rejected(tmp_path):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx

    ods_file = tmp_path / "x.ods"
    ods_file.write_text("")

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", file_path=str(ods_file), action="register")

    assert res["status"] == "error"
    assert res["code"] == "INVALID_FILE_TYPE"
    db_ctx.registerDatabaseLocation.assert_not_called()
    db_ctx.changeDatabaseLocation.assert_not_called()


def test_unregister_failure_surfaces():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.hasByName.return_value = True
    db_ctx.revokeDatabaseLocation.side_effect = RuntimeError("read-only")

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Clients", action="unregister")

    assert res["status"] == "error"
    assert res["code"] == "UNREGISTER_FAILED"
    assert "read-only" in res["message"]


def test_file_url_with_one_slash_is_normalized(tmp_path):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    db_ctx = MagicMock()
    smgr.createInstanceWithContext.return_value = db_ctx
    db_ctx.hasByName.return_value = False

    odb_file = tmp_path / "test.odb"
    odb_file.write_text("")

    tool = RegisterDataSource()
    res = tool.execute(ctx, name="Normalized", file_path=f"file:{odb_file}", action="register")

    assert res["status"] == "ok"
    assert res["file_url"].startswith("file:///")
    assert not res["file_url"].startswith("file:////")
    db_ctx.registerDatabaseLocation.assert_called_once_with("Normalized", res["file_url"])


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


def test_insert_field_search_rolls_back_failed_insert(monkeypatch):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()

    cursor_mock = MagicMock()
    doc.getTextFieldMasters.return_value = MagicMock(hasByName=lambda name: False)

    master_mock = MagicMock()
    field_mock = MagicMock()
    doc.createInstance.side_effect = [master_mock, field_mock]

    mgr = MagicMock()
    mgr.isLocked.return_value = False
    entered = {}
    mgr.enterUndoContext.side_effect = lambda t: entered.__setitem__("title", t)
    mgr.getAllUndoActionTitles.side_effect = lambda: (entered.get("title"),)
    doc.getUndoManager.return_value = mgr

    text_mock = MagicMock()
    cursor_mock.getText.return_value = text_mock
    text_mock.insertTextContent.side_effect = RuntimeError("insert failed")

    monkeypatch.setattr("plugin.writer.specialized.mail_merge.resolve_target_cursor", lambda ctx, t, o: cursor_mock)

    tool = InsertField()
    res = tool.execute(ctx, column_name="FirstName", target="search", old_content="TARGET")

    assert res["status"] == "error"
    assert "rolled back" in res["message"]
    assert "insert failed" in res["message"]
    cursor_mock.setString.assert_called_once_with("")
    mgr.undo.assert_called_once()


def test_insert_field_search_locked_undo_does_not_delete(monkeypatch):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()

    cursor_mock = MagicMock()
    doc.getTextFieldMasters.return_value = MagicMock(hasByName=lambda name: False)

    master_mock = MagicMock()
    field_mock = MagicMock()
    doc.createInstance.side_effect = [master_mock, field_mock]

    mgr = MagicMock()
    mgr.isLocked.return_value = True
    doc.getUndoManager.return_value = mgr

    monkeypatch.setattr("plugin.writer.specialized.mail_merge.resolve_target_cursor", lambda ctx, t, o: cursor_mock)

    tool = InsertField()
    res = tool.execute(ctx, column_name="FirstName", target="search", old_content="TARGET")

    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    cursor_mock.setString.assert_not_called()


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


def test_list_fields_stops_on_non_true_has_more_elements():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()

    fields_container = MagicMock()
    doc.getTextFields.return_value = fields_container
    enum_mock = MagicMock()
    fields_container.createEnumeration.return_value = enum_mock

    field1 = MagicMock()
    field1.supportsService.return_value = True
    info1 = MagicMock()
    info1.hasPropertyByName.return_value = False
    field1.getPropertySetInfo.return_value = info1
    field1.getPresentation.return_value = ""

    enum_mock.hasMoreElements.side_effect = [True, MagicMock()]
    enum_mock.nextElement.return_value = field1

    tool = ListFields()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    assert res["count"] == 1
    enum_mock.nextElement.assert_called_once()


def test_output_types_enum():
    assert RunMerge.parameters["properties"]["output_type"]["enum"] == ["file", "printer"]
    assert _OUTPUT_TYPES == {"printer": 1, "file": 2}


def test_run_merge_file_output(tmp_path):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.getURL.return_value = "file:///tmp/template.odt"
    doc.isModified.return_value = False

    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock

    out_dir = tmp_path / "output_docs"

    tool = RunMerge()
    res = tool.execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        output_type="file",
        output_path=str(out_dir),
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
    doc.createInstance.assert_not_called()


def test_run_merge_modified_active_doc_rejected():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.isModified.return_value = True
    doc.getURL.return_value = "file:///tmp/template.odt"

    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock

    tool = RunMerge()
    res = tool.execute(ctx, data_source_name="Clients", table_name="Sheet1")

    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_MODIFIED"
    mail_merge_mock.execute.assert_not_called()


def test_run_merge_missing_document_url_rejected():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()

    tool = RunMerge()
    res = tool.execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        document_url="/nonexistent/t.odt",
    )

    assert res["status"] == "error"
    assert res["code"] == "FILE_NOT_FOUND"
    smgr.createInstanceWithContext.assert_not_called()


def test_run_merge_without_output_path_leaves_output_url_unset():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.getURL.return_value = "file:///tmp/template.odt"
    doc.isModified.return_value = False

    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock

    tool = RunMerge()
    res = tool.execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        output_type="file",
    )

    assert res["status"] == "ok"
    assert "OutputURL" not in vars(mail_merge_mock)
    assert res["output_url"] is None
    assert mail_merge_mock.SaveAsSingleFile is False
    assert mail_merge_mock.SaveFilter == "writer8"


def test_run_merge_output_path_file_rejected(tmp_path):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.getURL.return_value = "file:///tmp/template.odt"
    doc.isModified.return_value = False

    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock

    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("hello")

    tool = RunMerge()
    res = tool.execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        output_type="file",
        output_path=str(file_path),
    )

    assert res["status"] == "error"
    assert res["code"] == "INVALID_OUTPUT_PATH"
    mail_merge_mock.execute.assert_not_called()


def test_run_merge_creates_output_directory(tmp_path):
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.getURL.return_value = "file:///tmp/template.odt"
    doc.isModified.return_value = False

    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock

    nested_dir = tmp_path / "new" / "nested"

    tool = RunMerge()
    res = tool.execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        output_type="file",
        output_path=str(nested_dir),
    )

    assert res["status"] == "ok"
    assert nested_dir.is_dir()
    assert res["output_url"].endswith("/")
    assert mail_merge_mock.OutputURL == res["output_url"]


def test_run_merge_printer_message_has_no_file_format():
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.getURL.return_value = "file:///tmp/template.odt"
    doc.isModified.return_value = False

    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock

    tool = RunMerge()
    res = tool.execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        output_type="printer",
    )

    assert res["status"] == "ok"
    assert res["output_directory"] is None
    assert "printer" in res["message"]
    assert "ODT" not in res["message"]
    assert "OutputURL" not in vars(mail_merge_mock)


def test_database_context_and_mail_merge_are_not_document_factory_services(tmp_path):
    """Writer doc.createInstance does not provide DatabaseContext or MailMerge (LO 26)."""
    ctx, doc, comp_ctx, smgr = _create_mock_ctx()
    doc.createInstance.side_effect = RuntimeError("unknown service")

    db_ctx = MagicMock()
    db_ctx.getElementNames.return_value = ()
    smgr.createInstanceWithContext.return_value = db_ctx
    listed = ListDataSources().execute(ctx)
    assert listed["status"] == "ok", listed
    smgr.createInstanceWithContext.assert_called_with("com.sun.star.sdb.DatabaseContext", comp_ctx)
    doc.createInstance.assert_not_called()

    doc.getURL.return_value = "file:///tmp/template.odt"
    doc.isModified.return_value = False
    mail_merge_mock = MagicMock()
    smgr.createInstanceWithContext.return_value = mail_merge_mock
    out_dir = tmp_path / "output_docs"
    merged = RunMerge().execute(
        ctx,
        data_source_name="Clients",
        table_name="Sheet1",
        output_type="file",
        output_path=str(out_dir),
    )
    assert merged["status"] == "ok", merged
    smgr.createInstanceWithContext.assert_called_with("com.sun.star.text.MailMerge", comp_ctx)
    doc.createInstance.assert_not_called()


def test_run_merge_is_sync():
    assert RunMerge().is_async() is False
