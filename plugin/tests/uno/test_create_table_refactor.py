import unittest
import uno
from plugin.main import get_tools
from plugin.framework.service_registry import ServiceRegistry
from plugin.framework.tool_context import ToolContext

class TestCreateTableRefactor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import officehelper
            cls.ctx = officehelper.bootstrap()
            from plugin.main import bootstrap, get_tools
            bootstrap(cls.ctx)
            cls.registry = get_tools()
        except Exception:
            cls.ctx = None

    def setUp(self):
        if self.ctx is None:
            self.skipTest("LibreOffice not available")
        from plugin.framework.uno_context import get_desktop
        desktop = get_desktop(self.ctx)
        self.doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
        self.services = self.registry._services
        self.tool_ctx = ToolContext(self.doc, self.ctx, "writer", self.services)

    def tearDown(self):
        if hasattr(self, "doc") and self.doc:
            try:
                self.doc.close(True)
            except Exception:
                pass

    def test_create_table_beginning(self):
        tool = self.registry.get("create_table")
        result = tool.execute(self.tool_ctx, rows=2, cols=3, target="beginning")
        self.assertEqual(result["status"], "ok")
        
        tables = self.doc.getTextTables()
        self.assertEqual(tables.getCount(), 1)

    def test_create_table_end(self):
        tool = self.registry.get("create_table")
        result = tool.execute(self.tool_ctx, rows=2, cols=3, target="end")
        self.assertEqual(result["status"], "ok")
        
        tables = self.doc.getTextTables()
        self.assertEqual(tables.getCount(), 1)

    def test_create_table_search(self):
        # Insert some text to search for
        cursor = self.doc.getText().createTextCursor()
        self.doc.getText().insertString(cursor, "Target location here.", False)
        
        tool = self.registry.get("create_table")
        result = tool.execute(self.tool_ctx, rows=2, cols=3, target="search", old_content="Target location")
        self.assertEqual(result["status"], "ok")
        
        tables = self.doc.getTextTables()
        self.assertEqual(tables.getCount(), 1)

    def test_create_table_selection(self):
        tool = self.registry.get("create_table")
        
        text = self.doc.getText()
        text.setString("Some text to select")
        
        # Select "text"
        controller = self.doc.getCurrentController()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.goRight(5, False)
        cursor.goRight(4, True)
        controller.select(cursor)
        
        result = tool.execute(self.tool_ctx, rows=2, cols=2, target="selection")
        self.assertEqual(result["status"], "ok")
        
        # In headless/bootstrapped mode, controller.select might not reflect in doc.getCurrentSelection()
        # but the tool tries to handle it.
        tables = self.doc.getTextTables()
        self.assertEqual(tables.getCount(), 1)

if __name__ == "__main__":
    unittest.main()
