import unittest
import json
from plugin.framework.document import (
    DocumentCache,
    build_heading_tree,
    resolve_locator,
    get_paragraph_ranges
)

class ElementStub:
    def __init__(self, text, outline_level=0, services=None):
        self.text = text
        self.outline_level = outline_level
        self.services = services or ["com.sun.star.text.Paragraph"]
    def getString(self): return self.text
    def getPropertyValue(self, name):
        if name == "OutlineLevel": return self.outline_level
        raise Exception("Property not found")
    def supportsService(self, service): return service in self.services
    def getStart(self): return self # Stub for range
    def getEnd(self): return self
    def getText(self): return self

class WriterDocStub:
    def __init__(self, elements):
        self.elements = elements
        self.url = "test://writer"
    def getText(self): 
        class TextStub:
            def __init__(self, el): self.el = el
            def createEnumeration(self):
                class EnumStub:
                    def __init__(self, el): 
                        self.el = el
                        self.idx = 0
                    def hasMoreElements(self): return self.idx < len(self.el)
                    def nextElement(self):
                        res = self.el[self.idx]
                        self.idx += 1
                        return res
                return EnumStub(self.el)
        return TextStub(self.elements)
    def supportsService(self, s): return s == "com.sun.star.text.TextDocument"

class TestWriterNavigation(unittest.TestCase):
    def test_document_cache(self):
        model = WriterDocStub([])
        cache1 = DocumentCache.get(model)
        cache2 = DocumentCache.get(model)
        self.assertIs(cache1, cache2)
        
        DocumentCache.invalidate(model)
        cache3 = DocumentCache.get(model)
        self.assertIsNot(cache1, cache3)

    def test_build_heading_tree(self):
        elements = [
            ElementStub("H1", outline_level=1),
            ElementStub("P1"),
            ElementStub("H1.1", outline_level=2),
            ElementStub("P2"),
            ElementStub("H2", outline_level=1),
        ]
        doc = WriterDocStub(elements)
        tree = build_heading_tree(doc)
        
        # root -> [H1, H2]
        self.assertEqual(len(tree["children"]), 2)
        h1 = tree["children"][0]
        self.assertEqual(h1["text"], "H1")
        self.assertEqual(len(h1["children"]), 1)
        self.assertEqual(h1["children"][0]["text"], "H1.1")
        
        h2 = tree["children"][1]
        self.assertEqual(h2["text"], "H2")
        self.assertEqual(h2["body_paragraphs"], 0) # H2 is at end

    def test_get_paragraph_ranges_caching(self):
        doc = WriterDocStub([ElementStub("P1"), ElementStub("P2")])
        ranges1 = get_paragraph_ranges(doc)
        self.assertEqual(len(ranges1), 2)
        
        # Change underlying elements, but cache should remain
        doc.elements = [ElementStub("P3")]
        ranges2 = get_paragraph_ranges(doc)
        self.assertEqual(len(ranges2), 2)
        self.assertEqual(ranges1, ranges2)
        
        DocumentCache.invalidate(doc)
        ranges3 = get_paragraph_ranges(doc)
        self.assertEqual(len(ranges3), 1)

    def test_resolve_locator(self):
        doc = WriterDocStub([
            ElementStub("H1", outline_level=1),
            ElementStub("P1"),
            ElementStub("H2", outline_level=1),
            ElementStub("H2.1", outline_level=2),
        ])
        
        res = resolve_locator(doc, "paragraph:1")
        self.assertEqual(res["para_index"], 1)
        
        res = resolve_locator(doc, "heading:2")
        self.assertEqual(res["para_index"], 2) # H2 is at index 2
        
        res = resolve_locator(doc, "heading:2.1")
        self.assertEqual(res["para_index"], 3) # H2.1 is at index 3

if __name__ == "__main__":
    unittest.main()
