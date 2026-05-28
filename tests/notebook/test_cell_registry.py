# WriterAgent - tests for notebook cell registry

from __future__ import annotations

import json
from unittest.mock import MagicMock

from plugin.notebook.cell_registry import (
    NOTEBOOK_REGISTRY_UDPROP,
    NOTEBOOK_SOURCE_PATH_UDPROP,
    NotebookCodeCell,
    NotebookDocState,
    has_notebook_registry,
    insert_output_start_bookmark,
    load_registry,
    new_code_cell_entry,
    save_notebook_source_path,
    save_registry,
    state_from_json,
    state_to_json,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.doc.document_helpers import get_document_property  # noqa: E402
from tests.writer.test_document_helpers import (  # noqa: E402
    _DocWithUserDefinedProperties,
    _UserDefinedProperties,
)


def test_state_json_round_trip():
    cell = new_code_cell_entry(0, 1, "nb_cell_0_code")
    state = NotebookDocState(source_path="/tmp/a.ipynb", code_cells=[cell], next_execution_count=2)
    raw = state_to_json(state)
    restored = state_from_json(raw)
    assert restored is not None
    assert restored.source_path == "/tmp/a.ipynb"
    assert len(restored.code_cells) == 1
    assert restored.code_cells[0].cell_id == cell.cell_id
    assert restored.code_cells[0].code_field_name == "nb_cell_0_code"
    assert restored.code_cells[0].execution_count == 1
    assert restored.code_cells[0].output_start_bookmark.startswith("nb_out_")
    assert restored.next_execution_count == 2


def test_state_from_json_infers_next_execution_count():
    cell = new_code_cell_entry(0, 3, "nb_cell_0_code")
    raw = json.dumps({"version": 1, "source_path": "", "code_cells": [cell.to_dict()]})
    restored = state_from_json(raw)
    assert restored is not None
    assert restored.next_execution_count == 4


def test_new_code_cell_entry_bookmark_name_matches_id():
    cell = new_code_cell_entry(2, None, "nb_cell_2_code")
    assert cell.output_start_bookmark == f"nb_out_{cell.cell_id.replace('-', '')}"


def test_state_from_json_corrupt_returns_none():
    assert state_from_json("{not json") is None
    assert state_from_json('{"version": 99, "code_cells": []}') is None
    assert state_from_json("") is None


def test_state_from_json_missing_required_cell_field_returns_none():
    raw = json.dumps({"version": 1, "code_cells": [{"index": 0}]})
    assert state_from_json(raw) is None


def test_load_save_registry_on_mock_doc(monkeypatch):
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    monkeypatch.setattr("plugin.doc.document_helpers.uno.getConstantByName", lambda _name: 1)

    cell = new_code_cell_entry(0, None, "nb_cell_0_code")
    state = NotebookDocState(source_path="/x.ipynb", code_cells=[cell])
    save_registry(doc, state)
    save_notebook_source_path(doc, "/x.ipynb")

    loaded = load_registry(doc)
    assert loaded is not None
    assert loaded.code_cells[0].cell_id == cell.cell_id
    assert get_document_property(doc, NOTEBOOK_REGISTRY_UDPROP) is not None
    assert get_document_property(doc, NOTEBOOK_SOURCE_PATH_UDPROP) == "/x.ipynb"


def test_has_notebook_registry_false_when_missing():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    assert has_notebook_registry(doc) is False


def test_has_notebook_registry_true_with_code_cells(monkeypatch):
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    monkeypatch.setattr("plugin.doc.document_helpers.uno.getConstantByName", lambda _name: 1)
    save_registry(doc, NotebookDocState(code_cells=[new_code_cell_entry(0, None, "nb_cell_0_code")]))
    assert has_notebook_registry(doc) is True


def test_insert_output_start_bookmark():
    doc = MagicMock()
    bookmarks = MagicMock()
    bookmarks.hasByName.return_value = False
    doc.getBookmarks.return_value = bookmarks
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor
    doc.getText.return_value = text
    bm = MagicMock()
    doc.createInstance.return_value = bm

    assert insert_output_start_bookmark(doc, "nb_out_abc123") is True
    text.insertTextContent.assert_called_once()
    assert bm.Name == "nb_out_abc123"


def test_insert_output_start_bookmark_existing_name_ok():
    doc = MagicMock()
    bookmarks = MagicMock()
    bookmarks.hasByName.return_value = True
    doc.getBookmarks.return_value = bookmarks
    assert insert_output_start_bookmark(doc, "nb_out_existing") is True
    doc.getText.assert_not_called()


def test_notebook_code_cell_from_dict():
    d = {
        "cell_id": "a",
        "index": 1,
        "code_field_name": "nb_cell_1_code",
        "execution_count": 3,
        "output_start_bookmark": "nb_out_a",
        "last_run_status": "ok",
    }
    cell = NotebookCodeCell.from_dict(d)
    assert cell.last_run_status == "ok"
    assert cell.execution_count == 3
