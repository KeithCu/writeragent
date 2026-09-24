

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


def test_writer_compound_undo_context_manager_closes_on_success_and_error():
    doc = _MockDoc(recording=True)
    with WriterCompoundUndo(doc, "WriterAgent: with-ok") as cu:
        assert cu is not None
        assert doc.undo.entered is True
        assert doc.undo.left is False
    assert doc.undo.left is True

    doc2 = _MockDoc(recording=True)
    try:
        with WriterCompoundUndo(doc2, "WriterAgent: with-err"):
            assert doc2.undo.entered is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert doc2.undo.left is True


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
        "import sys, types\n"
        "if 'pyuno' not in sys.modules:\n"
        "    _mod = types.ModuleType('pyuno')\n"
        "    _mod.getComponentContext = lambda: None\n"
        "    sys.modules['pyuno'] = _mod\n"
        "import plugin.doc.document_helpers\n"
        "assert 'plugin.calc.analyzer' not in sys.modules\n"
        "assert 'plugin.calc.bridge' not in sys.modules\n"
        "assert 'plugin.draw.bridge' not in sys.modules\n"
        "assert not hasattr(plugin.doc.document_helpers, 'get_calc_context_for_chat')\n"
        "assert not hasattr(plugin.doc.document_helpers, 'get_draw_context_for_chat')\n"
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


def test_document_helpers_uno_skips_windows_leftover_hidden_mml() -> None:
    """GHA 34678020608: leftover Hidden _blank .mml hang after latex skip."""
    from pathlib import Path

    src = Path(__file__).with_name("test_document_helpers_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "document helpers Hidden _blank .mml" in src


# ── DocumentService.doc_key + cache invalidation (Nelson item 3) ──


class _KeyDoc:
    def __init__(self, uid="", url=""):
        self._uid = uid
        self._url = url

    def getRuntimeUID(self):
        return self._uid

    def getURL(self):
        return self._url


class _FakeCacheModel:
    def __init__(self, uid, url=""):
        self._uid = uid
        self._url = url
        self.modify_listeners = []
        self.doc_listeners = []
        self.dead = False

    def getRuntimeUID(self):
        if self.dead:
            raise RuntimeError("disposed")
        return self._uid

    def getURL(self):
        if self.dead:
            raise RuntimeError("disposed")
        return self._url

    def addModifyListener(self, listener):
        self.modify_listeners.append(listener)

    def removeModifyListener(self, listener):
        self.modify_listeners.remove(listener)

    def addDocumentEventListener(self, listener):
        self.doc_listeners.append(listener)

    def removeDocumentEventListener(self, listener):
        self.doc_listeners.remove(listener)


def _clear_cache_listener_state():
    import plugin.doc.document_helpers as dh

    dh._CACHE_LISTENERS.clear()
    dh._IGNORE_DEPTH = 0


def test_doc_key_prefers_runtime_uid_then_url_never_id():
    from plugin.doc.document_helpers import UNKNOWN_DOC_KEY, DocumentService
    from plugin.framework.uno_context import normalize_doc_url

    _clear_cache_listener_state()
    svc = DocumentService()
    assert svc.doc_key(_KeyDoc(uid="42", url="file:///docs/a.odt")) == "uid:42"
    assert svc.doc_key(_KeyDoc(uid="", url="file:///docs/a.odt/")) == "url:" + normalize_doc_url(
        "file:///docs/a.odt/"
    )
    untitled = _KeyDoc(uid="7", url="")
    assert svc.doc_key(untitled) == "uid:7"
    unknown = _KeyDoc()
    assert svc.doc_key(unknown) == UNKNOWN_DOC_KEY
    assert svc.doc_key(unknown) != id(unknown)
    assert svc.doc_key(None) == UNKNOWN_DOC_KEY


def test_doc_key_geturl_error_is_unknown_not_id():
    from plugin.doc.document_helpers import UNKNOWN_DOC_KEY, DocumentService

    class _Boom:
        def getRuntimeUID(self):
            return ""

        def getURL(self):
            raise RuntimeError("disposed")

    svc = DocumentService()
    boom = _Boom()
    assert svc.doc_key(boom) == UNKNOWN_DOC_KEY
    assert svc.doc_key(boom) != id(boom)


def test_ignore_cache_invalidation_is_reentrant_and_drops_emits():
    from types import SimpleNamespace

    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.event_bus import get_event_bus

    _clear_cache_listener_state()
    svc = DocumentService()
    model = _FakeCacheModel("7")
    assert svc.doc_key(model) == "uid:7"
    assert len(model.modify_listeners) == 1

    received = []

    def handler(**kwargs):
        received.append(kwargs)

    bus = get_event_bus()
    bus.subscribe("document:cache_invalidated", handler)
    try:
        event = SimpleNamespace(Source=model)
        model.modify_listeners[0].modified(event)
        assert received and received[-1].get("doc") is model
        received.clear()
        with svc.ignore_cache_invalidation():
            with svc.ignore_cache_invalidation():
                model.modify_listeners[0].modified(event)
            model.modify_listeners[0].modified(event)
        assert received == []
        model.modify_listeners[0].modified(event)
        assert received and received[-1].get("doc") is model
    finally:
        bus.unsubscribe("document:cache_invalidated", handler)
        _clear_cache_listener_state()


def test_closed_doc_emit_uses_stored_key_without_touching_model():
    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.event_bus import get_event_bus

    _clear_cache_listener_state()
    svc = DocumentService()
    model = _FakeCacheModel("gone")
    svc.doc_key(model)
    model.dead = True

    received = []

    def handler(**kwargs):
        received.append(kwargs)

    bus = get_event_bus()
    bus.subscribe("document:cache_invalidated", handler)
    try:
        model.modify_listeners[0].disposing(None)
        assert received == [{"key": "uid:gone"}]
        assert "uid:gone" not in __import__(
            "plugin.doc.document_helpers", fromlist=["_CACHE_LISTENERS"]
        )._CACHE_LISTENERS
    finally:
        bus.unsubscribe("document:cache_invalidated", handler)
        _clear_cache_listener_state()


def test_cache_listener_dedupes_by_uid_and_recycle_does_not_evict_new():
    import plugin.doc.document_helpers as dh
    from plugin.doc.document_helpers import DocumentService

    _clear_cache_listener_state()
    svc = DocumentService()
    first = _FakeCacheModel("R")
    second = _FakeCacheModel("R")
    svc.doc_key(first)
    svc.doc_key(second)
    assert len(dh._CACHE_LISTENERS) == 1
    assert len(first.modify_listeners) == 1
    assert second.modify_listeners == []

    old = dh._CACHE_LISTENERS["uid:R"].modify
    replacement = dh._CacheModifyListener("uid:R")
    dh._CACHE_LISTENERS["uid:R"].modify = replacement
    old.disposing(None)
    assert "uid:R" in dh._CACHE_LISTENERS
    assert dh._CACHE_LISTENERS["uid:R"].modify is replacement
    _clear_cache_listener_state()


