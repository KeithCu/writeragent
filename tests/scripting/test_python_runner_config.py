import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add plugin to path
sys.path.append(os.getcwd())

from plugin.scripting.python_runner import run_python_dialog

class TestPythonRunnerConfig(unittest.TestCase):
    @patch('plugin.scripting.python_runner.get_ctx')
    @patch('plugin.scripting.python_runner.get_desktop')
    @patch('plugin.scripting.python_runner.is_writer')
    @patch('plugin.scripting.python_runner.is_calc')
    @patch('plugin.scripting.python_runner.is_draw')
    @patch('plugin.scripting.python_runner.get_config_str')
    @patch('plugin.scripting.python_runner.show_python_input_dialog')
    @patch('plugin.scripting.python_runner.set_config')
    @patch('plugin.scripting.python_runner.run_code_in_user_venv')
    def test_run_python_dialog_keys(self, mock_run, mock_set, mock_show, mock_get, 
                                   mock_is_draw, mock_is_calc, mock_is_writer, 
                                   mock_desktop, mock_ctx):
        mock_ctx_val = MagicMock()
        mock_ctx.return_value = mock_ctx_val
        mock_doc = MagicMock()
        mock_desktop.return_value.getCurrentComponent.return_value = mock_doc
        mock_show.return_value = "print('hello')"
        mock_run.return_value = {"status": "ok", "result": None, "stdout": ""}

        # Test Writer
        mock_is_writer.return_value = True
        mock_is_calc.return_value = False
        mock_is_draw.return_value = False
        
        run_python_dialog()
        mock_get.assert_called_with(mock_ctx_val, "last_python_script_writer")
        # Note: the test mocks get_config_str, so we don't strictly test the default value here
        # but we can verify the key is correct.
        mock_set.assert_called_with(mock_ctx_val, "last_python_script_writer", "print('hello')")

        # Test Calc
        mock_is_writer.return_value = False
        mock_is_calc.return_value = True
        mock_is_draw.return_value = False
        
        run_python_dialog()
        mock_get.assert_called_with(mock_ctx_val, "last_python_script_calc")
        mock_set.assert_called_with(mock_ctx_val, "last_python_script_calc", "print('hello')")

        # Test Draw
        mock_is_writer.return_value = False
        mock_is_calc.return_value = False
        mock_is_draw.return_value = True
        
        run_python_dialog()
        mock_get.assert_called_with(mock_ctx_val, "last_python_script_draw")
        mock_set.assert_called_with(mock_ctx_val, "last_python_script_draw", "print('hello')")

    def test_config_defaults(self):
        from plugin.framework.config import WriterAgentConfig
        config = WriterAgentConfig()
        self.assertTrue(config.last_python_script_writer.startswith("# Python Writer script"))
        self.assertTrue(config.last_python_script_calc.startswith("# Python Calc script"))
        self.assertTrue(config.last_python_script_draw.startswith("# Python Draw/Impress script"))

if __name__ == '__main__':
    unittest.main()
