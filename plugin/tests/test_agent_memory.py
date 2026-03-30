import unittest
import tempfile
import shutil
from unittest.mock import Mock

from plugin.modules.chatbot.memory import MemoryTool, MemoryStore

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


@unittest.skip("Disabled per user request")
class TestMemoryTool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)
        import uno
        uno.fileUrlToSystemPath = lambda x: x.replace("file://", "")

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

if __name__ == '__main__':
    unittest.main()
