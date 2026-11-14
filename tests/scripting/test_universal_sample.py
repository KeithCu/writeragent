from unittest.mock import patch

import pytest


from plugin.framework.uno_bootstrap import register_alias_importer
register_alias_importer()

import writeragent as wa

def test_get_active_document_type_writer():
    with patch("writeragent.call_host_tool") as mock_rpc:
        mock_rpc.return_value = {
            "documents": [
                {"is_active": False, "doc_type": "calc"},
                {"is_active": True, "doc_type": "writer"}
            ]
        }
        doc_type = wa.get_active_document_type()
        assert doc_type == "writer"
        mock_rpc.assert_called_once_with("list_open_documents")

def test_get_active_document_type_calc():
    with patch("writeragent.call_host_tool") as mock_rpc:
        mock_rpc.return_value = {
            "documents": [
                {"is_active": True, "doc_type": "calc"},
                {"is_active": False, "doc_type": "writer"}
            ]
        }
        doc_type = wa.get_active_document_type()
        assert doc_type == "calc"

def test_get_active_document_type_unknown():
    with patch("writeragent.call_host_tool") as mock_rpc:
        mock_rpc.return_value = {
            "documents": []
        }
        doc_type = wa.get_active_document_type()
        assert doc_type == "unknown"

def test_get_active_document_type_propagates_rpc_error():
    with patch("writeragent.call_host_tool", side_effect=RuntimeError("pipe closed")):
        try:
            wa.get_active_document_type()
        except RuntimeError as exc:
            assert "pipe closed" in str(exc)
        else:
            raise AssertionError("RPC failure was swallowed")


@pytest.mark.parametrize("doc_type", ["writer", "calc"])
def test_universal_sample_dispatches_by_doc_type(doc_type):
    with (
        patch("writeragent.get_active_document_type", return_value=doc_type),
        patch.object(wa.writer, "apply_document_content", return_value={}) as mock_apply,
        patch.object(wa.calc, "insert_cell_html", return_value={}) as mock_insert,
        patch.object(wa.shape, "upsert", return_value={}) as mock_upsert,
    ):
        from plugin.framework.config_schema import _DEFAULT_PYTHON_SCRIPTS

        exec(_DEFAULT_PYTHON_SCRIPTS["Universal Sample"], {"__name__": "__main__"})

    if doc_type == "writer":
        mock_apply.assert_called_once()
        mock_insert.assert_not_called()
    else:
        mock_insert.assert_called_once()
        mock_apply.assert_not_called()
    mock_upsert.assert_called_once()


def test_config_injects_universal_sample():
    from plugin.framework.config_schema import validate

    config = validate({"saved_python_scripts": {"Hello WriterAgent": "result = 1"}})
    assert "Universal Sample" in config["saved_python_scripts"]


def test_config_migrates_old_universal_sample():
    from plugin.framework.config_schema import validate, _DEFAULT_PYTHON_SCRIPTS

    old_code = 'wa.calc.insert_cell_html(cell_address="A1", html="hi")\nwa.shape.upsert_shape(action="create")'
    config = validate({"saved_python_scripts": {"Universal Sample": old_code}})
    migrated = config["saved_python_scripts"]["Universal Sample"]
    assert migrated == _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
    assert "Hello from Python SDK" not in migrated
    assert "cell_address=" not in migrated


def test_config_migrates_function_wrapped_universal_sample():
    from plugin.framework.config_schema import validate, _DEFAULT_PYTHON_SCRIPTS

    old_wrapped = 'import writeragent as wa\n\ndef run():\n    print("x")\n\nif __name__ == "__main__":\n    run()'
    config = validate({"saved_python_scripts": {"Universal Sample": old_wrapped}})
    assert config["saved_python_scripts"]["Universal Sample"] == _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
    assert "def run()" not in config["saved_python_scripts"]["Universal Sample"]


def test_config_keeps_user_edited_universal_sample():
    from plugin.framework.config_schema import validate

    custom = 'print("my own script")\nwa.shape.upsert(action="create")'
    config = validate({"saved_python_scripts": {"Universal Sample": custom}})
    assert config["saved_python_scripts"]["Universal Sample"] == custom


