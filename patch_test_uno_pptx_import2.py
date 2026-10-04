--- tests/ppt_master/test_uno_pptx_import.py
+++ tests/ppt_master/test_uno_pptx_import.py
@@ -244,6 +244,13 @@

 def test_stop_checker_cancels_import(monkeypatch):
     target, source, target_pages, _source_pages = _deck([["USER"]], 1)
+    called = {"copy": False}
+    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
+        called["copy"] = True
+        return 1
+
+    _patch_copy(monkeypatch, fake_copy)
+
     result = _import_slides_from_source(object(), target, source, stop_checker=lambda: True)
     assert result["status"] == "error"
     assert result["code"] == "USER_STOPPED"
+    assert called["copy"] is False
