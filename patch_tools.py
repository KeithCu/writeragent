--- plugin/ppt_master/tools.py
+++ plugin/ppt_master/tools.py
@@ -35,6 +35,8 @@
         return True

     def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
+        if getattr(ctx, "stop_checker", None) and ctx.stop_checker():
+            return self._tool_error("Cancelled by user", code="USER_STOPPED")
         apply_data_root_env(ctx.ctx)
         st = data_root_status(ctx.ctx)
         if not st.get("ok"):
@@ -46,7 +48,7 @@
         path = kwargs.get("project_path")
         if not path:
             return self._tool_error("project_path is required.", code="MISSING_PATH")
-        return export_project_to_impress(ctx.ctx, ctx.doc, path)
+        return export_project_to_impress(ctx.ctx, ctx.doc, path, stop_checker=getattr(ctx, "stop_checker", None))


 class ValidatePptMasterProject(ToolDrawPptMasterBase):
@@ -62,6 +64,8 @@
     }

     def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
+        if getattr(ctx, "stop_checker", None) and ctx.stop_checker():
+            return self._tool_error("Cancelled by user", code="USER_STOPPED")
         apply_data_root_env(ctx.ctx)
         path = kwargs.get("project_path")
         if not path:
@@ -83,6 +87,8 @@
     }

     def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
+        if getattr(ctx, "stop_checker", None) and ctx.stop_checker():
+            return self._tool_error("Cancelled by user", code="USER_STOPPED")
         apply_data_root_env(ctx.ctx)
         plan_path = kwargs.get("fill_plan_path")
         if not plan_path:
@@ -104,6 +110,8 @@
     }

     def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
+        if getattr(ctx, "stop_checker", None) and ctx.stop_checker():
+            return self._tool_error("Cancelled by user", code="USER_STOPPED")
         apply_data_root_env(ctx.ctx)
         path = kwargs.get("project_path")
         if not path:
