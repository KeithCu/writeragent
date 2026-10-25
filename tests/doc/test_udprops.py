# WriterAgent - tests for document user-defined properties

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugin.doc.udprops import (
    get_document_property,
    remove_document_property,
    set_document_property,
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


def test_set_document_property_trace_names_each_uno_step(capsys, monkeypatch):
    """GHA 33763078357: hang dump must name getDocumentProperties vs setPropertyValue."""
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("log.debug/print stripped in release bundle")
    from plugin.doc import udprops as udprops_mod

    class _Info:
        def hasPropertyByName(self, name):
            return name == "WriterAgentSessionID"

    class _Props:
        def getPropertySetInfo(self):
            return _Info()

        def setPropertyValue(self, name, value):
            self.written = (name, value)

    class _DocProps:
        def __init__(self):
            self.UserDefinedProperties = _Props()

    class _Model:
        def getDocumentProperties(self):
            return _DocProps()

    monkeypatch.setattr(udprops_mod, "_TRACE_UDPROPS", True)
    set_document_property(_Model(), "WriterAgentSessionID", "")
    err = capsys.readouterr().err
    assert "set_document_property: getDocumentProperties start name=WriterAgentSessionID" in err
    assert "set_document_property: getDocumentProperties done" in err
    assert "set_document_property: setPropertyValue start name=WriterAgentSessionID" in err
    assert "set_document_property: setPropertyValue done" in err

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

    class _Doc:
        def getDocumentProperties(self):
            class _DocProps:
                def __init__(self, p):
                    self.UserDefinedProperties = p
            return _DocProps(_PropsWithoutSetter())

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
    monkeypatch.setattr("plugin.framework.thread_guard._unwrap_uno", lambda obj: raw)
    with pytest.raises(UnoObjectError):
        set_document_property(_Guard(), "P", "v")
    raw.getURL.assert_called_once()

