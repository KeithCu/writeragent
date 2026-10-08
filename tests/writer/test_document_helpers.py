import pytest

from plugin.doc.udprops import get_document_property, set_document_property
from plugin.writer.edit_review import (
    TRACKED_SELECTION_MESSAGE,
    TrackedChangesInSelection,
    WriterCompoundUndo,
    WriterStreamedAppendSession,
    WriterStreamedRewriteSession,
    build_writer_rewrite_prompt,
)


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
        self.undone = False
        self.titles = []
        self.discard_empty_context = False

    def enterUndoContext(self, title: str) -> None:
        self.entered = True
        self.current_title = title
        assert "WriterAgent" in title

    def leaveUndoContext(self) -> None:
        self.left = True
        if hasattr(self, "current_title"):
            if not self.discard_empty_context:
                self.titles.insert(0, self.current_title)
            del self.current_title

    def getAllUndoActionTitles(self):
        return tuple(self.titles)

    def undo(self) -> None:
        self.undone = True
        if self.titles:
            self.titles.pop(0)


class _MockDoc:
    def __init__(self, recording=True):
        self.props = {"RecordChanges": recording}
        self.writes = []
        self.undo = _MockUndoManager()

    def getPropertyValue(self, name):
        return self.props[name]

    def setPropertyValue(self, name, value):
        self.writes.append((name, value))
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

    # The actual text restoration is now handled by doc.getUndoManager().undo() which is mocked.
    # We check that undo was called.
    assert doc.undo.undone is True
    assert doc.getPropertyValue("RecordChanges") is True
    assert doc.undo.left is True


def test_writer_streamed_rewrite_session_abort_empty_does_not_undo_previous_edit():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    # If the context was empty and discarded by LibreOffice leaveUndoContext,
    # the top undo title is the user's previous action, not the session title.
    doc.undo.discard_empty_context = True
    doc.undo.titles = ["Typing: hello"]
    session.abort_and_restore()

    assert doc.undo.undone is False
    assert doc.undo.titles == ["Typing: hello"]
    assert doc.getPropertyValue("RecordChanges") is True


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
    assert doc.undo.undone is True


def test_writer_streamed_rewrite_session_empty_finish_restores_original():
    """No model text must not erase the selection or record a deletion of it."""
    for recording in (False, True):
        doc = _MockDoc(recording=recording)
        text_range = _MutableTextRange()
        session = WriterStreamedRewriteSession(doc, text_range, "Original")

        assert text_range.getString() == ""
        assert session.finish() is None
        # Closing and undoing compound undo restores text and formatting without flattening
        assert doc.undo.undone is True
        # Recording ends as the document started it. A tracked deletion would
        # have left the range "" after turning RecordChanges back on.
        assert doc.getPropertyValue("RecordChanges") is recording
        assert doc.undo.left is True


def test_writer_streamed_rewrite_session_abort_fallback_when_compound_undo_never_opened():
    class NoUndoDoc(_MockDoc):
        def getUndoManager(self):
            return None

    doc = NoUndoDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")
    assert text_range.getString() == ""

    session.abort_and_restore()
    assert text_range.getString() == "Original"
    assert doc.getPropertyValue("RecordChanges") is True


def test_writer_streamed_rewrite_session_abort_fallback_when_undo_raises():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")
    assert text_range.getString() == ""

    def failing_undo():
        raise RuntimeError("undo failed")

    doc.undo.undo = failing_undo
    session.abort_and_restore()
    assert text_range.getString() == "Original"
    assert doc.getPropertyValue("RecordChanges") is True


def test_writer_streamed_rewrite_session_empty_finish_fallback_when_undo_fails():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")
    assert text_range.getString() == ""

    def failing_undo():
        raise RuntimeError("undo failed")

    doc.undo.undo = failing_undo
    assert session.finish() is None
    assert text_range.getString() == "Original"
    assert doc.getPropertyValue("RecordChanges") is True


def test_writer_streamed_rewrite_session_whitespace_chunk_is_kept():
    """Whitespace the model sent is a real rewrite, not an empty result."""
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    session.append_chunk(" ")
    assert session.finish() is None
    assert text_range.getString() == " "
    assert doc.getPropertyValue("RecordChanges") is True


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
        "if 'uno' not in sys.modules:\n"
        "    sys.modules['uno'] = types.ModuleType('uno')\n"
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


# --- Extend/Edit Selection must not accept or flatten existing redlines ---


class _Portion:
    def __init__(self, portion_type, redline_type=None, text="x"):
        self._portion_type = portion_type
        self._redline_type = redline_type
        self._text = text

    def getPropertyValue(self, name):
        if name == "TextPortionType":
            return self._portion_type
        if name == "RedlineType":
            if self._redline_type is None:
                raise RuntimeError("no redline type")
            return self._redline_type
        raise RuntimeError(name)

    def getString(self):
        return self._text


class _PortionEnum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


class _Paragraph:
    def __init__(self, portions):
        self._portions = list(portions)

    def createEnumeration(self):
        return _PortionEnum(self._portions)

    def getString(self):
        return "".join(portion.getString() for portion in self._portions)

    def getPropertyValue(self, name):
        raise RuntimeError("paragraph has no %s" % name)


class _RedlineCursor(_MutableTextRange):
    """Cursor enumeration yields paragraphs; each paragraph yields portions."""

    def __init__(self, paragraphs):
        super().__init__()
        self._paragraphs = list(paragraphs)
        self.set_calls = []

    def setString(self, value):
        self.set_calls.append(value)
        super().setString(value)

    def createEnumeration(self):
        return _PortionEnum(self._paragraphs)


class _PortionCursor(_MutableTextRange):
    """Range whose own enumeration is the portion list (a paragraph selection)."""

    def __init__(self, portions):
        super().__init__()
        self._portions = list(portions)
        self.set_calls = []

    def setString(self, value):
        self.set_calls.append(value)
        super().setString(value)

    def createEnumeration(self):
        return _PortionEnum(self._portions)


class _BoomEnum:
    def hasMoreElements(self):
        raise RuntimeError("enum failed")


class _BoomCursor(_MutableTextRange):
    def __init__(self):
        super().__init__()
        self.set_calls = []

    def setString(self, value):
        self.set_calls.append(value)
        super().setString(value)

    def createEnumeration(self):
        return _BoomEnum()


class _IndexSelection:
    def __init__(self, text_range):
        self._text_range = text_range

    def getCount(self):
        return 1

    def getByIndex(self, index):
        return self._text_range


class _MenuController:
    def __init__(self, text_range):
        self._selection = _IndexSelection(text_range)

    def getSelection(self):
        return self._selection


class _MenuDoc(_MockDoc):
    def __init__(self, text_range, recording=True):
        super().__init__(recording=recording)
        self.CurrentController = _MenuController(text_range)


def _tracked_cursor(kind):
    return _RedlineCursor([
        _Paragraph([
            _Portion("Text", text="Hello"),
            _Portion("Redline", kind),
            _Portion("Text", text="there"),
            _Portion("Redline", kind),
        ])
    ])


@pytest.mark.parametrize("kind", ["Insert", "Delete"])
@pytest.mark.parametrize("session_cls", [WriterStreamedRewriteSession, WriterStreamedAppendSession])
def test_streamed_session_refuses_tracked_insert_or_delete(kind, session_cls):
    doc = _MockDoc(recording=True)
    text_range = _tracked_cursor(kind)
    with pytest.raises(TrackedChangesInSelection) as raised:
        session_cls(doc, text_range, "Hello there")
    assert str(raised.value) == TRACKED_SELECTION_MESSAGE
    assert text_range.set_calls == []
    assert doc.writes == []
    assert doc.props["RecordChanges"] is True
    assert doc.undo.entered is False


def test_streamed_session_refuses_portion_level_insert():
    doc = _MockDoc(recording=True)
    text_range = _PortionCursor([
        _Portion("Text", text="Hello"),
        _Portion("Redline", "Insert"),
        _Portion("Text", text="x"),
        _Portion("Redline", "Insert"),
    ])
    with pytest.raises(TrackedChangesInSelection):
        WriterStreamedAppendSession(doc, text_range, "Hellox")
    assert text_range.set_calls == []
    assert doc.writes == []
    assert doc.undo.entered is False


def test_streamed_session_refuses_unreadable_redline_type():
    class _Unreadable(_Portion):
        def getPropertyValue(self, name):
            if name == "TextPortionType":
                return "Redline"
            raise RuntimeError("type unreadable")

    doc = _MockDoc(recording=True)
    text_range = _PortionCursor([_Portion("Text", text="Hello"), _Unreadable("Redline")])
    with pytest.raises(TrackedChangesInSelection):
        WriterStreamedRewriteSession(doc, text_range, "Hello")
    assert text_range.set_calls == []
    assert doc.writes == []


def test_streamed_session_refuses_when_portion_walk_fails():
    doc = _MockDoc(recording=True)
    text_range = _BoomCursor()
    with pytest.raises(TrackedChangesInSelection):
        WriterStreamedRewriteSession(doc, text_range, "Hello")
    assert text_range.set_calls == []
    assert doc.writes == []
    assert doc.undo.entered is False


def test_streamed_rewrite_allows_format_redline():
    doc = _MockDoc(recording=True)
    text_range = _RedlineCursor([
        _Paragraph([_Portion("Text", text="Hello"), _Portion("Redline", "Format")])
    ])
    session = WriterStreamedRewriteSession(doc, text_range, "Hello")
    assert text_range.set_calls == [""]
    assert doc.props["RecordChanges"] is False
    assert session.finish() is None


def test_streamed_append_allows_format_redline_and_still_appends():
    doc = _MockDoc(recording=True)
    text_range = _RedlineCursor([
        _Paragraph([_Portion("Redline", "Format"), _Portion("Text", text="Hi")])
    ])
    session = WriterStreamedAppendSession(doc, text_range, "Hi")
    assert doc.props["RecordChanges"] is False
    session.append_chunk("!")
    assert text_range.set_calls == ["Hi!"]


def test_menu_message_matches_session_exception():
    from pathlib import Path

    src = Path(__file__).resolve().parents[2].joinpath("plugin/writer/editselection.py").read_text(encoding="utf-8")
    assert TRACKED_SELECTION_MESSAGE in src


def test_do_extend_selection_refuses_deletion_only_without_writing(monkeypatch):
    from plugin.writer import editselection

    text_range = _RedlineCursor([
        _Paragraph([
            _Portion("Redline", "Delete"),
            _Portion("Text", text="gone"),
            _Portion("Redline", "Delete"),
        ])
    ])
    doc = _MenuDoc(text_range, recording=True)
    messages = []
    streams = []
    monkeypatch.setattr(editselection, "msgbox", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(editselection, "stream_completion", lambda *args, **kwargs: streams.append(args))

    editselection.do_extend_selection(object(), doc, object())

    assert streams == []
    assert len(messages) == 1
    assert "Extend Selection" in messages[0][1]
    assert messages[0][2] == TRACKED_SELECTION_MESSAGE
    assert text_range.set_calls == []
    assert doc.writes == []
    assert doc.undo.entered is False


def test_do_edit_selection_refuses_tracked_insert_before_prompt(monkeypatch):
    from plugin.writer import editselection

    text_range = _tracked_cursor("Insert")
    doc = _MenuDoc(text_range, recording=True)
    messages = []
    prompts = []
    streams = []
    monkeypatch.setattr(editselection, "msgbox", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(
        editselection,
        "prompt_for_edit_instructions",
        lambda *args, **kwargs: prompts.append(args) or ("x", ""),
    )
    monkeypatch.setattr(editselection, "stream_completion", lambda *args, **kwargs: streams.append(args))

    editselection.do_edit_selection(object(), doc, object())

    assert prompts == []
    assert streams == []
    assert len(messages) == 1
    assert "Edit Selection" in messages[0][1]
    assert messages[0][2] == TRACKED_SELECTION_MESSAGE
    assert text_range.set_calls == []
    assert doc.writes == []


def test_do_extend_selection_clean_range_still_streams(monkeypatch):
    from plugin.writer import editselection

    text_range = _MutableTextRange()
    text_range.text = "Hello"
    doc = _MenuDoc(text_range, recording=False)
    messages = []
    streams = []
    monkeypatch.setattr(editselection, "msgbox", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(editselection, "get_config_str", lambda key: "")
    monkeypatch.setattr(editselection, "get_current_endpoint", lambda: "ep")
    monkeypatch.setattr(editselection, "update_lru_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(editselection, "get_config_int", lambda key: 20)
    monkeypatch.setattr(editselection, "get_text_model", lambda: "model")
    monkeypatch.setattr(editselection, "create_validated_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(editselection, "review_recording_enabled", lambda ctx: False)
    monkeypatch.setattr(editselection, "stream_completion", lambda *args, **kwargs: streams.append(args))

    editselection.do_extend_selection(object(), doc, object())

    assert messages == []
    assert len(streams) == 1
    assert text_range.getString() == "Hello"
    assert doc.props["RecordChanges"] is False


def test_do_edit_selection_clean_range_still_clears_and_streams(monkeypatch):
    from plugin.writer import editselection

    text_range = _MutableTextRange()
    text_range.text = "Hello"
    doc = _MenuDoc(text_range, recording=False)
    messages = []
    streams = []
    monkeypatch.setattr(editselection, "msgbox", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(editselection, "prompt_for_edit_instructions", lambda *args, **kwargs: ("shorter", ""))
    monkeypatch.setattr(editselection, "get_config_int", lambda key: 20)
    monkeypatch.setattr(editselection, "create_validated_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(editselection, "review_recording_enabled", lambda ctx: False)
    monkeypatch.setattr(editselection, "stream_completion", lambda *args, **kwargs: streams.append(args))

    editselection.do_edit_selection(object(), doc, object())

    assert messages == []
    assert len(streams) == 1
    assert text_range.getString() == ""



def test_do_extend_selection_observes_stop_checker(monkeypatch):
    from plugin.writer import editselection

    text_range = _MutableTextRange()
    text_range.text = "Hello"
    doc = _MenuDoc(text_range, recording=False)
    messages = []
    streams = []
    monkeypatch.setattr(editselection, "msgbox", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(editselection, "get_config_str", lambda key: "")
    monkeypatch.setattr(editselection, "get_current_endpoint", lambda: "ep")
    monkeypatch.setattr(editselection, "update_lru_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(editselection, "get_config_int", lambda key: 20)
    monkeypatch.setattr(editselection, "get_text_model", lambda: "model")
    monkeypatch.setattr(editselection, "create_validated_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(editselection, "review_recording_enabled", lambda ctx: False)

    class _Ctx:
        def stop_checker(self) -> bool:
            return True

    def stream_completion_mock(*args, **kwargs):
        streams.append(args)
        assert "stop_checker" not in kwargs
        apply_chunk = args[5]
        # Stop does not drop a chunk already in hand (#1273).
        apply_chunk("more")

    monkeypatch.setattr(editselection, "stream_completion", stream_completion_mock)

    editselection.do_extend_selection(_Ctx(), doc, object())

    assert len(streams) == 1
    assert text_range.getString() == "Hellomore"


def test_do_edit_selection_observes_stop_checker(monkeypatch):
    from plugin.writer import editselection

    text_range = _MutableTextRange()
    text_range.text = "Hello"
    doc = _MenuDoc(text_range, recording=False)
    messages = []
    streams = []
    monkeypatch.setattr(editselection, "msgbox", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(editselection, "prompt_for_edit_instructions", lambda *args, **kwargs: ("shorter", ""))
    monkeypatch.setattr(editselection, "get_config_int", lambda key: 20)
    monkeypatch.setattr(editselection, "create_validated_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(editselection, "review_recording_enabled", lambda ctx: False)

    class _Ctx:
        def stop_checker(self) -> bool:
            return True

    def stream_completion_mock(*args, **kwargs):
        streams.append(args)
        assert "stop_checker" not in kwargs
        apply_chunk = args[5]
        # Stop does not drop a chunk already in hand (#1273).
        apply_chunk("more")

    monkeypatch.setattr(editselection, "stream_completion", stream_completion_mock)

    editselection.do_edit_selection(_Ctx(), doc, object())

    assert len(streams) == 1
    assert text_range.getString() == "more"


def test_stop_checker_exception_fails_closed():
    from plugin.framework.queue_executor import bind_send_stop_checker

    class MockScope:
        def is_cancelled(self):
            raise ValueError("Some internal error")

    # When scope raises an error, bind_send_stop_checker returns True
    checker1 = bind_send_stop_checker(MockScope())
    assert checker1() is True

    def failing_fallback():
        raise ValueError("Some internal error")

    checker2 = bind_send_stop_checker(None, fallback=failing_fallback)
    assert checker2() is True

    checker3 = bind_send_stop_checker(MockScope(), fallback=failing_fallback)
    assert checker3() is True


def test_do_extend_selection_failure_visible(monkeypatch):
    from plugin.writer import editselection

    text_range = _MutableTextRange()
    text_range.text = "Hello"
    doc = _MenuDoc(text_range, recording=False)
    messages = []

    monkeypatch.setattr(editselection, "msgbox", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(editselection, "get_config_str", lambda key: "")
    monkeypatch.setattr(editselection, "get_current_endpoint", lambda: "ep")
    monkeypatch.setattr(editselection, "update_lru_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(editselection, "get_config_int", lambda key: 20)
    monkeypatch.setattr(editselection, "get_text_model", lambda: "model")
    monkeypatch.setattr(editselection, "create_validated_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(editselection, "review_recording_enabled", lambda ctx: False)

    def stream_completion_mock(ctx, client, prompt, system_prompt, max_tokens, apply_chunk, on_done, on_error, stop_checker=None):
        on_error(ValueError("simulated stream failure"))

    monkeypatch.setattr(editselection, "stream_completion", stream_completion_mock)

    editselection.do_extend_selection(object(), doc, object())

    assert len(messages) == 1
    assert "simulated stream failure" in messages[0][2]


def test_document_helpers_unohelper_import_error():
    """Verify document_helpers handles unohelper ImportError gracefully."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    # What was wrong: macOS fix_uno_import.py puts LibreOffice uno.py on the
    # venv path without pyuno. text_helpers imports uno, uno.py imports pyuno,
    # and the child died before the unohelper handler (GHA 37719597720).
    # Windows was skipped because that .pth loads LO's native pyuno.
    # Why: stub pyuno and uno first, same as the analyzer-import test, so
    # this child never loads the office binary.
    code = (
        "import sys, types\n"
        "if 'pyuno' not in sys.modules:\n"
        "    _pyuno = types.ModuleType('pyuno')\n"
        "    _pyuno.getComponentContext = lambda: None\n"
        "    sys.modules['pyuno'] = _pyuno\n"
        "if 'uno' not in sys.modules:\n"
        "    sys.modules['uno'] = types.ModuleType('uno')\n"
        "class _BlockedImporter:\n"
        "    def find_spec(self, fullname, path, target=None):\n"
        "        if fullname == 'unohelper' or fullname.startswith('unohelper.'):\n"
        "            raise ImportError('unohelper blocked for test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _BlockedImporter())\n"
        "import plugin.doc.document_helpers as dh\n"
        "assert dh._HAVE_UNO_LISTENERS is False\n"
        "assert dh._unohelper is None\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_writer_has_math_ole_logs_exception(caplog):
    import logging
    from unittest.mock import MagicMock
    from plugin.doc.document_helpers import _writer_has_math_ole

    doc = MagicMock()
    doc.getEmbeddedObjects.side_effect = RuntimeError("embedded objects boom")
    with caplog.at_level(logging.DEBUG, logger="writeragent.document"):
        has_math = _writer_has_math_ole(doc)
    assert has_math is False
    # Release strips log.debug; returning False is the behavior that must survive.
    from plugin.doc import document_helpers as document_helpers_mod
    from tests.harness.strip_bundle import module_source_contains

    if module_source_contains(document_helpers_mod, "Failed checking Math OLE"):
        assert any("Failed checking Math OLE" in record.message for record in caplog.records)


def test_get_document_context_for_chat_writer_uno_object_error():
    from unittest.mock import MagicMock, patch
    from plugin.doc.document_helpers import get_document_context_for_chat
    from plugin.framework.errors import UnoObjectError

    doc = MagicMock()
    doc.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    with patch("plugin.doc.text_helpers._writer_char_count", side_effect=UnoObjectError("disposed")):
        with patch("plugin.doc.text_helpers.get_selection_text", return_value="my selection"):
            res = get_document_context_for_chat(doc)
    assert "[Document text reading failed. Active selection: my selection]" in res

