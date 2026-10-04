--- plugin/ppt_master/client.py
+++ plugin/ppt_master/client.py
@@ -7,7 +7,7 @@
 from __future__ import annotations

 from pathlib import Path
-from typing import Any
+from typing import Any, Callable

 from plugin.ppt_master.adapter.uno_enhance import apply_enhancement_project
 from plugin.ppt_master.adapter.uno_pptx_deck import export_project_to_doc
@@ -16,13 +16,13 @@
 from plugin.ppt_master.paths import apply_data_root_env


-def export_project_to_impress(ctx: Any, doc: Any, project_path: str | Path) -> dict[str, Any]:
+def export_project_to_impress(ctx: Any, doc: Any, project_path: str | Path, stop_checker: Callable[[], bool] | None = None) -> dict[str, Any]:
     """Apply a ppt-master project to the open Impress/Draw document via PPTX → ODP import."""
     apply_data_root_env(ctx)
     path = Path(project_path).expanduser().resolve()
     if not path.is_dir():
         return {"status": "error", "message": f"Project path not found: {path}"}
-    return export_project_to_doc(doc, path, ctx=ctx)
+    return export_project_to_doc(doc, path, ctx=ctx, stop_checker=stop_checker)


 def apply_template_fill(ctx: Any, doc: Any, plan_path: str | Path) -> dict[str, Any]:
