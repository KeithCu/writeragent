# WriterAgent - tests for notebook form control lookup

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.notebook.form_lookup import (
    find_control_shape_by_name,
    find_form_control_model_by_name,
    index_form_control_models,
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

    doc = MagicMock()
    doc.getDrawPage.side_effect = AttributeError("no draw page")
    doc.getText.return_value.createEnumeration.return_value = _enum_of([para])

    assert find_form_control_model_by_name(doc, "nb_cell_0_code") is field_model


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


def _enum_of(items):
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * len(items) + [False]
    enum.nextElement.side_effect = items
    return enum
