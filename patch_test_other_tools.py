--- tests/ppt_master/test_ppt_master_pptx_build.py
+++ tests/ppt_master/test_ppt_master_pptx_build.py
@@ -109,3 +109,30 @@
     result = ExportPresentationProject().execute(ctx, project_path="/dummy")
     assert result["status"] == "error"
     assert result["code"] == "USER_STOPPED"
+
+def test_validate_ppt_master_project_stop_checker():
+    from plugin.ppt_master.tools import ValidatePptMasterProject
+    from plugin.framework.tool import ToolContext
+    ctx = MagicMock(spec=ToolContext)
+    ctx.stop_checker = lambda: True
+    result = ValidatePptMasterProject().execute(ctx, project_path="/dummy")
+    assert result["status"] == "error"
+    assert result["code"] == "USER_STOPPED"
+
+def test_apply_ppt_master_template_fill_stop_checker():
+    from plugin.ppt_master.tools import ApplyPptMasterTemplateFill
+    from plugin.framework.tool import ToolContext
+    ctx = MagicMock(spec=ToolContext)
+    ctx.stop_checker = lambda: True
+    result = ApplyPptMasterTemplateFill().execute(ctx, fill_plan_path="/dummy")
+    assert result["status"] == "error"
+    assert result["code"] == "USER_STOPPED"
+
+def test_apply_ppt_master_native_enhance_stop_checker():
+    from plugin.ppt_master.tools import ApplyPptMasterNativeEnhance
+    from plugin.framework.tool import ToolContext
+    ctx = MagicMock(spec=ToolContext)
+    ctx.stop_checker = lambda: True
+    result = ApplyPptMasterNativeEnhance().execute(ctx, project_path="/dummy")
+    assert result["status"] == "error"
+    assert result["code"] == "USER_STOPPED"
