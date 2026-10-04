"""Point bookmarks are positions, not broken anchors."""

from types import SimpleNamespace

from plugin.doc.diagnostics import DocumentHealthCheck


class _Anchor:
    def __init__(self, text, start=None, start_error=False):
        self._text = text
        self._start = start
        self._start_error = start_error

    def getString(self):
        return self._text

    def getStart(self):
        if self._start_error:
            raise RuntimeError("no start")
        return self._start


class _Mark:
    def __init__(self, anchor):
        self._anchor = anchor

    def getAnchor(self):
        return self._anchor


class _Bookmarks:
    def __init__(self, marks):
        self._marks = marks

    def getElementNames(self):
        return list(self._marks)

    def getByName(self, name):
        return self._marks[name]


def _issues(marks):
    doc = SimpleNamespace(getBookmarks=lambda: _Bookmarks(marks))
    ctx = SimpleNamespace(
        doc=doc,
        services=SimpleNamespace(document=SimpleNamespace(get_paragraph_ranges=lambda _doc: [])),
    )
    result = DocumentHealthCheck().execute(ctx)
    return [issue["message"] for issue in result["issues"] if issue["type"] == "broken_bookmark"]


def test_point_mcp_and_user_bookmarks_are_not_broken():
    broken = _issues(
        {
            "_mcp_heading": _Mark(_Anchor("", start=0)),
            "UserPoint": _Mark(_Anchor("", start="cursor")),
            "Span": _Mark(_Anchor("hello")),
        }
    )
    assert broken == []


def test_missing_or_unreadable_anchor_is_broken():
    broken = _issues(
        {
            "Gone": _Mark(None),
            "Unreadable": _Mark(_Anchor("", start_error=True)),
        }
    )
    assert any("Gone" in message for message in broken)
    assert any("Unreadable" in message for message in broken)
