--- tests/ppt_master/test_uno_pptx_import.py
+++ tests/ppt_master/test_uno_pptx_import.py
@@ -194,6 +194,13 @@
     assert result["status"] == "error"
     assert "no slides" in result["message"]
     assert target_pages[0].shapes == ["USER"]
+
+def test_stop_checker_cancels_import(monkeypatch):
+    target, source, target_pages, _source_pages = _deck([["USER"]], 1)
+    result = _import_slides_from_source(object(), target, source, stop_checker=lambda: True)
+    assert result["status"] == "error"
+    assert result["code"] == "USER_STOPPED"
+    assert target_pages[0].shapes == ["USER"]


 class DisposedException(Exception):
