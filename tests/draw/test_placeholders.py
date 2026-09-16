# WriterAgent — unit tests for Impress placeholder role matching
# SPDX-License-Identifier: GPL-3.0-or-later

from plugin.draw.placeholders import _find_placeholder, _list_placeholders, _role_from_label


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


def test_positional_fallback_when_no_class_tags():
    page = _FakePage(
        [
            _FakeShape(text="first"),
            _FakeShape(text="second"),
        ]
    )
    _unused_shape, title_idx = _find_placeholder(page, "title")
    _unused_body, body_idx = _find_placeholder(page, "body")
    assert title_idx == 0
    assert body_idx == 1
    listed = _list_placeholders(page)
    assert "role" not in listed[0]
    assert "role" not in listed[1]
