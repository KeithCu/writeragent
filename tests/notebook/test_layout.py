from unittest.mock import MagicMock, patch

from plugin.notebook.layout import (
    height_for_text,
    insert_code_input_in_flow,
    runtime_uid_from_doc_key,
)


def test_runtime_uid_from_doc_key():
    assert runtime_uid_from_doc_key("uid:abc") == "abc"
    assert runtime_uid_from_doc_key("id:1") == ""


def test_insert_code_input_width_defaults_to_text_area():
    doc = MagicMock()
    model = MagicMock()
    shape = MagicMock()
    doc.createInstance.side_effect = [model, shape]
    with patch("plugin.notebook.layout.text_area_width_units", return_value=7777):
        insert_code_input_in_flow(doc, name="nb_cell_0_code", source="x = 1", controls_before=0, cap=15000)
    size = shape.setSize.call_args.args[0]
    assert size.Width == 7777
    assert model.Text == "x = 1"


def test_insert_code_input_uses_explicit_field_w():
    doc = MagicMock()
    model = MagicMock()
    shape = MagicMock()
    doc.createInstance.side_effect = [model, shape]
    insert_code_input_in_flow(doc, name="nb_cell_1_code", source="x = 1\ny = 2", controls_before=0, field_w=8888, cap=15000)
    size = shape.setSize.call_args.args[0]
    assert size.Width == 8888
    assert size.Height == height_for_text("x = 1\ny = 2", field_w=8888, cap=15000)
