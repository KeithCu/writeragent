import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from plugin.modules.chatbot.memory import (
    MemoryStore,
    MemoryTool,
    UPSERT_MEMORY_CHAT_VALUE_MAX,
    format_upsert_memory_chat_line,
    format_upsert_memory_chat_line_from_arguments,
    memory_key_from_tool_arguments,
    upsert_memory_arguments_dict,
)


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

    def test_format_upsert_memory_chat_line_shows_key_and_value(self):
        line = format_upsert_memory_chat_line({"key": "nickname", "content": "Bob"})
        self.assertIn("nickname", line)
        self.assertIn("Bob", line)
        self.assertTrue(line.startswith("[Memory update:"))

    def test_format_upsert_memory_chat_line_truncates_long_value(self):
        long_val = "x" * (UPSERT_MEMORY_CHAT_VALUE_MAX + 50)
        line = format_upsert_memory_chat_line({"key": "k", "content": long_val})
        self.assertIn("...", line)
        self.assertLess(len(line), len(long_val) + 80)

    def test_format_upsert_memory_chat_line_from_arguments_json_string(self):
        line = format_upsert_memory_chat_line_from_arguments(
            '{"key": "a", "content": "b"}'
        )
        self.assertIn("'a'", line)
        self.assertIn("'b'", line)

    def test_memory_key_from_tool_arguments(self):
        self.assertEqual(memory_key_from_tool_arguments({"key": "name"}), "name")
        self.assertIsNone(memory_key_from_tool_arguments({}))
        self.assertEqual(
            memory_key_from_tool_arguments('{"key": "nested.k", "content": "v"}'),
            "nested.k",
        )

    def test_upsert_memory_arguments_dict(self):
        self.assertEqual(
            upsert_memory_arguments_dict({"key": "x", "content": "y"}),
            {"key": "x", "content": "y"},
        )
        self.assertIsNone(upsert_memory_arguments_dict("not json"))


if __name__ == "__main__":
    unittest.main()
