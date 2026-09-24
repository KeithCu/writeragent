from unittest.mock import patch


from plugin.framework.uno_bootstrap import register_alias_importer
register_alias_importer()

import writeragent as wa

def test_get_active_document_type_writer():
    with patch("writeragent._rpc_call") as mock_rpc:
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
    with patch("writeragent._rpc_call") as mock_rpc:
        mock_rpc.return_value = {
            "documents": [
                {"is_active": True, "doc_type": "calc"},
                {"is_active": False, "doc_type": "writer"}
            ]
        }
        doc_type = wa.get_active_document_type()
        assert doc_type == "calc"

def test_get_active_document_type_unknown():
    with patch("writeragent._rpc_call") as mock_rpc:
        mock_rpc.return_value = {
            "documents": []
        }
        doc_type = wa.get_active_document_type()
        assert doc_type == "unknown"

def test_universal_sample_writer():
    with patch("writeragent.get_active_document_type") as mock_get_type, \
         patch.object(wa.writer, "apply_document_content") as mock_apply, \
         patch.object(wa.shape, "upsert") as mock_upsert:
        
        mock_get_type.return_value = "writer"
        mock_apply.return_value = {}
        mock_upsert.return_value = {}
        
        from plugin.framework.config_schema import _DEFAULT_PYTHON_SCRIPTS
        code = _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
        exec(code, {"__name__": "__main__"})
        
        mock_apply.assert_called_once_with(
            content=["<h1>Hello from WriterAgent</h1>", "<p>Rich <b>HTML</b> at the end.</p>"],
            target="end",
        )
        mock_upsert.assert_called_once_with(
            action="create",
            shape_type="star24",
            x=2000,
            y=5000,
            width=4000,
            height=4000,
            fill_color="blue",
            text="24-sided Star"
        )

def test_universal_sample_calc():
    with patch("writeragent.get_active_document_type") as mock_get_type, \
         patch.object(wa.calc, "insert_cell_html") as mock_insert, \
         patch.object(wa.shape, "upsert") as mock_upsert:
        
        mock_get_type.return_value = "calc"
        mock_insert.return_value = {}
        mock_upsert.return_value = {}
        
        from plugin.framework.config_schema import _DEFAULT_PYTHON_SCRIPTS
        code = _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
        exec(code, {"__name__": "__main__"})
        
        mock_insert.assert_called_once_with(
            cell="A1",
            html="<h1>Hello from WriterAgent</h1><p>Rich <b>HTML</b>.</p>",
        )
        mock_upsert.assert_called_once_with(
            action="create",
            shape_type="star24",
            x=2000,
            y=5000,
            width=4000,
            height=4000,
            fill_color="blue",
            text="24-sided Star"
        )


def test_config_injects_universal_sample():
    from plugin.framework.config_schema import WriterAgentConfig
    
    # Test that a config without "Universal Sample" gets it injected during validation
    config = WriterAgentConfig.from_dict({"saved_python_scripts": {"Hello WriterAgent": "result = 1"}})
    assert "Universal Sample" not in config.saved_python_scripts
    
    config.validate()
    assert "Universal Sample" in config.saved_python_scripts
    sample = config.saved_python_scripts["Universal Sample"]
    assert "import writeragent as wa" in sample
    assert any(
        "apply_document_content(" in line and "target=" in line
        for line in sample.splitlines()
    )
    assert any(
        "insert_cell_html(" in line and "html=" in line
        for line in sample.splitlines()
    )
    assert any(
        "shape.upsert(" in line and "shape_type=" in line
        for line in sample.splitlines()
    )


def test_config_migrates_old_universal_sample():
    from plugin.framework.config_schema import WriterAgentConfig, _DEFAULT_PYTHON_SCRIPTS

    old_code = 'wa.calc.insert_cell_html(cell_address="A1", html="hi")\nwa.shape.upsert_shape(action="create")'
    config = WriterAgentConfig.from_dict({"saved_python_scripts": {"Universal Sample": old_code}})
    config.validate()
    migrated = config.saved_python_scripts["Universal Sample"]
    assert migrated == _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
    assert "Hello from Python SDK" not in migrated
    assert "cell_address=" not in migrated


def test_config_migrates_function_wrapped_universal_sample():
    from plugin.framework.config_schema import WriterAgentConfig, _DEFAULT_PYTHON_SCRIPTS

    old_wrapped = 'import writeragent as wa\n\ndef run():\n    print("x")\n\nif __name__ == "__main__":\n    run()'
    config = WriterAgentConfig.from_dict({"saved_python_scripts": {"Universal Sample": old_wrapped}})
    config.validate()
    assert config.saved_python_scripts["Universal Sample"] == _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
    assert "def run()" not in config.saved_python_scripts["Universal Sample"]


def test_config_keeps_user_edited_universal_sample():
    from plugin.framework.config_schema import WriterAgentConfig

    custom = 'print("my own script")\nwa.shape.upsert(action="create")'
    config = WriterAgentConfig.from_dict({"saved_python_scripts": {"Universal Sample": custom}})
    config.validate()
    assert config.saved_python_scripts["Universal Sample"] == custom


