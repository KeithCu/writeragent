--- plugin/ppt_master/adapter/uno_pptx_import.py
+++ plugin/ppt_master/adapter/uno_pptx_import.py
@@ -10,7 +10,7 @@

 import logging
 from pathlib import Path
-from typing import Any
+from typing import Any, Callable

 from plugin.draw.draw_bridge import DrawBridge
 from plugin.draw.uno_shapes import (
@@ -147,6 +147,7 @@
     *,
     slide_indices: list[int] | None = None,
     clear_existing: bool = True,
+    stop_checker: Callable[[], bool] | None = None,
 ) -> dict[str, Any]:
     source_pages = source_doc.getDrawPages()
     source_count = int(source_pages.getCount())
@@ -165,6 +166,8 @@
     pages = bridge.get_pages()
     results: list[dict[str, Any]] = []
     for out_index, src_index in enumerate(indices):
+        if stop_checker is not None and stop_checker():
+            return {"status": "error", "message": "Import cancelled by user", "code": "USER_STOPPED"}
         source_page = source_pages.getByIndex(src_index)
         # What was wrong: the target slide was cleared before the copy, so a
         # copy that returned no shapes had already destroyed user content.
@@ -206,6 +209,7 @@
     *,
     clear_existing: bool = True,
     save_mirror_odp: Path | None = None,
+    stop_checker: Callable[[], bool] | None = None,
 ) -> dict[str, Any]:
     """Load PPTX hidden, copy all slides into *target_doc*, optionally write mirror ODP."""
     pptx_path = Path(pptx_path).expanduser().resolve()
@@ -218,7 +222,7 @@
             save_mirror_odp = Path(save_mirror_odp).expanduser().resolve()
             save_mirror_odp.parent.mkdir(parents=True, exist_ok=True)
             source_doc.storeToURL(save_mirror_odp.as_uri(), ())
-        result = _import_slides_from_source(ctx, target_doc, source_doc, clear_existing=clear_existing)
+        result = _import_slides_from_source(ctx, target_doc, source_doc, clear_existing=clear_existing, stop_checker=stop_checker)
         if result.get("status") == "ok":
             result["pptx_path"] = str(pptx_path)
             if save_mirror_odp is not None:
