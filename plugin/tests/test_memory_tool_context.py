import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from plugin.modules.chatbot.memory import MemoryStore, MemoryTool


class _ToolContextLike:
    def __init__(self, inner_ctx):
        self.ctx = inner_ctx


class TestMemoryToolContext(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_memory_store_uses_tool_context_inner_ctx(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)

        with patch("plugin.modules.chatbot.memory.user_config_dir", return_value=self.tmp_dir) as mock_cfg:
            MemoryStore(tctx)

        self.assertEqual(mock_cfg.call_count, 1)
        self.assertIs(mock_cfg.call_args[0][0], inner_ctx)

    def test_memory_store_accepts_raw_ctx(self):
        raw_ctx = object()

        with patch("plugin.modules.chatbot.memory.user_config_dir", return_value=self.tmp_dir) as mock_cfg:
            MemoryStore(raw_ctx)

        self.assertEqual(mock_cfg.call_count, 1)
        self.assertIs(mock_cfg.call_args[0][0], raw_ctx)

    def test_memory_tool_execute_with_tool_context_like(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)
        tool = MemoryTool()

        with patch("plugin.modules.chatbot.memory.user_config_dir", return_value=self.tmp_dir):
            result = tool.execute(tctx, key="user_name", content="Keith")

        self.assertEqual(result.get("status"), "ok")
        user_memory_path = os.path.join(self.tmp_dir, "memories", "USER.md")
        with open(user_memory_path, "r", encoding="utf-8") as f:
            self.assertIn('"user_name": "Keith"', f.read())


if __name__ == "__main__":
    unittest.main()
