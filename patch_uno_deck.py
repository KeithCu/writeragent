--- plugin/ppt_master/adapter/uno_pptx_deck.py
+++ plugin/ppt_master/adapter/uno_pptx_deck.py
@@ -8,13 +8,13 @@
 from __future__ import annotations

 from pathlib import Path
-from typing import Any
+from typing import Any, Callable

 from plugin.contrib.ppt_master.upstream import collect_svg_files
 from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_to_doc
 from plugin.ppt_master.paths import data_root_status
 from plugin.ppt_master.pptx_build import ensure_project_pptx


-def export_project_to_doc(doc: Any, project_path: Path, ctx: Any | None = None) -> dict[str, Any]:
+def export_project_to_doc(doc: Any, project_path: Path, ctx: Any | None = None, stop_checker: Callable[[], bool] | None = None) -> dict[str, Any]:
     """Export a ppt-master project into *doc* via PPTX → LO ODP import."""
     project_path = Path(project_path).expanduser().resolve()
     svg_files = collect_svg_files(project_path)
@@ -43,6 +43,7 @@
             pptx_path,
             clear_existing=True,
             save_mirror_odp=mirror_odp,
+            stop_checker=stop_checker,
         )
     )
