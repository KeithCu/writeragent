# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Search reach beyond the body text model — header/footer labeling and the comment sweep.

Pure control-flow tests with fakes (no LibreOffice). The UNO behavior itself (findFirst reaching
header/footer text, comments being invisible to it) was verified live; what is testable here is
the labeling logic of _header_footer_label / describe_match_location and the matching plus
defensiveness of comment_matches."""
from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.search import comment_matches, describe_match_location, _header_footer_label


# ---- fakes ----------------------------------------------------------------

class FakeHeadFootText:
    ImplementationName = "SwXHeadFootText"


class FakeBodyText:
    ImplementationName = "SwXBodyText"

    def createTextCursorByRange(self, rng):
        return FakeCursor()


class FakeCursor:
    def getPropertyValue(self, name):
        return None  # not in a table, not in a frame


class FakePageStyle:
    def __init__(self, name, header=None, footer=None, in_use=True):
        self._name = name
        self.HeaderText = header
        self.FooterText = footer
        self._in_use = in_use

    def isInUse(self):
        return self._in_use

    def getName(self):
        return self._name


class FakeStyles:
    def __init__(self, styles):
        self._styles = styles

    def getCount(self):
        return len(self._styles)

    def getByIndex(self, i):
        return self._styles[i]


class FakeDoc:
    def __init__(self, styles=(), fields=()):
        self._styles = FakeStyles(list(styles))
        self._fields = list(fields)

    def getStyleFamilies(self):
        outer = self

        class Fam:
            def getByName(self, name):
                assert name == "PageStyles"
                return outer._styles
        return Fam()

    def getTextFields(self):
        outer = self

        class Fields:
            def createEnumeration(self):
                return FakeEnum(outer._fields)
        return Fields()


class FakeEnum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


class FakeAnnotation:
    def __init__(self, content, author="Ana"):
        self.Content = content
        self.Author = author

    def supportsService(self, s):
        return s == "com.sun.star.text.textfield.Annotation"


class FakeOtherField:
    def supportsService(self, s):
        return False


class FakeRange:
    def __init__(self, text):
        self._text = text

    def getText(self):
        return self._text

    def getStart(self):
        return object()


# ---- _header_footer_label --------------------------------------------------

def test_non_headfoot_text_returns_none():
    assert _header_footer_label(FakeBodyText(), FakeDoc()) is None


def test_header_found_with_style_name():
    hf = FakeHeadFootText()
    doc = FakeDoc(styles=[FakePageStyle("Standard", header=hf)])
    assert _header_footer_label(hf, doc) == "header (page style 'Standard')"


def test_footer_found_with_style_name():
    hf = FakeHeadFootText()
    doc = FakeDoc(styles=[FakePageStyle("Landscape", footer=hf)])
    assert _header_footer_label(hf, doc) == "footer (page style 'Landscape')"


def test_unused_styles_are_skipped():
    hf = FakeHeadFootText()
    unused = FakePageStyle("Old", header=hf, in_use=False)
    used = FakePageStyle("Standard", footer=hf)
    doc = FakeDoc(styles=[unused, used])
    assert _header_footer_label(hf, doc) == "footer (page style 'Standard')"


def test_style_walk_failure_still_labels_generically():
    hf = FakeHeadFootText()
    assert _header_footer_label(hf, doc=None) == "header or footer"
    assert _header_footer_label(hf, FakeDoc(styles=[])) == "header or footer"


# ---- describe_match_location with header text ------------------------------

def test_location_reports_header_not_body():
    class HeadTextWithCursor(FakeHeadFootText):
        def createTextCursorByRange(self, rng):
            return FakeCursor()

    hf = HeadTextWithCursor()
    doc = FakeDoc(styles=[FakePageStyle("Standard", header=hf)])
    assert describe_match_location(FakeRange(hf), doc) == "header (page style 'Standard')"


def test_location_body_unchanged_without_doc():
    assert describe_match_location(FakeRange(FakeBodyText())) == "body"


def test_location_reports_table_cell():
    table = MagicMock()
    table.getName.return_value = "Table1"
    cur = MagicMock()

    def _get_prop(name):
        if name == "TextTable":
            return table
        if name == "CellName":
            return "B2"
        return None

    cur.getPropertyValue.side_effect = _get_prop
    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.return_value = cur
    assert describe_match_location(found) == "table 'Table1' cell B2"


# ---- comment_matches --------------------------------------------------------

def test_comment_match_yields_hit_author_content():
    doc = FakeDoc(fields=[FakeAnnotation("please FIX this paragraph", author="Ana")])
    got = list(comment_matches(doc, "fix", use_regex=False, case_sensitive=False))
    assert got == [("FIX", "Ana", "please FIX this paragraph")]


def test_comment_no_match_yields_nothing():
    doc = FakeDoc(fields=[FakeAnnotation("nothing here")])
    assert list(comment_matches(doc, "absent", False, False)) == []


def test_non_annotation_fields_are_skipped():
    doc = FakeDoc(fields=[FakeOtherField(), FakeAnnotation("target inside", author="Bo")])
    got = list(comment_matches(doc, "target", False, False))
    assert got == [("target", "Bo", "target inside")]


def test_multiple_hits_in_one_comment():
    doc = FakeDoc(fields=[FakeAnnotation("x a x b x")])
    got = list(comment_matches(doc, "x", False, False))
    assert [g[0] for g in got] == ["x", "x", "x"]


def test_comment_regex_matching():
    doc = FakeDoc(fields=[FakeAnnotation("codes A1 and B2 here")])
    got = list(comment_matches(doc, r"[A-Z]\d", use_regex=True, case_sensitive=True))
    assert [g[0] for g in got] == ["A1", "B2"]


def test_enumeration_failure_is_silent():
    class BrokenDoc:
        def getTextFields(self):
            raise RuntimeError("no fields")
    assert list(comment_matches(BrokenDoc(), "x", False, False)) == []
