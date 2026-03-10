import traceback

from plugin.framework.document import (
    DocumentCache,
    build_heading_tree,
    resolve_locator,
    get_paragraph_ranges,
)
from plugin.framework.uno_helpers import get_desktop


def run_writer_tests(ctx, doc=None):
    """Entry point for testing the core writer module functionality inside LibreOffice."""
    passed = 0
    failed = 0
    log = []

    def ok(msg):
        log.append("OK: " + msg)

    def fail(msg):
        log.append("FAIL: " + msg)

    try:
        log.append("Starting Writer Tests...")

        # Always create a hidden test writer document to avoid mutating user's open document
        close_doc = True
        desktop = get_desktop(ctx)
        from com.sun.star.beans import PropertyValue
        hidden_prop = PropertyValue()
        hidden_prop.Name = "Hidden"
        hidden_prop.Value = True
        doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
        if not doc:
            raise Exception("Could not create hidden test writer document")

        cache3 = DocumentCache.get(doc)

        # 1. Setup doc content
        try:
            text = doc.getText()
            cursor = text.createTextCursor()

            # H1
            text.insertString(cursor, "H1", False)
            cursor.setPropertyValue("ParaStyleName", "Heading 1")
            text.insertControlCharacter(cursor, 0, False) # PARAGRAPH_BREAK

            # P1
            text.insertString(cursor, "P1", False)
            cursor.setPropertyValue("ParaStyleName", "Default Paragraph Style")
            text.insertControlCharacter(cursor, 0, False)

            # H1.1
            text.insertString(cursor, "H1.1", False)
            cursor.setPropertyValue("ParaStyleName", "Heading 2")
            text.insertControlCharacter(cursor, 0, False)

            # P2
            text.insertString(cursor, "P2", False)
            cursor.setPropertyValue("ParaStyleName", "Default Paragraph Style")
            text.insertControlCharacter(cursor, 0, False)

            # H2
            text.insertString(cursor, "H2", False)
            cursor.setPropertyValue("ParaStyleName", "Heading 1")
        except Exception as e:
            failed += 1
            fail(f"Writer Test Setup failed: {type(e).__name__}: {e!r}")
            return passed, failed, log

        # Populate cache.length so DummyDocSvc and cache test can use it
        from plugin.framework.document import get_document_length
        get_document_length(doc)

        try:
            # Test Proximity Service
            from plugin.modules.writer.proximity import ProximityService
            from plugin.modules.writer.bookmarks import BookmarkService
            from plugin.modules.writer.tree import TreeService
            from plugin.framework.events import EventBus

            events = EventBus()
            class DummyDocSvc:
                def get_document_length(self, model):
                    return cache3.length

            # Using real instances except for doc_svc which only needs get_document_length
            doc_svc = DummyDocSvc()
            bm = BookmarkService(doc_svc, events)
            tree_svc = TreeService(doc_svc, bm, events)
            prox = ProximityService(doc_svc, tree_svc, bm, events)

            res = prox.get_context_at_offset(doc, 0)
            if res and res["paragraph_index"] == 0:
                passed += 1
                ok("ProximityService get_context_at_offset passed")
            else:
                failed += 1
                fail(f"ProximityService get_context_at_offset failed: {res}")
        except Exception as e:
            failed += 1
            fail(f"ProximityService test failed: {e}\n{traceback.format_exc()}")

        try:
            # Test format_support content_has_markup
            from plugin.modules.writer.format_support import content_has_markup
            if content_has_markup("**bold**") and not content_has_markup("plain text"):
                passed += 1
                ok("content_has_markup passed")
            else:
                failed += 1
                fail("content_has_markup failed")
        except Exception as e:
            failed += 1
            fail(f"content_has_markup test failed: {e}")

        try:
            # Test Bookmarks & Indexing
            from plugin.modules.writer.bookmarks import ensure_heading_bookmarks
            ensure_heading_bookmarks(doc)
            bookmarks = doc.getBookmarks()
            bnames = bookmarks.getElementNames()
            if len(bnames) == 3: # H1, H1.1, H2
                passed += 1
                ok("ensure_heading_bookmarks created 3 bookmarks")
            else:
                failed += 1
                fail(f"ensure_heading_bookmarks created {len(bnames)} bookmarks instead of 3")

            # Running ensure_heading_bookmarks again should not duplicate
            ensure_heading_bookmarks(doc)
            bnames = doc.getBookmarks().getElementNames()
            if len(bnames) == 3:
                passed += 1
                ok("ensure_heading_bookmarks did not duplicate bookmarks")
            else:
                failed += 1
                fail(f"ensure_heading_bookmarks duplicated bookmarks, total: {len(bnames)}")
        except Exception as e:
            failed += 1
            fail(f"ensure_heading_bookmarks test failed: {e}")

        try:
            # 3. Test paragraph ranges
            ranges = get_paragraph_ranges(doc)
            if len(ranges) == 5:
                passed += 1
                ok("get_paragraph_ranges found 5 paragraphs")
            else:
                failed += 1
                fail(f"get_paragraph_ranges expected 5 paragraphs, got {len(ranges)}")
        except Exception as e:
            failed += 1
            fail(f"get_paragraph_ranges test failed: {e}")

        try:
            # 4. Test heading tree
            tree = build_heading_tree(doc)
            if "children" in tree and len(tree["children"]) == 2:
                h1 = tree["children"][0]
                h2 = tree["children"][1]
                if h1["text"] == "H1" and len(h1["children"]) == 1 and h1["children"][0]["text"] == "H1.1" and h2["text"] == "H2" and h2["body_paragraphs"] == 0:
                    passed += 1
                    ok("build_heading_tree constructed correct tree")
                else:
                    failed += 1
                    fail(f"build_heading_tree structure incorrect: {tree}")
            else:
                failed += 1
                fail("build_heading_tree did not find 2 root children")
        except Exception as e:
            failed += 1
            fail(f"build_heading_tree test failed: {e}")

        try:
            # 5. Test resolve locator
            res1 = resolve_locator(doc, "paragraph:1")
            if res1 and res1["para_index"] == 1:
                passed += 1
                ok("resolve_locator paragraph:1 passed")
            else:
                failed += 1
                fail(f"resolve_locator paragraph:1 failed: {res1}")

            res2 = resolve_locator(doc, "heading:2") # should be index 4 (H2)
            if res2 and res2["para_index"] == 4:
                passed += 1
                ok("resolve_locator heading:2 passed")
            else:
                failed += 1
                fail(f"resolve_locator heading:2 failed: {res2}")

            res3 = resolve_locator(doc, "heading:1.1") # should be index 2 (H1.1)
            if res3 and res3["para_index"] == 2:
                passed += 1
                ok("resolve_locator heading:1.1 passed")
            else:
                failed += 1
                fail(f"resolve_locator heading:1.1 failed: {res3}")
        except Exception as e:
            failed += 1
            fail(f"resolve_locator test failed: {e}")

        try:
            # 6. Test Document Cache length tracking
            _ = get_document_length(doc)
            prev_len = cache3.length
            if prev_len is not None and prev_len > 0:
                text.insertControlCharacter(cursor, 0, False)
                text.insertString(cursor, "More text", False)
                DocumentCache.invalidate(doc)
                _ = get_document_length(doc)
                cache3_new = DocumentCache.get(doc)
                new_len = cache3_new.length
                if new_len is not None and new_len > prev_len:
                    passed += 1
                    ok("DocumentCache length updated after invalidate and get_document_length")
                else:
                    failed += 1
                    fail("DocumentCache length did not update")
            else:
                failed += 1
                fail("DocumentCache length not properly initialized")
        except Exception as e:
            failed += 1
            fail(f"DocumentCache cache tracking test failed: {e}")

        if close_doc:
            doc.close(True)

    except Exception as e:
        failed += 1
        fail(f"Exception during tests: {e}\n{traceback.format_exc()}")

    return passed, failed, log
