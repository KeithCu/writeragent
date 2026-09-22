# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Outline-hyperlink capture/restore without LibreOffice.

The fake document is only a text cursor and portion enumeration — enough to
run capture_outline_hyperlinks / restore_outline_hyperlinks. Substitution
rules are tested on the pure planner as well.
"""

from plugin.writer.hyperlink_fixup import (
    OutlineLink,
    capture_outline_hyperlinks,
    plan_outline_updates,
    restore_outline_hyperlinks,
    substitute_if_present,
)


_OUTLINE = "#1.Old title|outline"
_BOOKMARK = "#__RefHeading___Toc123"


def test_substitute_if_present_uses_the_matched_text():
    assert substitute_if_present(_OUTLINE, "Old title", "New title") == "#1.New title|outline"
    assert substitute_if_present(_BOOKMARK, "Old title", "New title") is None
    assert substitute_if_present(_OUTLINE, "", "New title") is None


def test_substitute_skips_the_outline_suffix_and_a_repeated_title():
    """str.replace would turn |outline into |outrow, and rename both copies of a repeated word."""
    assert substitute_if_present("#1.The line.|outline", "line", "row") == "#1.The row.|outline"
    assert substitute_if_present(
        "#1.outline of the method|outline", "outline", "summary") == "#1.summary of the method|outline"
    assert substitute_if_present("#1.foo foo|outline", "foo", "bar") is None


def test_plan_leaves_a_repeated_title_and_a_repeated_name():
    repeated = OutlineLink("#1.foo foo|outline", "foo", "foo")
    plans = plan_outline_updates([repeated], "foo", "bar", None)
    assert plans[0]["new_url"] is None
    assert plans[0]["hyperlink_updated"] is False
    once = OutlineLink("#1.foo|outline", "foo foo", "foo")
    plans = plan_outline_updates([once], "foo", "bar", None)
    assert plans[0]["new_url"] == "#1.bar|outline"
    assert plans[0]["new_name"] is None
    assert plans[0]["new_target"] == "bar"


def test_plan_skips_bookmark_and_a_url_that_does_not_contain_the_match():
    bookmark = OutlineLink(_BOOKMARK, "Old title", "")
    unrelated = OutlineLink("#1.Other|outline", "Old title", "Old title")
    assert plan_outline_updates([bookmark], "Old title", "New title", None) == []
    plans = plan_outline_updates([unrelated], "Old title", "New title", None)
    assert plans[0]["new_url"] is None
    assert plans[0]["new_name"] is None
    assert plans[0]["hyperlink_updated"] is False
    assert plans[0]["hyperlink_url_after"] == "#1.Other|outline"


def test_plan_rewrites_url_and_only_name_or_target_that_contain_the_match():
    link = OutlineLink(_OUTLINE, "Old title", "keep")
    plans = plan_outline_updates([link], "Old title", "New title", None)
    assert plans[0]["new_url"] == "#1.New title|outline"
    assert plans[0]["new_name"] == "New title"
    assert plans[0]["new_target"] is None
    assert plans[0]["hyperlink_updated"] is True


def test_plan_override_replaces_the_url_without_substitution():
    link = OutlineLink(_OUTLINE, "Old title", "Old title")
    plans = plan_outline_updates([link], "Old title", "New title", "#9.Custom|outline")
    assert plans[0]["new_url"] == "#9.Custom|outline"
    assert plans[0]["new_name"] == "New title"
    assert plans[0]["new_target"] == "New title"


class _Pos:
    def __init__(self, n):
        self.n = n


class _Slice:
    def __init__(self, text, url="", name="", target=""):
        self.text = text
        self.url = url
        self.name = name
        self.target = target


class _Enum:
    def __init__(self, items):
        self._items = list(items)
        self._i = 0

    def hasMoreElements(self):
        return self._i < len(self._items)

    def nextElement(self):
        item = self._items[self._i]
        self._i += 1
        return item


class _Doc:
    """One paragraph of slices. compareRegion* uses the point index, matching collapsed positions."""

    def __init__(self, slices):
        self.slices = slices

    def text(self):
        return "".join(sl.text for sl in self.slices)

    def slice_bounds(self, sl):
        start = 0
        for item in self.slices:
            end = start + len(item.text)
            if item is sl:
                return start, end
            start = end
        raise KeyError(sl)

    def _n(self, obj):
        return obj.n if isinstance(obj, _Pos) else obj.start

    def compareRegionStarts(self, left, right):
        a, b = self._n(left), self._n(right)
        if a < b:
            return 1
        if a > b:
            return -1
        return 0

    def compareRegionEnds(self, left, right):
        return self.compareRegionStarts(left, right)

    def createTextCursorByRange(self, pos):
        n = self._n(pos)
        return _Cursor(self, n, n)


class _Cursor:
    def __init__(self, doc, start, end):
        self.doc = doc
        self.start = start
        self.end = end

    def getText(self):
        return self.doc

    def getStart(self):
        return _Pos(self.start)

    def getEnd(self):
        return _Pos(self.end)

    def getString(self):
        return self.doc.text()[self.start:self.end]

    def gotoStartOfParagraph(self, expand):
        if expand:
            self.start = 0
        else:
            self.start = 0
            self.end = 0

    def gotoEndOfParagraph(self, expand):
        end = len(self.doc.text())
        if expand:
            self.end = end
        else:
            self.start = end
            self.end = end

    def gotoRange(self, pos, expand):
        n = self.doc._n(pos)
        if not expand:
            self.start = n
            self.end = n
            return
        if n < self.start:
            self.start = n
        else:
            self.end = n

    def goRight(self, count, expand):
        nxt = self.end + count
        if nxt > len(self.doc.text()):
            self.end = len(self.doc.text())
            return False
        if expand:
            self.end = nxt
        else:
            self.start = nxt
            self.end = nxt
        return True

    def createEnumeration(self):
        return _Enum([_Paragraph(self.doc)])

    def setPropertyValue(self, name, value):
        for sl in self.doc.slices:
            start, end = self.doc.slice_bounds(sl)
            if start < self.end and end > self.start:
                if name == "HyperLinkURL":
                    sl.url = value
                elif name == "HyperLinkName":
                    sl.name = value
                elif name == "HyperLinkTarget":
                    sl.target = value


class _Paragraph:
    def __init__(self, doc):
        self.doc = doc

    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"

    def createEnumeration(self):
        return _Enum(_Portion(self.doc, sl) for sl in self.doc.slices)


class _Portion:
    def __init__(self, doc, sl):
        self.doc = doc
        self.sl = sl

    def getText(self):
        return self.doc

    def getString(self):
        return self.sl.text

    def getStart(self):
        start, _end = self.doc.slice_bounds(self.sl)
        return _Pos(start)

    def getEnd(self):
        _start, end = self.doc.slice_bounds(self.sl)
        return _Pos(end)

    def getPropertyValue(self, name):
        if name == "HyperLinkURL":
            return self.sl.url
        if name == "HyperLinkName":
            return self.sl.name
        if name == "HyperLinkTarget":
            return self.sl.target
        if name == "TextPortionType":
            return "Text"
        raise AttributeError(name)


def _match(doc, start, end):
    return _Cursor(doc, start, end)


def _replace_equal(sl, new_text):
    assert len(new_text) == len(sl.text)
    sl.text = new_text


def test_capture_reads_only_the_overlapping_outline_link():
    prefix = _Slice("2.3.4. ", _OUTLINE)
    title = _Slice("Old title", _OUTLINE, "Old title", "keep")
    bookmark = _Slice(" tail", _BOOKMARK)
    doc = _Doc([prefix, title, bookmark])
    # "Old title" starts after the 7-character prefix.
    snapshot = capture_outline_hyperlinks(_match(doc, 7, 16))
    assert [link.url for link in snapshot.links] == [_OUTLINE]
    assert snapshot.links[0].name == "Old title"
    assert snapshot.matched == "Old title"
    assert snapshot.prefix == "2.3.4. "
    assert snapshot.suffix == " tail"
    assert snapshot.single_paragraph is True


def test_restore_rewrites_the_whole_outline_span_and_leaves_the_bookmark():
    prefix = _Slice("2.3.4. ", _OUTLINE)
    title = _Slice("Old title", _OUTLINE, "Old title", "keep")
    bookmark = _Slice(" tail", _BOOKMARK, "Old title", "")
    doc = _Doc([prefix, title, bookmark])
    match = _match(doc, 7, 16)
    snapshot = capture_outline_hyperlinks(match)
    _replace_equal(title, "New title")
    reports = restore_outline_hyperlinks(_match(doc, 7, 7), snapshot, "New title", None)
    assert prefix.url == "#1.New title|outline"
    assert title.url == "#1.New title|outline"
    assert title.name == "New title"
    assert title.target == "keep"
    assert bookmark.url == _BOOKMARK
    assert bookmark.name == "Old title"
    assert reports[0]["hyperlink_updated"] is True
    assert reports[0]["hyperlink_url_after"] == "#1.New title|outline"


def test_restore_repaints_a_run_whose_url_the_html_path_dropped():
    """setString('') + import leaves the new characters with no HyperLinkURL."""
    prefix = _Slice("2.3.4. ", _OUTLINE)
    title = _Slice("Old title", _OUTLINE)
    suffix = _Slice("\t42", _OUTLINE)
    doc = _Doc([prefix, title, suffix])
    snapshot = capture_outline_hyperlinks(_match(doc, 7, 16))
    _replace_equal(title, "New title")
    title.url = ""
    restore_outline_hyperlinks(_match(doc, 7, 7), snapshot, "New title", None)
    assert prefix.url == "#1.New title|outline"
    assert title.url == "#1.New title|outline"
    assert suffix.url == "#1.New title|outline"


def test_restore_leaves_the_url_when_the_matched_text_is_not_in_it():
    title = _Slice("Old title", "#1.Other|outline", "Old title", "")
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    _replace_equal(title, "New title")
    title.url = ""
    reports = restore_outline_hyperlinks(_match(doc, 0, 0), snapshot, "New title", None)
    assert title.url == "#1.Other|outline"
    assert title.name == "Old title"
    assert reports[0]["hyperlink_updated"] is False


def test_restore_puts_a_bookmark_url_back_when_the_replace_clears_it():
    """setString drops HyperLinkURL on the characters it rewrites. A bookmark target stays."""
    title = _Slice("Old title", _BOOKMARK)
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    assert snapshot.links == []
    assert snapshot.preserve_url == _BOOKMARK
    _replace_equal(title, "New title")
    title.url = ""
    reports = restore_outline_hyperlinks(_match(doc, 0, 0), snapshot, "New title", None)
    assert title.url == _BOOKMARK
    assert reports == []


def test_restore_override_sets_the_exact_url():
    title = _Slice("Old title", _OUTLINE, "Old title", "")
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    _replace_equal(title, "New title")
    reports = restore_outline_hyperlinks(
        _match(doc, 0, 0), snapshot, "New title", "#9.Custom|outline")
    assert title.url == "#9.Custom|outline"
    assert reports[0]["hyperlink_url_after"] == "#9.Custom|outline"


def test_restore_does_not_paint_an_override_onto_a_bookmark():
    title = _Slice("Old title", _BOOKMARK)
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    _replace_equal(title, "New title")
    title.url = ""
    reports = restore_outline_hyperlinks(
        _match(doc, 0, 0), snapshot, "New title", "#9.Custom|outline")
    assert title.url == _BOOKMARK
    assert reports == []


def test_restore_accepts_deleting_the_whole_outline_link():
    """No portion remains to paint. The delete must not raise."""
    title = _Slice("Old title", _OUTLINE)
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    title.text = ""
    title.url = ""
    reports = restore_outline_hyperlinks(_match(doc, 0, 0), snapshot, "", None)
    assert title.url == ""
    assert reports[0]["hyperlink_url_after"] == "#1.|outline"


def test_restore_does_not_paint_one_outline_url_across_a_second_link():
    """The spanning phrase is in the first URL only. The second link must keep its target."""
    left = _Slice("Alpha ", "#1.Alpha Beta|outline")
    right = _Slice("Beta", "#2.Beta|outline")
    doc = _Doc([left, right])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 10))
    assert [link.url for link in snapshot.links] == [
        "#1.Alpha Beta|outline", "#2.Beta|outline"]
    _replace_equal(left, "Gamma ")
    restore_outline_hyperlinks(_match(doc, 0, 0), snapshot, "Gamma Beta", None)
    assert left.url == "#1.Gamma Beta|outline"
    assert right.url == "#2.Beta|outline"


def test_restore_does_not_put_the_first_url_on_a_neighbor_it_does_not_contain():
    """Neither URL contains the spanning phrase, so neither is written onto the other."""
    left = _Slice("Alpha", "#1.Alpha|outline")
    right = _Slice("Beta", "#2.Beta|outline")
    doc = _Doc([left, right])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    restore_outline_hyperlinks(_match(doc, 0, 0), snapshot, "AlphaBeta", None)
    assert left.url == "#1.Alpha|outline"
    assert right.url == "#2.Beta|outline"


def test_restore_does_not_paint_a_bookmark_across_an_overlapping_outline_link():
    left = _Slice("Alpha ", "#1.Other|outline")
    right = _Slice("Beta", _BOOKMARK)
    doc = _Doc([left, right])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 10))
    assert snapshot.preserve_url == _BOOKMARK
    assert len(snapshot.links) == 1
    _replace_equal(left, "Gamma ")
    restore_outline_hyperlinks(_match(doc, 0, 0), snapshot, "Gamma Beta", None)
    assert left.url == "#1.Other|outline"
    assert right.url == _BOOKMARK


def test_capture_outline_hyperlinks_magicmock_range_does_not_spin():
    """MagicMock.hasMoreElements() is truthy; the walker must stop (PR #823 merge CI)."""
    from unittest.mock import MagicMock

    snap = capture_outline_hyperlinks(MagicMock())
    assert snap.links == []
