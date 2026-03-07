import unittest
from unittest.mock import MagicMock
from plugin.framework.eval_runner import EvalRunner

class TestEvalRunner(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.doc = MagicMock()
        self.model_name = "test-model"
        # Optional: Mock get_config/get_api_config if needed

    def test_runner_init(self):
        runner = EvalRunner(self.ctx, self.doc, self.model_name)
        self.assertEqual(runner.passed, 0)
        self.assertEqual(runner.failed, 0)

if __name__ == "__main__":
    unittest.main()
