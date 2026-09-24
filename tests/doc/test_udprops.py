# WriterAgent - tests for document user-defined properties

from __future__ import annotations

from plugin.doc.udprops import get_document_property, set_document_property


def test_get_document_property_missing_api_returns_default():
    class _Model:
        pass

    assert get_document_property(_Model(), "WriterAgentSessionID", default="x") == "x"


def test_set_document_property_trace_names_each_uno_step(capsys, monkeypatch):
    """GHA 33763078357: hang dump must name getDocumentProperties vs setPropertyValue."""
    from tests.strip_bundle import skip_if_release_build

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
