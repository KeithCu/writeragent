import sys
import pytest
from unittest.mock import MagicMock, patch

from tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

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
         patch.object(wa.shape, "upsert_shape") as mock_upsert:
        
        mock_get_type.return_value = "writer"
        mock_apply.return_value = {}
        mock_upsert.return_value = {}
        
        from plugin.framework.config import _DEFAULT_PYTHON_SCRIPTS
        code = _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
        exec(code, {"__name__": "__main__"})
        
        mock_apply.assert_called_once()
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
         patch.object(wa.shape, "upsert_shape") as mock_upsert:
        
        mock_get_type.return_value = "calc"
        mock_insert.return_value = {}
        mock_upsert.return_value = {}
        
        from plugin.framework.config import _DEFAULT_PYTHON_SCRIPTS
        code = _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
        exec(code, {"__name__": "__main__"})
        
        mock_insert.assert_called_once()
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
    from plugin.framework.config import WriterAgentConfig
    
    # Test that a config without "Universal Sample" gets it injected during validation
    config = WriterAgentConfig.from_dict({"saved_python_scripts": {"Hello WriterAgent": "result = 1"}})
    assert "Universal Sample" not in config.saved_python_scripts
    
    config.validate()
    assert "Universal Sample" in config.saved_python_scripts
    assert "import writeragent as wa" in config.saved_python_scripts["Universal Sample"]


