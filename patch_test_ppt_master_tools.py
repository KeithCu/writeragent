--- tests/ppt_master/test_ppt_master_pptx_build.py
+++ tests/ppt_master/test_ppt_master_pptx_build.py
@@ -99,3 +99,14 @@
 def test_export_presentation_project_timeout_is_600():
     from plugin.ppt_master.tools import ExportPresentationProject
     assert ExportPresentationProject().timeout == 600.0
+
+def test_export_presentation_project_stop_checker():
+    from plugin.ppt_master.tools import ExportPresentationProject
+    from plugin.framework.tool import ToolContext
+
+    ctx = MagicMock(spec=ToolContext)
+    ctx.stop_checker = lambda: True
+
+    result = ExportPresentationProject().execute(ctx, project_path="/dummy")
+    assert result["status"] == "error"
+    assert result["code"] == "USER_STOPPED"
