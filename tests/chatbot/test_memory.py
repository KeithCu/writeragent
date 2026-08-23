import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from plugin.chatbot.memory import (
    MemoryStore,
    MemoryTool,
    UPSERT_MEMORY_CHAT_VALUE_MAX,
    format_upsert_memory_chat_line,
    format_upsert_memory_chat_line_from_arguments,
    memory_key_from_tool_arguments,
    upsert_memory_arguments_dict,
    user_profile_exists,
)

class DummyCtx:
    def __init__(self, tmp_dir):
        self.tmp_dir = tmp_dir

    # Mocking getServiceManager so user_config_dir resolves here
    def getServiceManager(self):
        sm = Mock()
        path_settings = Mock()
        path_settings.UserConfig = f"file://{self.tmp_dir}"
        sm.createInstanceWithContext.return_value = path_settings
        return sm


class _ToolContextLike:
    def __init__(self, inner_ctx):
        self.ctx = inner_ctx


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_memory_store_uses_tool_context_inner_ctx(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir) as mock_cfg:
            MemoryStore(tctx)

        mock_cfg.assert_called_once_with()

    def test_user_profile_exists_false_when_empty(self):
        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir):
            assert user_profile_exists(self.ctx) is False
            MemoryStore(self.ctx).write("user", "   ")
            assert user_profile_exists(self.ctx) is False
            MemoryStore(self.ctx).write("user", '{"name": "Keith"}')
            assert user_profile_exists(self.ctx) is True

    def test_user_profile_exists_false_on_store_error(self):
        with patch("plugin.chatbot.memory.MemoryStore", side_effect=RuntimeError("no config")):
            assert user_profile_exists(object()) is False

    def test_memory_store_accepts_raw_ctx(self):
        raw_ctx = object()

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir) as mock_cfg:
            MemoryStore(raw_ctx)

        mock_cfg.assert_called_once_with()

    def test_memory_tool_execute_with_tool_context_like(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)
        tool = MemoryTool()

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir):
            result = tool.execute(tctx, key="user_name", content="Keith")

        self.assertEqual(result.get("status"), "ok")
        user_memory_path = os.path.join(self.tmp_dir, "memories", "USER.md")
        with open(user_memory_path, "r", encoding="utf-8") as f:
            self.assertIn('"user_name": "Keith"', f.read())

    def test_memory_tool_execute_skips_redundant_write(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)
        tool = MemoryTool()

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir):
            # First write
            tool.execute(tctx, key="color", content="blue")

            # Patch MemoryStore.write to track calls
            with patch("plugin.chatbot.memory.MemoryStore.write") as mock_write:
                result = tool.execute(tctx, key="color", content="blue")

        self.assertEqual(result.get("status"), "ok")
        self.assertIn("already up to date", result.get("message", ""))
        mock_write.assert_not_called()

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

'''
@unittest.skip("Disabled per user request - depends on uno")
class TestMemoryTool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)
        try:
            import uno
            uno.fileUrlToSystemPath = lambda x: x.replace("file://", "")
        except ImportError:
            pass

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_memory_tool_actions(self):
        tool = MemoryTool()
        store = MemoryStore(self.ctx)

        # Insert new key
        res = tool.execute(self.ctx, key="favorite_language", content="Python")
        self.assertEqual(res["status"], "ok", f"Expected ok but got {res}")

        # Verify store read
        content = store.read("user")
        self.assertIn("favorite_language: Python", content)

        # Update existing key
        res = tool.execute(self.ctx, key="favorite_language", content="Rust")
        self.assertEqual(res["status"], "ok")
        content = store.read("user")
        self.assertIn("favorite_language: Rust", content)

        # Insert nested key
        res = tool.execute(self.ctx, key="editor.vim", content="Yes")
        self.assertEqual(res["status"], "ok")
        content = store.read("user")
        self.assertIn("editor:", content)
        self.assertIn("vim: Yes", content)
'''

class TestMemoryWriteConcurrency(unittest.TestCase):
    """Regression: parallel upsert_memory calls raced and tore USER.md (2026-08-23).

    Two calls in one model turn (name + favorite color) interleaved their
    open/write cycles: shorter payload won the head of the file, the longer
    write's stale tail survived, and the sibling key was lost.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)
        self.store = MemoryStore(self.ctx)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_parallel_upserts_merge_without_corruption(self):
        import threading

        from plugin.chatbot.memory import MemoryTool

        tool = MemoryTool()

        def run(key, content):
            res = tool.execute(self.ctx, key=key, content=content)
            assert res.get("status") == "ok", res

        t1 = threading.Thread(target=run, args=("name", "Andre"))
        t2 = threading.Thread(target=run, args=("favorite_colors", "dark blue"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        data = json.loads(self.store.read("user"))  # must be valid JSON
        self.assertEqual(data["name"], "Andre")
        self.assertEqual(data["favorite_colors"], "dark blue")

    def test_write_is_atomic_and_cleans_temp_files(self):
        import os

        self.store.write("user", json.dumps({"name": "Andre"}))
        self.assertEqual(json.loads(self.store.read("user")), {"name": "Andre"})
        leftovers = [f for f in os.listdir(self.tmp_dir) if f.startswith(".memory-")]
        self.assertEqual(leftovers, [], "atomic-write temp files must not leak")

    def test_upserting_name_clears_seed_marker(self):
        # After the user confirms their name, the seed guidance must stop
        # injecting — otherwise every new session re-asks name/color.
        seeded = {
            "name_source": "auto-detected from LibreOffice User Data or OS login; not yet confirmed by the user",
            "favorite_colors": "",
            "name": "andre",
        }
        self.store.write("user", json.dumps(seeded))

        res = MemoryTool().execute(self.ctx, key="name", content="André")
        self.assertEqual(res["status"], "ok")

        data = json.loads(self.store.read("user"))
        self.assertNotIn("name_source", data)
        self.assertEqual(data["name"], "André")

    def test_color_upsert_alone_keeps_seed_marker(self):
        seeded = {"name_source": "auto-detected", "favorite_colors": "", "name": "andre"}
        self.store.write("user", json.dumps(seeded))

        MemoryTool().execute(self.ctx, key="favorite_colors", content="blue")

        data = json.loads(self.store.read("user"))
        self.assertIn("name_source", data)  # name still unconfirmed → keep asking once


if __name__ == '__main__':
    unittest.main()
