# WriterAgent - tests for notebook form control lookup

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.notebook.form_lookup import (
    control_lookup_scope,
    find_control_shape_by_name,
    find_form_control_model_by_name,
    index_form_control_models,
    read_code_from_field,
)


def test_index_form_control_models_from_draw_page():
    run_model = MagicMock()
    run_model.Name = "nb_run_abc"
    code_model = MagicMock()
    code_model.Name = "nb_cell_1_code"

    run_shape = MagicMock()
    run_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    run_shape.Control = run_model

    code_shape = MagicMock()
    code_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    code_shape.Control = code_model

    other_shape = MagicMock()
    other_shape.getShapeType.return_value = "com.sun.star.drawing.RectangleShape"

    dp = MagicMock()
    dp.getCount.return_value = 3
    dp.getByIndex.side_effect = [run_shape, code_shape, other_shape]

    doc = MagicMock()
    doc.getDrawPage.return_value = dp
    doc.getText.side_effect = AttributeError("no text")

    by_name = index_form_control_models(doc)
    assert by_name["nb_run_abc"] is run_model
    assert by_name["nb_cell_1_code"] is code_model
    assert len(by_name) == 2


def test_find_form_control_model_by_name_text_fallback():
    field_model = MagicMock()
    field_model.Name = "nb_cell_0_code"

    portion = MagicMock()
    portion.getPropertyValue.return_value = "Frame"
    portion.TextField = field_model

    para = MagicMock()
    para.createEnumeration.return_value = _enum_of([portion])

    dp = MagicMock()
    dp.getCount.return_value = 0

    doc = MagicMock()
    doc.getDrawPage.return_value = dp
    doc.getText.return_value.createEnumeration.return_value = _enum_of([para])

    assert find_form_control_model_by_name(doc, "nb_cell_0_code") is field_model


def test_read_code_from_field_missing_is_none_empty_text_is_blank():
    present = MagicMock()
    present.Name = "nb_cell_0_code"
    present.Text = ""
    dp = MagicMock()
    dp.getCount.return_value = 1
    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    shape.Control = present
    dp.getByIndex.side_effect = [shape, shape]
    doc = MagicMock()
    doc.getDrawPage.return_value = dp

    assert read_code_from_field(doc, "nb_cell_0_code") == ""
    assert read_code_from_field(doc, "nb_cell_9_code") is None


def test_find_control_shape_by_name_from_draw_page():
    run_model = MagicMock()
    run_model.Name = "nb_run_abc"
    code_model = MagicMock()
    code_model.Name = "nb_cell_1_code"

    run_shape = MagicMock()
    run_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    run_shape.Control = run_model

    code_shape = MagicMock()
    code_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    code_shape.Control = code_model

    dp = MagicMock()
    dp.getCount.return_value = 2
    dp.getByIndex.side_effect = [run_shape, code_shape]

    doc = MagicMock()
    doc.getDrawPage.return_value = dp

    assert find_control_shape_by_name(doc, "nb_cell_1_code") is code_shape
    dp.getByIndex.side_effect = [run_shape, code_shape]
    assert find_control_shape_by_name(doc, "nb_run_abc") is run_shape
    dp.getByIndex.side_effect = [run_shape, code_shape]
    assert find_control_shape_by_name(doc, "missing") is None
    assert find_control_shape_by_name(doc, "") is None


def test_text_fallback_skips_block_without_portion_enumeration():
    table = MagicMock()
    table.createEnumeration.side_effect = AttributeError("text table")

    field_model = MagicMock()
    field_model.Name = "nb_cell_0_code"
    portion = MagicMock()
    portion.getPropertyValue.return_value = "Frame"
    portion.TextField = field_model
    para = MagicMock()
    para.createEnumeration.return_value = _enum_of([portion])

    dp = MagicMock()
    dp.getCount.return_value = 0
    doc = MagicMock()
    doc.getDrawPage.return_value = dp
    doc.getText.return_value.createEnumeration.return_value = _enum_of([table, para])

    assert find_form_control_model_by_name(doc, "nb_cell_0_code") is field_model


def _enum_of(items):
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * len(items) + [False]
    enum.nextElement.side_effect = items
    return enum


def test_control_lookup_scope_reuses_single_draw_page_walk():
    run_model = MagicMock()
    run_model.Name = "nb_run_abc"
    code_model = MagicMock()
    code_model.Name = "nb_cell_1_code"
    code_model.Text = "x = 42"

    run_shape = MagicMock()
    run_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    run_shape.Control = run_model

    code_shape = MagicMock()
    code_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    code_shape.Control = code_model

    dp = MagicMock()
    dp.getCount.return_value = 2
    dp.getByIndex.side_effect = [run_shape, code_shape]

    doc = MagicMock()
    doc.getDrawPage.return_value = dp

    with control_lookup_scope(doc):
        # First lookup uses the index built on entering scope
        assert find_form_control_model_by_name(doc, "nb_cell_1_code") is code_model
        assert find_control_shape_by_name(doc, "nb_run_abc") is run_shape
        assert read_code_from_field(doc, "nb_cell_1_code") == "x = 42"
        # Nested scope reuses outer index without rebuilding
        with control_lookup_scope(doc):
            assert find_form_control_model_by_name(doc, "nb_run_abc") is run_model

    # Exactly 2 calls to getByIndex: 1 pass across all shapes
    assert dp.getByIndex.call_count == 2


def test_missing_field_name_in_scope_returns_none_without_second_walk():
    code_model = MagicMock()
    code_model.Name = "nb_cell_1_code"
    code_shape = MagicMock()
    code_shape.getShapeType.return_value = "com.sun.star.drawing.ControlShape"
    code_shape.Control = code_model

    dp = MagicMock()
    dp.getCount.return_value = 1
    dp.getByIndex.return_value = code_shape

    doc = MagicMock()
    doc.getDrawPage.return_value = dp

    with control_lookup_scope(doc):
        assert read_code_from_field(doc, "nb_cell_missing") is None
        assert find_control_shape_by_name(doc, "nb_cell_missing") is None
        assert find_form_control_model_by_name(doc, "nb_cell_missing") is None

    # Only 1 call to getByIndex during the initial build
    assert dp.getByIndex.call_count == 1


def test_lookup_with_none_doc_is_empty_not_a_crash():
    assert index_form_control_models(None) == {}
    assert find_form_control_model_by_name(None, "nb_cell_0_code") is None
    assert find_control_shape_by_name(None, "nb_cell_0_code") is None
    assert read_code_from_field(None, "nb_cell_0_code") is None


def test_text_fallback_accepts_uno_integer_one():
    field_model = MagicMock()
    field_model.Name = "nb_cell_0_code"
    portion = MagicMock()
    portion.getPropertyValue.return_value = "Frame"
    portion.TextField = field_model
    para = MagicMock()
    para.createEnumeration.return_value = _enum_of([portion])

    outer = MagicMock()
    outer.hasMoreElements.side_effect = [1, False]
    outer.nextElement.return_value = para

    dp = MagicMock()
    dp.getCount.return_value = 0
    doc = MagicMock()
    doc.getDrawPage.return_value = dp
    doc.getText.return_value.createEnumeration.return_value = outer

    assert find_form_control_model_by_name(doc, "nb_cell_0_code") is field_model

