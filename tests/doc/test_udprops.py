# WriterAgent - tests for document user-defined properties

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugin.doc.udprops import (
    get_document_property,
    list_user_defined_document_properties,
    read_builtin_document_properties,
    remove_document_property,
    set_document_property,
    write_builtin_document_properties,
)
from plugin.framework.errors import DocumentDisposedError, UnoObjectError
from tests.writer.test_document_helpers import (
    _DocWithUserDefinedProperties,
    _UserDefinedProperties,
)


def test_get_document_property_missing_api_returns_default():
    class _Model:
        pass

    assert get_document_property(_Model(), "WriterAgentSessionID", default="x") == "x"


def test_get_document_property_disposed():
    doc = MagicMock()
    doc.getDocumentProperties.side_effect = DocumentDisposedError("stale")

    with pytest.raises(DocumentDisposedError):
        get_document_property(doc, "SomeProp")


def test_set_document_property_disposed():
    doc = MagicMock()
    doc.getDocumentProperties.side_effect = DocumentDisposedError("stale")

    with pytest.raises(DocumentDisposedError):
        set_document_property(doc, "SomeProp", "Value")


def test_set_document_property_twice_uses_set_not_add():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    assert set_document_property(doc, "P", "one") is True
    assert set_document_property(doc, "P", "two") is True
    assert props.values["P"] == "two"
    assert len(props.add_calls) == 1
    assert props.set_calls == [("P", "two")]


def test_set_document_property_no_bag_returns_false():
    class _NoBag:
        pass

    assert set_document_property(_NoBag(), "P", "v") is False

    class _NoUserDefined:
        def getDocumentProperties(self):
            class _PropsWithoutUserDefined:
                pass
            return _PropsWithoutUserDefined()

    assert set_document_property(_NoUserDefined(), "P", "v") is False


def test_set_document_property_existing_without_setter_does_not_add():
    class _Info:
        def hasPropertyByName(self, name):
            return True

    class _PropsWithoutSetter:
        def __init__(self):
            self.add_calls = []

        def getPropertySetInfo(self):
            return _Info()

        def addProperty(self, name, attrs, value):
            self.add_calls.append((name, value))

    props = _PropsWithoutSetter()
    doc = _DocWithUserDefinedProperties(props)
    assert set_document_property(doc, "P", "v") is False
    assert len(props.add_calls) == 0


def test_set_document_property_add_collision_raises_uno_object_error():
    class _Info:
        def hasPropertyByName(self, name):
            return False

    class _PropsCollision:
        def getPropertySetInfo(self):
            return _Info()

        def addProperty(self, name, attrs, value):
            raise RuntimeError("Property name or handle already used")

    doc = _DocWithUserDefinedProperties(_PropsCollision())
    with pytest.raises(UnoObjectError):
        set_document_property(doc, "P", "v")


def test_remove_document_property_absent_and_present():
    class _NoBag:
        pass

    assert remove_document_property(_NoBag(), "P") is True

    class _RemovableProps(_UserDefinedProperties):
        def removeProperty(self, name):
            del self.values[name]

    props = _RemovableProps()
    doc = _DocWithUserDefinedProperties(props)
    assert set_document_property(doc, "P", "val") is True
    assert remove_document_property(doc, "P") is True
    assert "P" not in props.values
    assert remove_document_property(doc, "P") is True


def test_remove_document_property_failure_returns_false():
    class _FailProps(_UserDefinedProperties):
        def removeProperty(self, name):
            raise RuntimeError("nope")

    props = _FailProps()
    doc = _DocWithUserDefinedProperties(props)
    assert set_document_property(doc, "P", "val") is True
    assert remove_document_property(doc, "P") is False


def test_set_document_property_error_path_probes_raw_model(monkeypatch):
    class _Guard:
        def __getattr__(self, name):
            raise AssertionError("the guard proxy must not be touched in the error path")

    raw = MagicMock()
    raw.getDocumentProperties.side_effect = UnoObjectError("boom")
    raw.getURL.return_value = "file:///tmp/x.odt"
    raw.isReadonly.return_value = False
    monkeypatch.setattr("plugin.framework.thread_guard.unwrap_uno", lambda obj: raw)
    with pytest.raises(UnoObjectError):
        set_document_property(_Guard(), "P", "v")
    raw.getURL.assert_called_once()


def test_list_user_defined_document_properties_sorted():
    props = _UserDefinedProperties()
    props.values["Zebra"] = "z"
    props.values["Alpha"] = "a"
    doc = _DocWithUserDefinedProperties(props)
    assert list_user_defined_document_properties(doc) == ["Alpha", "Zebra"]


def test_read_and_write_builtin_document_properties():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)

    class _DocProps:
        def __init__(self):
            self.UserDefinedProperties = props
            self.Title = "old"
            self.Subject = ""
            self.Author = ""
            self.Description = ""
            self.Keywords = ()

    bag = _DocProps()
    doc.getDocumentProperties = lambda: bag
    read = read_builtin_document_properties(doc)
    assert read["title"] == "old"
    updated = write_builtin_document_properties(doc, title="new", keywords=["a", "b"])
    assert updated == ["title", "keywords"]
    assert bag.Title == "new"
    assert bag.Keywords == ("a", "b")

