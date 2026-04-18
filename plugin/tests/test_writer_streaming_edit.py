from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.framework.document import (
    build_writer_rewrite_prompt,
    get_string_without_tracked_deletions,
    WriterStreamedRewriteSession,
)


class _Enum:
    def __init__(self, items):
        self._items = list(items)
        self._idx = 0

    def hasMoreElements(self):
        return self._idx < len(self._items)

    def nextElement(self):
        item = self._items[self._idx]
        self._idx += 1
        return item


class _Portion:
    def __init__(self, text="", portion_type="Text", redline_type=None):
        self._text = text
        self._portion_type = portion_type
        self._redline_type = redline_type

    def getPropertyValue(self, name):
        if name == "TextPortionType":
            return self._portion_type
        if name == "RedlineType":
            return self._redline_type
        raise KeyError(name)

    def getString(self):
        return self._text


class _Paragraph:
    def __init__(self, portions, fallback_text=""):
        self._portions = portions
        self._fallback_text = fallback_text

    def createEnumeration(self):
        return _Enum(self._portions)

    def getString(self):
        return self._fallback_text


class _TextRange:
    def __init__(self, paragraphs, fallback_text):
        self._paragraphs = paragraphs
        self._fallback_text = fallback_text

    def createEnumeration(self):
        return _Enum(self._paragraphs)

    def getString(self):
        return self._fallback_text


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


class _MockDoc:
    def __init__(self, recording=True):
        self.props = {"RecordChanges": recording}

    def getPropertyValue(self, name):
        return self.props[name]

    def setPropertyValue(self, name, value):
        self.props[name] = value


def test_get_string_without_tracked_deletions_skips_deleted_portions():
    text_range = _TextRange(
        [
            _Paragraph(
                [
                    _Portion("Keep "),
                    _Portion(portion_type="Redline", redline_type="Delete"),
                    _Portion("remove me"),
                    _Portion(portion_type="Redline", redline_type="Delete"),
                    _Portion("text"),
                ],
                fallback_text="Keep remove metext",
            ),
            _Paragraph([_Portion("Next line")], fallback_text="Next line"),
        ],
        fallback_text="Keep remove metext\nNext line",
    )

    assert get_string_without_tracked_deletions(text_range) == "Keep text\nNext line"


def test_build_writer_rewrite_prompt_uses_direct_rewrite_format():
    prompt = build_writer_rewrite_prompt("Original text", "Make it shorter")

    assert "Rewrite the following text" in prompt
    assert "Instructions: Make it shorter" in prompt
    assert "Text to rewrite:\nOriginal text" in prompt


def test_writer_streamed_rewrite_session_finishes_as_single_tracked_change():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    assert doc.getPropertyValue("RecordChanges") is False
    assert text_range.getString() == ""

    session.append_chunk("Generated")
    warning = session.finish()

    assert warning is None
    assert text_range.getString() == "Generated"
    assert doc.getPropertyValue("RecordChanges") is True


def test_writer_streamed_rewrite_session_abort_restores_original_text():
    doc = _MockDoc(recording=True)
    text_range = _MutableTextRange()
    session = WriterStreamedRewriteSession(doc, text_range, "Original")

    session.append_chunk("Partial")
    session.abort_and_restore()

    assert text_range.getString() == "Original"
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
