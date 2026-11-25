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

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.framework.errors import ToolExecutionError
from plugin.writer.hyperlink_fixup import (
    OutlineLink,
    OutlineSnapshot,
    _paint,
    _paragraph_portions,
    _renamed_url,
    capture_outline_hyperlinks,
    heading_renames,
    plan_outline_updates,
    public_hyperlink_reports,
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
    assert public_hyperlink_reports(plans)[0]["hyperlink_updated"] is False
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
    assert public_hyperlink_reports(plans)[0]["hyperlink_updated"] is False
    assert public_hyperlink_reports(plans)[0]["hyperlink_url_after"] == "#1.Other|outline"


def test_plan_rewrites_url_and_only_name_or_target_that_contain_the_match():
    link = OutlineLink(_OUTLINE, "Old title", "keep")
    plans = plan_outline_updates([link], "Old title", "New title", None)
    assert plans[0]["new_url"] == "#1.New title|outline"
    assert plans[0]["new_name"] == "New title"
    assert plans[0]["new_target"] is None
    assert public_hyperlink_reports(plans)[0]["hyperlink_updated"] is True


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
    def __init__(self, text, url="", name="", target="", color=None, underline=None,
                 text_type="Text", raise_props=False, direct=True, link_style=""):
        self.text = text
        self.url = url
        self.name = name
        self.target = target
        self.color = color
        self.underline = underline
        self.text_type = text_type
        self.raise_props = raise_props
        self.direct = direct
        self.link_style = link_style


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

    def __init__(self, slices, stomp_look_on_url=False):
        self.slices = slices
        # LibreOffice reapplies Internet-link defaults when HyperLinkURL is set.
        self.stomp_look_on_url = stomp_look_on_url
        self.writes = []

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

    def getPropertyValue(self, name):
        for sl in self.doc.slices:
            start, end = self.doc.slice_bounds(sl)
            if start < self.end and end > self.start:
                if name == "HyperLinkURL":
                    return sl.url
                if name == "HyperLinkName":
                    return sl.name
                if name == "HyperLinkTarget":
                    return sl.target
                if name == "CharColor":
                    return sl.color
                if name == "CharUnderline":
                    return sl.underline
                if name in ("UnvisitedCharStyleName", "VisitedCharStyleName"):
                    return sl.link_style or None
        raise AttributeError(name)

    def createEnumeration(self):
        return _Enum([_Paragraph(self.doc)])

    def setPropertyValue(self, name, value):
        self.doc.writes.append((name, value))
        for sl in self.doc.slices:
            start, end = self.doc.slice_bounds(sl)
            if start < self.end and end > self.start:
                if name == "HyperLinkURL":
                    sl.url = value
                    if self.doc.stomp_look_on_url:
                        sl.color = 128
                        sl.underline = 1
                        sl.link_style = "Internet Link"
                elif name == "HyperLinkName":
                    sl.name = value
                elif name == "HyperLinkTarget":
                    sl.target = value
                elif name == "CharColor":
                    sl.color = value
                elif name == "CharUnderline":
                    sl.underline = value
                elif name in ("UnvisitedCharStyleName", "VisitedCharStyleName"):
                    sl.link_style = value

    def setPropertyToDefault(self, name):
        self.doc.writes.append((f"DEFAULT:{name}", None))
        for sl in self.doc.slices:
            start, end = self.doc.slice_bounds(sl)
            if start < self.end and end > self.start and name == "HyperLinkURL":
                sl.url = ""
                sl.name = ""
                sl.target = ""
                sl.link_style = ""


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

    def getPropertyState(self, name):
        if self.sl.raise_props:
            raise AttributeError(name)
        val = None
        if name == "CharColor":
            val = self.sl.color
        elif name == "CharUnderline":
            val = self.sl.underline
        elif hasattr(self.sl, name):
            val = getattr(self.sl, name)
        if self.sl.direct and val is not None:
            return SimpleNamespace(value="DIRECT_VALUE")
        return SimpleNamespace(value="DEFAULT_VALUE")

    def getPropertyValue(self, name):
        if name == "TextPortionType":
            return self.sl.text_type
        if name in ("RedlineType", "IsStart") and hasattr(self.sl, name):
            return getattr(self.sl, name)
        if self.sl.raise_props:
            raise AttributeError(name)
        if name == "HyperLinkURL":
            return self.sl.url
        if name == "HyperLinkName":
            return self.sl.name
        if name == "HyperLinkTarget":
            return self.sl.target
        if name == "CharColor":
            return self.sl.color
        if name == "CharUnderline":
            return self.sl.underline
        if name in ("UnvisitedCharStyleName", "VisitedCharStyleName"):
            return self.sl.link_style or None
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
    reports = restore_outline_hyperlinks(doc, _match(doc, 7, 7), snapshot, "New title", None)
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
    prefix = _Slice("2.3.4. ", _OUTLINE, link_style="Index Link")
    title = _Slice("Old title", _OUTLINE, name="Old title", target="_blank", link_style="Index Link")
    suffix = _Slice("\t42", _OUTLINE, link_style="Index Link")
    doc = _Doc([prefix, title, suffix])
    snapshot = capture_outline_hyperlinks(_match(doc, 7, 16))
    _replace_equal(title, "New title")
    title.url = ""
    title.name = ""
    title.target = ""
    title.link_style = ""
    restore_outline_hyperlinks(doc, _match(doc, 7, 7), snapshot, "New title", None)
    assert prefix.url == "#1.New title|outline"
    assert title.url == "#1.New title|outline"
    assert title.name == "New title"
    assert title.target == "_blank"
    assert title.link_style == "Index Link"
    assert suffix.url == "#1.New title|outline"


def test_restore_leaves_the_url_when_the_matched_text_is_not_in_it():
    title = _Slice("Old title", "#1.Other|outline", "Old title", "")
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    _replace_equal(title, "New title")
    title.url = ""
    reports = restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "New title", None)
    assert title.url == "#1.Other|outline"
    assert title.name == "Old title"
    assert reports[0]["hyperlink_updated"] is False


def test_restore_puts_a_table_url_back_when_the_replace_clears_it():
    """A table target is a stable name. A title change must not turn it into an outline URL."""
    table_url = "#Taula69|table"
    title = _Slice("Annex", table_url)
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 5))
    assert snapshot.links == []
    assert snapshot.preserve_url == table_url
    _replace_equal(title, "Cover")
    title.url = ""
    reports = restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "Cover", None)
    assert title.url == table_url
    assert "|outline" not in title.url
    assert reports == []


def test_restore_puts_a_bookmark_url_back_when_the_replace_clears_it():
    """setString drops HyperLinkURL on the characters it rewrites. A bookmark target stays."""
    title = _Slice("Old title", _BOOKMARK)
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    assert snapshot.links == []
    assert snapshot.preserve_url == _BOOKMARK
    _replace_equal(title, "New title")
    title.url = ""
    reports = restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "New title", None)
    assert title.url == _BOOKMARK
    assert reports == []


def test_restore_override_sets_the_exact_url():
    title = _Slice("Old title", _OUTLINE, "Old title", "")
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    _replace_equal(title, "New title")
    reports = restore_outline_hyperlinks(
        doc, _match(doc, 0, 0), snapshot, "New title", "#9.Custom|outline")
    assert title.url == "#9.Custom|outline"
    assert reports[0]["hyperlink_url_after"] == "#9.Custom|outline"


def test_restore_does_not_paint_an_override_onto_a_bookmark():
    title = _Slice("Old title", _BOOKMARK)
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    _replace_equal(title, "New title")
    title.url = ""
    reports = restore_outline_hyperlinks(
        doc, _match(doc, 0, 0), snapshot, "New title", "#9.Custom|outline")
    assert title.url == _BOOKMARK
    assert reports == []


def test_restore_accepts_deleting_the_whole_outline_link():
    """No portion remains to paint. The delete must not raise."""
    title = _Slice("Old title", _OUTLINE)
    doc = _Doc([title])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    title.text = ""
    title.url = ""
    reports = restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "", None)
    assert title.url == ""
    assert reports[0]["hyperlink_url_after"] == ""
    assert reports[0]["hyperlink_updated"] is False


def test_restore_does_not_paint_one_outline_url_across_a_second_link():
    """The spanning phrase is in the first URL only. The second link must keep its target."""
    left = _Slice("Alpha ", "#1.Alpha Beta|outline")
    right = _Slice("Beta", "#2.Beta|outline")
    doc = _Doc([left, right])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 10))
    assert [link.url for link in snapshot.links] == [
        "#1.Alpha Beta|outline", "#2.Beta|outline"]
    _replace_equal(left, "Gamma ")
    restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "Gamma Beta", None)
    assert left.url == "#1.Gamma Beta|outline"
    assert right.url == "#2.Beta|outline"


def test_restore_does_not_put_the_first_url_on_a_neighbor_it_does_not_contain():
    """Neither URL contains the spanning phrase, so neither is written onto the other."""
    left = _Slice("Alpha", "#1.Alpha|outline")
    right = _Slice("Beta", "#2.Beta|outline")
    doc = _Doc([left, right])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "AlphaBeta", None)
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
    restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "Gamma Beta", None)
    assert left.url == "#1.Other|outline"
    assert right.url == _BOOKMARK


def test_restore_puts_back_black_and_no_underline_after_url_write():
    """HyperLinkURL assignment applies Internet-link chrome; colour / underline return."""
    prefix = _Slice("2.3.4. ", _OUTLINE, color=0, underline=0)
    title = _Slice("Old title", _OUTLINE, color=0, underline=0)
    suffix = _Slice(".\t42", _OUTLINE, color=0, underline=0)
    doc = _Doc([prefix, title, suffix], stomp_look_on_url=True)
    snapshot = capture_outline_hyperlinks(_match(doc, 7, 16))
    _replace_equal(title, "New title")
    restore_outline_hyperlinks(doc, _match(doc, 7, 7), snapshot, "New title", None)
    assert prefix.url == "#1.New title|outline"
    assert title.url == "#1.New title|outline"
    assert suffix.url == "#1.New title|outline"
    assert (prefix.color, prefix.underline) == (0, 0)
    assert (title.color, title.underline) == (0, 0)
    assert (suffix.color, suffix.underline) == (0, 0)


@pytest.mark.timeout(5)
def test_capture_outline_hyperlinks_magicmock_range_does_not_spin():
    """MagicMock.hasMoreElements() is truthy; the walker must stop (PR #823 merge CI)."""
    snap = capture_outline_hyperlinks(MagicMock())
    assert snap.links == []


def test_restore_selects_after_a_non_bmp_prefix():
    prefix = _Slice("😀 ", _OUTLINE)
    title = _Slice("Old title", _OUTLINE)
    suffix = _Slice(" tail", _OUTLINE)
    doc = _Doc([prefix, title, suffix])
    snapshot = capture_outline_hyperlinks(_match(doc, 2, 11))
    _replace_equal(title, "New title")
    reports = restore_outline_hyperlinks(doc, _match(doc, 2, 2), snapshot, "New title", None)
    assert title.url == "#1.New title|outline"
    assert suffix.url == "#1.New title|outline"
    assert reports[0]["hyperlink_updated"] is True
    assert reports[0]["hyperlink_url_after"] == "#1.New title|outline"


def test_capture_sets_fully_linked_false_when_plain_text_in_match():
    plain = _Slice("START ", "")
    linked = _Slice("link text", "https://example.com/", name="nm")
    doc = _Doc([plain, linked])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, len("START link text")))
    assert snapshot.fully_linked is False
    assert snapshot.props["HyperLinkName"] == "nm"

    linked_only = capture_outline_hyperlinks(_match(doc, len("START "), len("START link text")))
    assert linked_only.fully_linked is True


def test_paint_refuses_a_selection_that_does_not_match_the_replacement():
    title = _Slice("Old title!", _OUTLINE)
    doc = _Doc([title])
    snapshot = OutlineSnapshot([OutlineLink(_OUTLINE, "", "")], "X", "", "Old title", True)
    with pytest.raises(ToolExecutionError, match="Could not select the replaced text") as exc_info:
        _paint(doc, _Pos(0), snapshot, "New title", "#1.New title|outline", {}, required=True)
    assert exc_info.value.code == "HYPERLINK_UPDATE_FAILED"
    assert title.url == _OUTLINE
    assert _paint(doc, _Pos(0), snapshot, "New title", "#1.New title|outline", {}, required=False) is False
    assert title.url == _OUTLINE


def test_restore_without_anchor_raises_hyperlink_update_failed():
    snapshot = OutlineSnapshot([OutlineLink(_OUTLINE, "", "")], "X", "", "Old title", True)
    with pytest.raises(ToolExecutionError, match="Could not locate the replaced text") as exc_info:
        restore_outline_hyperlinks(_Doc([]), None, snapshot, "x", None)
    assert exc_info.value.code == "HYPERLINK_UPDATE_FAILED"


def test_paragraph_portions_skips_redline_markers():
    doc = _Doc([_Slice("Old title", _OUTLINE), _Slice("", text_type="Redline")])
    assert "Redline" not in [p.getPropertyValue("TextPortionType") for p in _paragraph_portions(doc, _Pos(0))]


def test_restore_ignores_a_marker_without_link_properties():
    title = _Slice("Old title", _OUTLINE)
    marker = _Slice("", text_type="Bookmark", raise_props=True)
    doc = _Doc([title, marker])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 9))
    _replace_equal(title, "New title")
    reports = restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "New title", None)
    assert title.url == "#1.New title|outline"
    assert reports[0]["hyperlink_updated"] is True
    assert reports[0]["hyperlink_url_after"] == "#1.New title|outline"


def test_restore_keeps_inherited_look_without_direct_color():
    """Inherited styles on sibling links are preserved via link style names without adding direct CharColor."""
    prefix = _Slice("2.3.4. ", _OUTLINE, color=0, underline=0, direct=False, link_style="Index Link")
    title = _Slice("Old title", _OUTLINE, color=0, underline=0, direct=False, link_style="Index Link")
    suffix = _Slice("\t42", _OUTLINE, color=0, underline=0, direct=False, link_style="Index Link")
    doc = _Doc([prefix, title, suffix], stomp_look_on_url=True)
    snapshot = capture_outline_hyperlinks(_match(doc, 7, 16))
    _replace_equal(title, "New title")
    restore_outline_hyperlinks(doc, _match(doc, 7, 7), snapshot, "New title", None)
    for sl in (prefix, title, suffix):
        assert sl.url == "#1.New title|outline"
        assert sl.link_style == "Index Link"
    assert ("UnvisitedCharStyleName", "Index Link") in doc.writes
    assert not any(name == "CharColor" for name, _ in doc.writes)


@pytest.mark.parametrize(
    ("before", "after", "expected_renames", "expected_lost"),
    [
        # Two renames around an equal title
        ([(1, "A"), (1, "Keep"), (2, "B")], [(1, "A2"), (1, "Keep"), (2, "B2")], {"A": "A2", "B": "B2"}, []),
        # A rename plus an added heading gives the rename only
        ([(1, "A"), (2, "B")], [(1, "A2"), (2, "B"), (2, "C")], {"A": "A2"}, []),
        # A repeated old title gives none and is reported lost
        ([(1, "Dup"), (2, "Dup")], [(1, "X"), (2, "Y")], {}, ["Dup"]),
        # A level change gives none and is reported lost
        ([(1, "A")], [(2, "A2")], {}, ["A"]),
        # A swap gives none
        ([(1, "A"), (1, "B")], [(1, "B"), (1, "A")], {}, []),
        # Two headings renamed to the same title gives none
        ([(1, "A"), (1, "B")], [(1, "Same"), (1, "Same")], {}, ["A", "B"]),
        # Swap plus rename pairs by title similarity
        (
            [(1, "Project scope"), (1, "Delivery costs")],
            [(1, "Total delivery costs"), (1, "Agreed project scope")],
            {"Project scope": "Agreed project scope", "Delivery costs": "Total delivery costs"},
            [],
        ),
        # Numbered chapter prefixes: follow Intro and Methods rather than chapter numbers
        (
            [(1, "Chapter 1: Intro"), (1, "Chapter 2: Methods")],
            [(1, "Chapter 1: Methods"), (1, "Chapter 2: Intro")],
            {"Chapter 1: Intro": "Chapter 2: Intro", "Chapter 2: Methods": "Chapter 1: Methods"},
            [],
        ),
        # Renamed heading moved past kept headings
        (
            [(1, "Project scope"), (1, "Costs"), (1, "Risks")],
            [(1, "Costs"), (1, "Risks"), (1, "Agreed project scope")],
            {"Project scope": "Agreed project scope"},
            [],
        ),
        # Rename next to an added heading
        (
            [(1, "Project scope"), (1, "Costs")],
            [(1, "Background"), (1, "Agreed project scope"), (1, "Costs")],
            {"Project scope": "Agreed project scope"},
            [],
        ),
        # Rename next to a deleted heading
        (
            [(1, "Old annex"), (1, "Project scope"), (1, "Costs")],
            [(1, "Agreed project scope"), (1, "Costs")],
            {"Project scope": "Agreed project scope"},
            ["Old annex"],
        ),
        # In-place translation pairs by position when nothing moved
        (
            [(1, "Scope"), (2, "Costs"), (1, "Risks")],
            [(1, "Alcance"), (2, "Presupuesto"), (1, "Peligros")],
            {"Scope": "Alcance", "Costs": "Presupuesto", "Risks": "Peligros"},
            [],
        ),
        # Swap plus rename alongside an unrelated deleted/added pair
        (
            [(1, "Project scope"), (1, "Delivery costs"), (1, "Old annex")],
            [(1, "Total delivery costs"), (1, "Agreed project scope"), (1, "Glossary")],
            {"Project scope": "Agreed project scope", "Delivery costs": "Total delivery costs"},
            ["Old annex"],
        ),
        # Kept titles compete in similarity so Q1 results does not pair with Q4 results
        (
            [(1, "Q1 results"), (1, "Q2 results"), (1, "Q3 results")],
            [(1, "Q2 results"), (1, "Q3 results"), (1, "Q4 results")],
            {},
            ["Q1 results"],
        ),
        # Ambiguous 1-to-2 split leaves A unpaired and lost
        ([(1, "A")], [(1, "A2"), (1, "A3")], {}, ["A"]),
        # Shared "Chapter N:" prefix must not dominate unrelated in-place retitles
        (
            [
                (1, "Chapter 1: Introduction"),
                (1, "Chapter 2: Literature Review"),
                (1, "Chapter 3: Methodology"),
                (1, "Chapter 4: Results"),
            ],
            [
                (1, "Chapter 1: Background"),
                (1, "Chapter 2: Related Work"),
                (1, "Chapter 3: Approach"),
                (1, "Chapter 4: Findings"),
            ],
            {
                "Chapter 1: Introduction": "Chapter 1: Background",
                "Chapter 2: Literature Review": "Chapter 2: Related Work",
                "Chapter 3: Methodology": "Chapter 3: Approach",
                "Chapter 4: Results": "Chapter 4: Findings",
            },
            [],
        ),
        # Shared "Article N." prefix must not dominate unrelated in-place retitles
        (
            [(1, "Article 1. Definitions"), (1, "Article 2. Term"), (1, "Article 3. Fees")],
            [(1, "Article 1. Interpretation"), (1, "Article 2. Duration"), (1, "Article 3. Charges")],
            {
                "Article 1. Definitions": "Article 1. Interpretation",
                "Article 2. Term": "Article 2. Duration",
                "Article 3. Fees": "Article 3. Charges",
            },
            [],
        ),
        # Renumbered "Step N:" headings still follow their body text when a step is inserted
        (
            [(1, "Step 1: Install"), (1, "Step 2: Configure")],
            [(1, "Step 1: Download"), (1, "Step 2: Install"), (1, "Step 3: Configure")],
            {"Step 1: Install": "Step 2: Install", "Step 2: Configure": "Step 3: Configure"},
            [],
        ),
        # Number-only changes after a move are left unpaired unless the number was passed on
        (
            [(1, "Appendix A"), (1, "Costs")],
            [(1, "Costs"), (1, "Appendix B")],
            {},
            ["Appendix A"],
        ),
        (
            [(1, "Sales 2023: Europe"), (1, "Costs")],
            [(1, "Costs"), (1, "Sales 2024: Europe")],
            {},
            ["Sales 2023: Europe"],
        ),
        (
            [(1, "Sales 2023: Europe"), (1, "Sales 2023: Asia"), (1, "Costs")],
            [(1, "Costs"), (1, "Sales 2023: Asia"), (1, "Sales 2024: Europe")],
            {},
            ["Sales 2023: Europe"],
        ),
        (
            [(1, "Appendix A"), (1, "Costs")],
            [(1, "Final costs"), (1, "Appendix B")],
            {"Costs": "Final costs"},
            ["Appendix A"],
        ),
        (
            [(1, "Appendix A"), (1, "Costs")],
            [(1, "Appendix B"), (1, "Costs")],
            {"Appendix A": "Appendix B"},
            [],
        ),
        (
            [(1, "1. Scope"), (1, "2. Costs")],
            [(1, "1. Costs"), (1, "2. Scope")],
            {"1. Scope": "2. Scope", "2. Costs": "1. Costs"},
            [],
        ),
        (
            [(1, "Step 1: Install"), (1, "Step 2: Configure")],
            [(1, "Step 1: Configure"), (1, "Step 2: Download"), (1, "Step 3: Install")],
            {"Step 1: Install": "Step 3: Install", "Step 2: Configure": "Step 1: Configure"},
            [],
        ),
        (
            [(1, "1 Background"), (2, "1.1 History"), (2, "1.2 Prior work"), (1, "2 Costs")],
            [(1, "1 Costs"), (1, "2 Background"), (2, "2.1 History"), (2, "2.2 Prior work")],
            {
                "1 Background": "2 Background",
                "1.1 History": "2.1 History",
                "1.2 Prior work": "2.2 Prior work",
                "2 Costs": "1 Costs",
            },
            [],
        ),
        (
            [(1, "Release 2.1: Notes"), (1, "Costs")],
            [(1, "Costs"), (1, "Release 3.1: Notes")],
            {},
            ["Release 2.1: Notes"],
        ),
        (
            [(1, "1 First quarter"), (2, "1.1 Summary"), (1, "2 Second quarter")],
            [(1, "1 First quarter"), (1, "2 Second quarter"), (2, "2.1 Summary")],
            {},
            ["1.1 Summary"],
        ),
        # Number-only pairs need position to confirm them
        (
            [(1, "Appendix A"), (1, "Scope")],
            [(1, "Alcance"), (1, "Appendix B")],
            {},
            ["Appendix A", "Scope"],
        ),
        (
            [(1, "Appendix A"), (1, "Costs")],
            [(1, "Glossary"), (1, "Appendix B"), (1, "Costs")],
            {},
            ["Appendix A"],
        ),
        (
            [(1, "Appendix A"), (1, "Scope")],
            [(1, "Appendix B"), (1, "Alcance")],
            {"Appendix A": "Appendix B", "Scope": "Alcance"},
            [],
        ),
        (
            [(1, "Appendix A"), (1, "Costs")],
            [(1, "Appendix B"), (1, "Costs"), (1, "Glossary")],
            {"Appendix A": "Appendix B"},
            [],
        ),
        (
            [(1, "Section 1"), (2, "Section 1.1 History"), (1, "Costs")],
            [(1, "Costs"), (1, "Section 2"), (2, "Section 2.1 History")],
            {"Section 1.1 History": "Section 2.1 History"},
            ["Section 1"],
        ),
        # CJK numerals and counters
        (
            [(1, "第一章"), (1, "Costs")],
            [(1, "Costs"), (1, "第二章")],
            {},
            ["第一章"],
        ),
        (
            [(1, "第一章"), (1, "费用")],
            [(1, "第二章"), (1, "费用")],
            {"第一章": "第二章"},
            [],
        ),
        (
            [(1, "第1章"), (1, "费用")],
            [(1, "价格"), (1, "第2章")],
            {},
            ["第1章", "费用"],
        ),
        (
            [(1, "第一条 定义"), (1, "费用")],
            [(1, "费用"), (1, "第二条 定义")],
            {},
            ["第一条 定义"],
        ),
        (
            [(1, "第一章 绪论"), (1, "费用")],
            [(1, "费用"), (1, "第一章 背景")],
            {},
            ["第一章 绪论"],
        ),
        (
            [(1, "十一、总则"), (1, "十二、附则")],
            [(1, "十一、附则"), (1, "十二、总则")],
            {"十一、总则": "十二、总则", "十二、附则": "十一、附则"},
            [],
        ),
        (
            [(1, "（一）总则"), (1, "（二）附则")],
            [(1, "（一）附则"), (1, "（二）总则")],
            {"（一）总则": "（二）总则", "（二）附则": "（一）附则"},
            [],
        ),
        # English number words
        (
            [(1, "Chapter One"), (1, "Scope")],
            [(1, "Alcance"), (1, "Chapter Two")],
            {},
            ["Chapter One", "Scope"],
        ),
        (
            [(1, "Chapter Twenty-One"), (1, "Costs")],
            [(1, "Costs"), (1, "Chapter Twenty-Two")],
            {},
            ["Chapter Twenty-One"],
        ),
        (
            [(1, "Chapter One"), (1, "Costs")],
            [(1, "Chapter Two"), (1, "Costs")],
            {"Chapter One": "Chapter Two"},
            [],
        ),
        (
            [(1, "Chapter Seventeen: Costs"), (1, "Risks")],
            [(1, "Risks"), (1, "Chapter Seventeen: Scope")],
            {},
            ["Chapter Seventeen: Costs"],
        ),
        (
            [(1, "Step One: Install"), (1, "Step Two: Configure")],
            [(1, "Step One: Configure"), (1, "Step Two: Download"), (1, "Step Three: Install")],
            {"Step One: Install": "Step Three: Install", "Step Two: Configure": "Step One: Configure"},
            [],
        ),
        (
            [(1, "第一部分"), (1, "费用")],
            [(1, "费用"), (1, "分析")],
            {},
            ["第一部分"],
        ),
        (
            [(1, "第一部分"), (1, "费用")],
            [(1, "第二部分"), (1, "费用")],
            {"第一部分": "第二部分"},
            [],
        ),
        (
            [(1, "第一章节"), (1, "费用")],
            [(1, "费用"), (1, "节日")],
            {},
            ["第一章节"],
        ),
        (
            [(1, "第一章 — 绪论"), (1, "费用")],
            [(1, "费用"), (1, "第一章 — 背景")],
            {},
            ["第一章 — 绪论"],
        ),
        (
            [(1, "Appendix A"), (1, "Sales 2023: Europe")],
            [(1, "Glossary"), (1, "Appendix B"), (1, "Sales 2024: Europe")],
            {},
            ["Appendix A", "Sales 2023: Europe"],
        ),
    ],
)
def test_heading_renames(before, after, expected_renames, expected_lost):
    assert heading_renames(before, after) == (expected_renames, expected_lost)


@pytest.mark.parametrize(
    ("url", "successors", "expected"),
    [
        ("#Old title|outline", {"Old title": "New title"}, "#New title|outline"),
        ("#1.Old title|outline", {"Old title": "New title"}, "#1.New title|outline"),
        ("#1.2.Old title|outline", {"Old title": "New title"}, "#1.2.New title|outline"),
        ("#Old%20title|outline", {"Old title": "New title"}, "#New%20title|outline"),
        ("#1.Old%20title|outline", {"Old title": "New title"}, "#1.New%20title|outline"),
        ("#Old%20title%7Coutline", {"Old title": "New title"}, "#New%20title%7Coutline"),
        ("#2026. Plan|outline", {"2026. Plan": "2027. Plan"}, "#2027. Plan|outline"),
        ("#1.2026. Plan|outline", {"2026. Plan": "2027. Plan"}, "#1.2027. Plan|outline"),
        ("#50% off|outline", {"50% off": "40% off"}, "#40% off|outline"),
        ("#Other|outline", {"Old title": "New title"}, "#Other|outline"),
        ("#__RefHeading___Toc123", {"Old title": "New title"}, "#__RefHeading___Toc123"),
        ("#2.1 Scope|outline", {"1 Scope": "1 Aim", "2.1 Scope": "2.1 Scope"}, "#2.1 Scope|outline"),
        ("#1.Scope|outline", {"Scope": "1.Scope", "1.Scope": "1.Scope"}, "#1.Scope|outline"),
        ("#1.1.Plan|outline", {"Plan": None, "1.Plan": "1.Plan"}, "#1.1.Plan|outline"),
        ("#1.Plan|outline", {"Plan": None}, None),
        ("#caf%c3%a9|outline", {"café": "café"}, "#caf%c3%a9|outline"),
    ],
)
def test_renamed_url(url, successors, expected):
    assert _renamed_url(url, successors) == expected


def test_restore_partly_linked_does_not_paint_outline_url():
    lead = _Slice("See ")
    title = _Slice("Old title", _OUTLINE)
    tail = _Slice(" here")
    doc = _Doc([lead, title, tail])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, len(doc.text())))
    assert snapshot.fully_linked is False
    # Simulate an HTML replace that dropped the link on the replaced text.
    doc.slices = [_Slice("Read New text now")]
    reports = restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "Read New text now", None)
    assert doc.slices[0].url == ""
    assert reports[0]["hyperlink_updated"] is False


def test_restore_two_non_outline_urls_does_not_paint():
    first = _Slice("Alpha ", "https://a.example.com/")
    second = _Slice("Beta", "https://b.example.com/")
    doc = _Doc([first, second])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, len(doc.text())))
    assert snapshot.multi_preserve is True
    doc.slices = [_Slice("Gamma delta epsilon")]
    restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "Gamma delta epsilon", None)
    assert doc.slices[0].url == ""


def test_restore_mid_paragraph_outline_link_leaves_neighbors_unlinked():
    zed = "#Zed|outline"
    lead = _Slice("See ")
    title = _Slice("Old title", zed, color=0x112233, direct=True)
    tail = _Slice(" here")
    doc = _Doc([lead, title, tail])
    snapshot = capture_outline_hyperlinks(_match(doc, 4, 13))
    assert snapshot.whole_paragraph is False
    doc.slices = [lead, _Slice("Swap"), tail]
    restore_outline_hyperlinks(doc, _match(doc, 4, 4), snapshot, "Swap", None)
    assert lead.url == ""
    assert tail.url == ""
    assert doc.slices[1].url == zed


def test_restore_whole_paragraph_toc_row_writes_whole_paragraph():
    num = _Slice("1. ", _OUTLINE, color=0x112233, direct=True)
    title = _Slice("Old title", _OUTLINE, color=0x112233, direct=True)
    page = _Slice("\t5", _OUTLINE, color=0x112233, direct=True)
    doc = _Doc([num, title, page], stomp_look_on_url=True)
    snapshot = capture_outline_hyperlinks(_match(doc, 13, 14))
    assert snapshot.whole_paragraph is True
    page.text = "\t6"
    restore_outline_hyperlinks(doc, _match(doc, 13, 13), snapshot, "6", None)
    assert all(sl.url == _OUTLINE and sl.color == 0x112233 for sl in (num, title, page))


def test_restore_reports_a_dropped_link():
    left = _Slice("Alpha ", "#1.Alpha Beta|outline")
    right = _Slice("Beta", "#1.Other|outline")
    doc = _Doc([left, right])
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 10))
    doc.slices = [_Slice("New title")]
    reports = restore_outline_hyperlinks(doc, _match(doc, 0, 0), snapshot, "New title", None)
    assert doc.slices[0].url == ""
    assert [(r["hyperlink_url_after"], r["hyperlink_updated"]) for r in reports] == [
        ("", False),
        ("", False),
    ]

    left2 = _Slice("Alpha ", "#1.Alpha Beta|outline")
    right2 = _Slice("Beta", "#1.Other|outline")
    tail2 = _Slice(" rest", "#1.Alpha Beta|outline")
    doc2 = _Doc([left2, right2, tail2])
    snapshot2 = capture_outline_hyperlinks(_match(doc2, 0, 10))
    doc2.slices = [_Slice("New title"), tail2]
    reports2 = restore_outline_hyperlinks(doc2, _match(doc2, 0, 0), snapshot2, "New title", None)
    assert [(r["hyperlink_url_after"], r["hyperlink_updated"]) for r in reports2] == [
        ("#1.New title|outline", True),
        ("", False),
    ]


def test_restore_whole_row_with_different_names_keeps_them():
    url = "#1.Other|outline"
    s1 = _Slice("1. ", url, name="n1")
    s2 = _Slice("Old title", url, name="n2")
    s3 = _Slice("\t5", url, name="n3")
    doc = _Doc([s1, s2, s3])
    snapshot = capture_outline_hyperlinks(_match(doc, 12, 14))
    assert snapshot.whole_paragraph is False
    s3.text = "\t6"
    s3.url = ""
    s3.name = ""
    restore_outline_hyperlinks(doc, _match(doc, 12, 12), snapshot, "\t6", None)
    assert s1.name == "n1"
    assert s2.name == "n2"
    assert s3.name == "n3"
    assert s3.url == url


def test_hyperlink_fields_omits_empty_and_wraps_non_empty():
    from plugin.writer.hyperlink_fixup import hyperlink_fields

    assert hyperlink_fields([]) == {}
    reports = [{"hyperlink_url": "#1.A|outline", "hyperlink_url_after": "#1.B|outline", "hyperlink_updated": True}]
    assert hyperlink_fields(reports) == {"hyperlinks": reports}


def test_keep_links_while_recording_reports_empty_when_only_struck_text_linked():
    from plugin.writer.hyperlink_fixup import restore_links

    url = "#1.Title|outline"
    doc = _Doc([_Slice("Title", url)])
    doc._recording = True
    doc.getPropertyValue = lambda k: True if k == "RecordChanges" else None
    snapshot = capture_outline_hyperlinks(_match(doc, 0, 5))

    # Simulate tracked Delete of old "Title" and unlinked Insert of "Other"
    del_start = _Slice("", text_type="Redline")
    del_start.RedlineType = "Delete"
    del_start.IsStart = True
    del_end = _Slice("", text_type="Redline")
    del_end.RedlineType = "Delete"
    del_end.IsStart = False
    doc.slices = [del_start, _Slice("Title", url), del_end, _Slice("Other", "")]

    reports = restore_links(doc, _match(doc, 0, 0), snapshot, "Other", None)
    # Note: _put_back_url gave url, so _keep_links_while_recording painted "Other" with url.
    assert reports[0]["hyperlink_url_after"] == url
    assert "hyperlink_note" in reports[0]

    # When no live run carries the link (e.g. multi-link match where _put_back_url is ""),
    # hyperlink_url_after is "" and hyperlink_note is omitted.
    url2 = "#2.Other|outline"
    doc2 = _Doc([_Slice("A ", url), _Slice("B", url2)])
    doc2._recording = True
    doc2.getPropertyValue = lambda k: True if k == "RecordChanges" else None
    snap2 = capture_outline_hyperlinks(_match(doc2, 0, 3))
    doc2.slices = [del_start, _Slice("A ", url), _Slice("B", url2), del_end, _Slice("New", "")]
    reports2 = restore_links(doc2, _match(doc2, 0, 0), snap2, "New", None)
    assert all(r["hyperlink_url_after"] == "" for r in reports2)
    assert all("hyperlink_note" not in r for r in reports2)


def test_final_reports_seeds_deletion_state_from_earlier_paragraph(monkeypatch):
    from plugin.doc.text_helpers import DeletionState
    from plugin.writer import hyperlink_fixup

    url = "#1.T|outline"
    del_end = _Slice("", text_type="Redline")
    del_end.RedlineType = "Delete"
    del_end.IsStart = False

    class _FakeTracked:
        def __init__(self, _doc):
            pass

        def state_at(self, _cursor):
            return DeletionState(in_delete=True)

    monkeypatch.setattr(hyperlink_fixup, "TrackedDeletions", _FakeTracked)

    doc = _Doc([_Slice("Old", url), del_end, _Slice(" plain ", "")])
    plans = [{"url": url, "new_url": None}]
    reports = hyperlink_fixup._final_reports(doc, doc, _match(doc, 0, 0), plans, set(), True)
    assert reports == [{"hyperlink_url": url, "hyperlink_url_after": "", "hyperlink_updated": False}]

    doc_after = _Doc([_Slice("Old", ""), del_end, _Slice("New", url)])
    reports_after = hyperlink_fixup._final_reports(doc_after, doc_after, _match(doc_after, 0, 0), plans, set(), True)
    assert reports_after == [{"hyperlink_url": url, "hyperlink_url_after": url, "hyperlink_updated": False}]
