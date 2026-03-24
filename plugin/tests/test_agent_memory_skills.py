import unittest
import os
import tempfile
import shutil
from unittest.mock import Mock

from plugin.modules.chatbot.memory import MemoryTool, MemoryStore
from plugin.modules.chatbot.skills import SkillManageTool, SkillsListTool, SkillViewTool, SkillsStore

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
        
        # Add to memory
        res = tool.execute(self.ctx, action="add", target="user", content="User likes Python.")
        self.assertEqual(res["status"], "ok", f"Expected ok but got {res}")
        
        # Read memory
        res = tool.execute(self.ctx, action="read", target="user")
        self.assertEqual(res["status"], "ok")
        self.assertIn("User likes Python.", res["content"])

        # Replace memory
        res = tool.execute(self.ctx, action="replace", target="user", content="User likes Rust.")
        self.assertEqual(res["status"], "ok")
        res = tool.execute(self.ctx, action="read", target="user")
        self.assertEqual(res["content"], "User likes Rust.")
        
        # Remove memory
        res = tool.execute(self.ctx, action="remove", target="user")
        self.assertEqual(res["status"], "ok")
        res = tool.execute(self.ctx, action="read", target="user")
        self.assertEqual(res["content"], "")


@unittest.skip("Disabled per user request")
class TestSkillsTool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)
        import uno
        uno.fileUrlToSystemPath = lambda x: x.replace("file://", "")
        
    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_skill_manage_and_list(self):
        manage_tool = SkillManageTool()
        list_tool = SkillsListTool()
        view_tool = SkillViewTool()
        
        content = "---\nname: my_skill\ndescription: A test skill\n---\n# My Skill\nDo the thing."
        
        # Create
        res = manage_tool.execute(self.ctx, action="create", name="my_skill", content=content)
        self.assertEqual(res["status"], "ok")
        
        # List
        res = list_tool.execute(self.ctx)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["skills"][0]["name"], "my_skill")
        self.assertEqual(res["skills"][0]["description"], "A test skill")
        
        # View
        res = view_tool.execute(self.ctx, name="my_skill")
        self.assertEqual(res["status"], "ok")
        self.assertIn("Do the thing.", res["content"])
        
        # Patch
        res = manage_tool.execute(self.ctx, action="patch", name="my_skill", old_string="thing", new_string="task")
        self.assertEqual(res["status"], "ok")
        
        res = view_tool.execute(self.ctx, name="my_skill")
        self.assertIn("Do the task.", res["content"])
        
        # Write file
        res = manage_tool.execute(self.ctx, action="write_file", name="my_skill", file_path="references/ref.md", file_content="reference")
        self.assertEqual(res["status"], "ok")
        
        # View file
        res = view_tool.execute(self.ctx, name="my_skill", file_path="references/ref.md")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["content"], "reference")
        
        # Delete
        res = manage_tool.execute(self.ctx, action="delete", name="my_skill")
        self.assertEqual(res["status"], "ok")

if __name__ == '__main__':
    unittest.main()
