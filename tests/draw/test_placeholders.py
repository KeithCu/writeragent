# WriterAgent — unit tests for Impress placeholder role matching
# SPDX-License-Identifier: GPL-3.0-or-later

from types import SimpleNamespace
from unittest.mock import patch

from plugin.draw.placeholders import (
    SetPlaceholderText,
    _EMPTY_PLACEHOLDER_HINT,
    _find_placeholder,
    _list_placeholders,
    _role_from_label,
    _role_miss_error_kwargs,
)


class _FakeShape:
    def __init__(self, class_name=None, name="", text=""):
        if class_name is not None:
            self.ClassName = class_name
        self.Name = name
        self._text = text

    def getString(self):
        return self._text


class _FakePage:
    def __init__(self, shapes):
        self._shapes = shapes

    def getCount(self):
        return len(self._shapes)

    def getByIndex(self, index):
        return self._shapes[index]


def test_role_from_label_priority_title_not_body():
    assert _role_from_label("TitleTextShape") == "title"
    assert _role_from_label("com.sun.star.presentation.TitleTextShape") == "title"
    assert _role_from_label("OutlinerShape") == "body"
    assert _role_from_label("SubTitleShape") == "subtitle"
    assert _role_from_label("TextShape") is None


def test_body_does_not_match_title_text_shape():
    """A2: 'text' in 'titletextshape' used to return the title as body."""
    page = _FakePage(
        [
            _FakeShape(class_name="TitleTextShape", text="Title"),
            _FakeShape(class_name="OutlinerShape", text="Body"),
        ]
    )
    _unused_shape, title_idx = _find_placeholder(page, "title")
    _unused_body, body_idx = _find_placeholder(page, "body")
    assert title_idx == 0
    assert body_idx == 1


def test_role_match_independent_of_shape_order():
    page = _FakePage(
        [
            _FakeShape(class_name="OutlinerShape", text="Body"),
            _FakeShape(class_name="TitleTextShape", text="Title"),
        ]
    )
    _unused_shape, title_idx = _find_placeholder(page, "title")
    _unused_body, body_idx = _find_placeholder(page, "body")
    assert title_idx == 1
    assert body_idx == 0


def test_subtitle_not_classified_as_title():
    """'title' is a substring of 'subtitle'; SubTitle must win."""
    page = _FakePage(
        [
            _FakeShape(class_name="SubTitleShape", text="Sub"),
            _FakeShape(class_name="TitleTextShape", text="Title"),
        ]
    )
    _unused_sub, sub_idx = _find_placeholder(page, "subtitle")
    _unused_title, title_idx = _find_placeholder(page, "title")
    assert sub_idx == 0
    assert title_idx == 1
    assert _list_placeholders(page)[0]["role"] == "subtitle"


def test_body_on_title_only_slide_is_not_the_title():
    page = _FakePage([_FakeShape(class_name="TitleTextShape", text="Only title")])
    assert _find_placeholder(page, "body") == (None, None)


def test_list_placeholders_roles_from_class_map():
    page = _FakePage(
        [
            _FakeShape(class_name="TitleTextShape", text="T"),
            _FakeShape(class_name="OutlinerShape", text="B"),
        ]
    )
    listed = _list_placeholders(page)
    assert [entry.get("role") for entry in listed] == ["title", "body"]


def test_role_from_shape_type_when_class_name_is_missing():
    """Impress layout placeholders expose ShapeType, not ClassName."""

    class _Typed:
        def __init__(self, shape_type: str, text: str) -> None:
            self._type = shape_type
            self._text = text

        def getShapeType(self) -> str:
            return self._type

        def getString(self) -> str:
            return self._text

    page = _FakePage(
        [
            _Typed("com.sun.star.presentation.TitleTextShape", "T"),
            _Typed("com.sun.star.presentation.OutlinerShape", "B"),
        ]
    )
    assert _find_placeholder(page, "title")[1] == 0
    assert _find_placeholder(page, "body")[1] == 1
    listed = _list_placeholders(page)
    assert [entry.get("role") for entry in listed] == ["title", "body"]
    assert "TitleTextShape" in listed[0]["class"]


def test_untagged_text_shapes_are_not_positional_roles():
    page = _FakePage(
        [
            _FakeShape(text="first"),
            _FakeShape(text="second"),
        ]
    )
    assert _find_placeholder(page, "title") == (None, None)
    assert _find_placeholder(page, "body") == (None, None)
    listed = _list_placeholders(page)
    assert "role" not in listed[0]
    assert "role" not in listed[1]


def test_role_miss_empty_available_is_actionable():
    """C1: available=[] includes hint + suggest_layout so the model can recover."""
    extra = _role_miss_error_kwargs(_FakePage([]))
    assert extra["available"] == []
    assert extra["suggest_layout"] == "text"
    assert extra["shape_text_count"] == 0
    assert extra["hint"] == _EMPTY_PLACEHOLDER_HINT
    assert "fallback_indices" not in extra


def test_role_miss_nonempty_available_stays_simple():
    page = _FakePage([_FakeShape(class_name="TitleTextShape", text="T")])
    extra = _role_miss_error_kwargs(page)
    assert extra["available"]
    assert "suggest_layout" not in extra
    assert "hint" not in extra


def test_role_miss_fallback_indices_for_class_only_shapes():
    """C1 read-only hint when shapes exist but lack getString (not in available)."""

    class _ClassOnly:
        def __init__(self):
            self.ClassName = "TitleTextShape"
            self.Name = "Title"

    extra = _role_miss_error_kwargs(_FakePage([_ClassOnly()]))
    assert extra["available"] == []
    assert extra["suggest_layout"] == "text"
    assert extra["shape_text_count"] == 0
    assert extra["fallback_indices"] == [{"index": 0, "class": "TitleTextShape", "name": "Title"}]


def test_get_placeholder_text_by_role_returns_index():
    page = _FakePage([
        _FakeShape(class_name="TitleTextShape", text="Title"),
        _FakeShape(class_name="OutlinerShape", text="Body")
    ])

    with patch("plugin.draw.placeholders.DrawBridge.get_slide_for_tool", return_value=page):
        from plugin.draw.placeholders import GetPlaceholderText
        res = GetPlaceholderText().execute(SimpleNamespace(doc=object()), role="body")

    assert res["status"] == "ok"
    assert res["role"] == "body"
    assert res["index"] == 1

    # We should also be able to pass that index to SetPlaceholderText directly
    class _MockShapeWithSetString(_FakeShape):
        def setString(self, text):
            self._text = text

    page2 = _FakePage([
        _MockShapeWithSetString(class_name="TitleTextShape", text="Title"),
        _MockShapeWithSetString(class_name="OutlinerShape", text="Body")
    ])
    with patch("plugin.draw.placeholders.DrawBridge.get_slide_for_tool", return_value=page2):
        res2 = SetPlaceholderText().execute(SimpleNamespace(doc=object()), text="New Body", index=res["index"])

    assert res2["status"] == "ok"
    assert page2.getByIndex(1).getString() == "New Body"


def test_set_placeholder_text_empty_role_miss_payload():
    page = _FakePage([])
    with patch("plugin.draw.placeholders.DrawBridge.get_slide_for_tool", return_value=page):
        err = SetPlaceholderText().execute(SimpleNamespace(doc=object()), text="x", role="title")
    assert err["status"] == "error"
    details = err["details"]
    assert details["available"] == []
    assert details["suggest_layout"] == "text"
    assert details["shape_text_count"] == 0
    assert "slide_layouts" in details["hint"]
    assert "set_slide_layout" not in details["hint"]
    assert "Placeholder 'title' not found on this slide." == err["message"]


def test_placeholder_tool_descriptions_steer_list_then_layout():
    assert "list_placeholders" in SetPlaceholderText.description
    assert "index" in SetPlaceholderText.description
    assert "layout" in SetPlaceholderText.description
    from plugin.draw.placeholders import ListPlaceholders

    assert "before set_placeholder_text" in ListPlaceholders.description
    assert "slide_layouts" in ListPlaceholders.description
