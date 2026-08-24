from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.doc.udprops import get_document_property, set_document_property
from plugin.writer.edit_review import WriterCompoundUndo, WriterStreamedRewriteSession, build_writer_rewrite_prompt


class _MutableTextRange:
    def __init__(self):
        self.text = "initial"
        self.fail_once_on_generated = False

    def setString(self, value):
        if self.fail_once_on_generated and value == "Generated":
            self.fail_once_on_generated = False
            raise RuntimeError("tracked write failed")
        self.text = value

    def getString(self):
        return self.text


class _MockUndoManager:
    def __init__(self):
        self.entered = False
        self.left = False

    def enterUndoContext(self, title: str) -> None:
        self.entered = True
        assert "WriterAgent" in title

    def leaveUndoContext(self) -> None:
        self.left = True


class _MockDoc:
    def __init__(self, recording=True):
        self.props = {"RecordChanges": recording}
        self.undo = _MockUndoManager()

    def getPropertyValue(self, name):
        return self.props[name]

    def setPropertyValue(self, name, value):
        self.props[name] = value

    def getUndoManager(self):
        return self.undo


class _UserDefinedPropertySetInfo:
    def __init__(self, owner):
        self._owner = owner

    def hasPropertyByName(self, name):
        return name in self._owner.values


class _UserDefinedProperties:
    """Mirrors LibreOffice's ``UserDefinedProperties`` (``PropertyBag``).

    Real bag exposes ``getPropertySetInfo()`` + ``addProperty`` + ``setPropertyValue``
    + ``getPropertyValue``, but NOT ``hasByName`` (it is not an ``XNameAccess``).
    """

    def __init__(self):
        self.values = {}
        self.add_calls = []
        self.set_calls = []

    def getPropertySetInfo(self):
        return _UserDefinedPropertySetInfo(self)

    def addProperty(self, name, _attrs, value):
        if name in self.values:
            raise RuntimeError("Property name or handle already used")
        self.add_calls.append((name, value))
        self.values[name] = value

    def setPropertyValue(self, name, value):
        if name not in self.values:
            raise RuntimeError("Unknown property")
        self.set_calls.append((name, value))
        self.values[name] = value
        return None

    def getPropertyValue(self, name):
        if name not in self.values:
            raise RuntimeError("Unknown property")
        return self.values[name]


class _DocWithUserDefinedProperties:
    def __init__(self, props):
        self._props = props

    def getDocumentProperties(self):
        class _DocProps:
            def __init__(self, user_props):
                self.UserDefinedProperties = user_props

        return _DocProps(self._props)


def test_build_writer_rewrite_prompt_uses_direct_rewrite_format():
    prompt = build_writer_rewrite_prompt("Original text", "Make it shorter")

    assert "Rewrite the following text" in prompt
    assert "Instructions: Make it shorter" in prompt
    assert "Text to rewrite:\nOriginal text" in prompt


def test_writer_streamed_rewrite_session_finishes_as_single_tracked_change():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    assert doc.undo.entered is True
    assert doc.undo.left is False
    assert doc.getPropertyValue("RecordChanges") is False
    assert text_range.getString() == ""

    session.append_chunk("Generated")
    warning = session.finish()

    assert warning is None
    assert text_range.getString() == "Generated"
    assert doc.getPropertyValue("RecordChanges") is True
    assert doc.undo.left is True


def test_writer_streamed_rewrite_session_abort_restores_original_text():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    session.append_chunk("Partial")
    session.abort_and_restore()

    assert text_range.getString() == "Original"
    assert doc.getPropertyValue("RecordChanges") is True
    assert doc.undo.left is True


def test_writer_streamed_rewrite_session_fallback_keeps_generated_text():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    session.append_chunk("Generated")
    text_range.fail_once_on_generated = True
    warning = session.finish()

    assert warning is not None
    assert "generated text was kept" in warning
    assert text_range.getString() == "Generated"
    assert doc.getPropertyValue("RecordChanges") is True
    assert doc.undo.left is True


def test_writer_streamed_rewrite_session_finish_without_tracking_leaves_undo_context():
    doc = _MockDoc(recording=False)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    assert doc.undo.entered is True
    assert session.finish() is None
    assert doc.undo.left is True


def test_writer_compound_undo_enter_close_and_idempotent():
    doc = _MockDoc(recording=True)
    cu = WriterCompoundUndo(doc, "WriterAgent: test")
    assert doc.undo.entered is True
    assert doc.undo.left is False
    cu.close()
    assert doc.undo.left is True
    cu.close()
    assert doc.undo.left is True


def test_set_document_property_updates_existing_without_readding(monkeypatch):
    """Regression: ``UserDefinedProperties`` exposes existence via ``getPropertySetInfo``,
    not ``hasByName``. The old check fell through to ``addProperty`` even when the
    property already existed and second saves raised ``Property name or handle already used``.
    """
    props = _UserDefinedProperties()
    props.values["WriterAgentGrammarCache"] = "{}"
    doc = _DocWithUserDefinedProperties(props)

    monkeypatch.setattr("plugin.doc.udprops.uno.getConstantByName", lambda _name: 1)

    set_document_property(doc, "WriterAgentGrammarCache", '{"fp":[]}')

    assert props.values["WriterAgentGrammarCache"] == '{"fp":[]}'
    assert props.set_calls == [("WriterAgentGrammarCache", '{"fp":[]}')]
    assert props.add_calls == []


def test_set_document_property_creates_missing_property(monkeypatch):
    """First save on a doc that has never stored the cache must call addProperty()."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)

    monkeypatch.setattr("plugin.doc.udprops.uno.getConstantByName", lambda _name: 1)

    set_document_property(doc, "WriterAgentGrammarCache", '{"fp":[]}')

    assert props.values["WriterAgentGrammarCache"] == '{"fp":[]}'
    assert props.add_calls == [("WriterAgentGrammarCache", '{"fp":[]}')]
    assert props.set_calls == []


def test_get_document_property_returns_default_when_missing_without_warning():
    """First open: property doesn't exist yet. Old code warned via the
    ``Get property value fallback`` path; existence check via PropertySetInfo
    means we now return ``default`` quietly."""
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)

    assert get_document_property(doc, "WriterAgentGrammarCache", default=None) is None


def test_get_document_property_returns_existing_value():
    props = _UserDefinedProperties()
    props.values["WriterAgentGrammarCache"] = '{"fp":[1]}'
    doc = _DocWithUserDefinedProperties(props)

    assert get_document_property(doc, "WriterAgentGrammarCache", default=None) == '{"fp":[1]}'


def test_document_helpers_import_does_not_load_calc_analyzer():
    """document_helpers must not import SheetAnalyzer/CalcBridge at module load."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    code = (
        "import sys\n"
        "import plugin.doc.document_helpers\n"
        "assert 'plugin.calc.analyzer' not in sys.modules\n"
        "assert 'plugin.calc.bridge' not in sys.modules\n"
        "assert not hasattr(plugin.doc.document_helpers, 'get_calc_context_for_chat')\n"
        "assert not hasattr(plugin.doc.document_helpers, 'collect_tracked_changes')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0, result.stdout + result.stderr


