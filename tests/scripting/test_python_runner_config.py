import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add plugin to path
sys.path.append(os.getcwd())

from plugin.scripting.python_runner import run_python_dialog

class TestPythonRunnerConfig(unittest.TestCase):
    @patch('plugin.scripting.python_runner.monaco_open_expected', return_value=(None, False))
    @patch('plugin.scripting.python_runner.get_ctx')
    @patch('plugin.scripting.python_runner.get_desktop')
    @patch('plugin.scripting.python_runner.is_writer')
    @patch('plugin.scripting.python_runner.is_calc')
    @patch('plugin.scripting.python_runner.is_draw')
    @patch('plugin.scripting.python_runner.get_config', return_value={})
    @patch('plugin.framework.config.get_config_str')
    @patch('plugin.scripting.python_runner.resolve_run_script_name_config_key')
    @patch('plugin.scripting.python_runner.execute_and_insert_result')
    @patch('plugin.scripting.python_runner.set_config')
    @patch('plugin.scripting.python_runner.show_python_input_dialog')
    def test_run_python_dialog_keys(
        self,
        mock_show,
        mock_set,
        mock_execute,
        mock_name_key,
        mock_get_str,
        mock_get_config,
        mock_is_draw,
        mock_is_calc,
        mock_is_writer,
        mock_desktop,
        mock_ctx,
        mock_monaco,
    ):
        mock_ctx_val = MagicMock()
        mock_ctx.return_value = mock_ctx_val
        mock_doc = MagicMock()
        mock_desktop.return_value.getCurrentComponent.return_value = mock_doc
        mock_get_str.return_value = ""

        # Test Writer
        mock_is_writer.return_value = True
        mock_is_calc.return_value = False
        mock_is_draw.return_value = False
        mock_name_key.return_value = "last_python_script_name_writer"
        
        run_python_dialog()
        mock_name_key.assert_called_with(mock_doc)
        mock_get_str.assert_called_with("last_python_script_name_writer")
        mock_show.assert_called()
        mock_set.assert_not_called()
        mock_execute.assert_not_called()

        # Test Calc
        mock_is_writer.return_value = False
        mock_is_calc.return_value = True
        mock_is_draw.return_value = False
        mock_name_key.return_value = "last_python_script_name_calc"
        
        run_python_dialog()
        mock_get_str.assert_called_with("last_python_script_name_calc")
        mock_set.assert_not_called()
        mock_execute.assert_not_called()

        # Test Draw
        mock_is_writer.return_value = False
        mock_is_calc.return_value = False
        mock_is_draw.return_value = True
        mock_name_key.return_value = "last_python_script_name_draw"
        
        run_python_dialog()
        mock_get_str.assert_called_with("last_python_script_name_draw")
        mock_set.assert_not_called()
        mock_execute.assert_not_called()

    def test_config_defaults(self):
        from plugin.framework.config import WriterAgentConfig
        config = WriterAgentConfig()
        self.assertEqual(config.last_python_script_name_writer, "Prime Numbers")
        self.assertEqual(config.last_python_script_name_calc, "Prime Numbers")
        self.assertEqual(config.last_python_script_name_draw, "Prime Numbers")

if __name__ == '__main__':
    unittest.main()
