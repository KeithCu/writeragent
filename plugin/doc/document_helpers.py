# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import logging
import uno
from typing import TypedDict
from enum import Enum, auto
from plugin.calc.bridge import CalcBridge
from plugin.calc.analyzer import SheetAnalyzer
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.uno_context import get_active_document as get_active_doc
from plugin.framework.errors import UnoObjectError, check_disposed, safe_call, safe_uno_call
from plugin.framework.thread_guard import assert_main_thread, _wrap_uno


def normalize_linebreaks(text: str | None) -> str:
    """Ensure all linebreaks use \n (LF).

    Some UNO APIs (especially on Windows) or clipboard paths can return \r\n
    or \r. This ensures consistent offsets and string length for the LLM.
    """
    if text is None:
        return ""
    # Normalize \r\n -> \n
    text = text.replace("\r\n", "\n")
    # Normalize \n\r (rare but possible) -> \n
    text = text.replace("\n\r", "\n")
    # Normalize remaining \r -> \n
    text = text.replace("\r", "\n")
    return text


class HeadingTreeNode(TypedDict):
    """Shape of nodes returned by :func:`build_heading_tree` (recursive heading tree)."""

    level: int
    text: str
    para_index: int
    children: list["HeadingTreeNode"]
    body_paragraphs: int


class DocumentType(Enum):
    UNKNOWN = auto()
    WRITER = auto()
    CALC = auto()
    DRAW = auto()
    IMPRESS = auto()


_DOCUMENT_SERVICE_MAP = {DocumentType.WRITER: "com.sun.star.text.TextDocument", DocumentType.CALC: "com.sun.star.sheet.SpreadsheetDocument", DocumentType.DRAW: "com.sun.star.drawing.DrawingDocument", DocumentType.IMPRESS: "com.sun.star.presentation.PresentationDocument"}


@safe_uno_call(default=DocumentType.UNKNOWN)
def get_document_type(model):
    """Return the DocumentType for the given model."""
    assert_main_thread("document_helpers.get_document_type")
    if model is None:
        return DocumentType.UNKNOWN

    # Check services in priority order
    for doc_type, service_name in _DOCUMENT_SERVICE_MAP.items():
        if safe_call(model.supportsService, f"Check {service_name}", service_name):
            return doc_type

    return DocumentType.UNKNOWN


def is_writer(model):
    """Return True if model is a Writer document."""
    return get_document_type(model) == DocumentType.WRITER


def is_calc(model):
    """Return True if model is a Calc document."""
    return get_document_type(model) == DocumentType.CALC


def is_draw(model):
    """Return True if model is a Draw/Impress document."""
    doc_type = get_document_type(model)
    return doc_type in (DocumentType.DRAW, DocumentType.IMPRESS)


def get_string_without_tracked_deletions(text_range) -> str:
    """Return text_range text while skipping tracked deletions when possible."""
    if hasattr(text_range, "_mock_return_value") or type(text_range).__name__ in ("Mock", "MagicMock"):
        return text_range.getString()
    try:
        para_enum = text_range.createEnumeration()
    except Exception:
        return text_range.getString()

    parts: list[str] = []
    try:
        first_para = True
        while para_enum.hasMoreElements():
            para = para_enum.nextElement()
            if not first_para:
                parts.append("\n")
            first_para = False

            try:
                portion_enum = para.createEnumeration()
            except Exception:
                parts.append(para.getString())
                continue

            in_delete = False
            while portion_enum.hasMoreElements():
                portion = portion_enum.nextElement()
                try:
                    try:
                        portion_type = portion.getPropertyValue("TextPortionType")
                    except Exception:
                        portion_type = portion.TextPortionType
                except Exception:
                    continue

                if portion_type == "Redline":
                    try:
                        if str(portion.getPropertyValue("RedlineType")) == "Delete":
                            in_delete = not in_delete
                    except Exception:
                        pass
                    continue

                if in_delete:
                    continue

                try:
                    chunk = portion.getString()
                except Exception:
                    continue
                if chunk:
                    parts.append(chunk)
    except Exception:
        return text_range.getString()

    return "".join(parts)


def build_writer_rewrite_prompt(original_text: str, instructions: str) -> str:
    """Return a direct rewrite prompt for Writer selection edits."""
    return f"Rewrite the following text according to the instructions below. Output only the rewritten text with no labels, headings, or explanations.\n\nInstructions: {instructions}\n\nText to rewrite:\n{original_text}"


class WriterCompoundUndo:
    """Wrap ``XUndoManager.enterUndoContext`` / ``leaveUndoContext`` for one Ctrl+Z step.

    Call :meth:`close` when the operation finishes (success or error). Safe to call
    multiple times.
    """

    def __init__(self, doc, title: str) -> None:
        self._log = logging.getLogger(__name__)
        self._title = title
        self._undo_manager = None
        self._open = False
        try:
            if not hasattr(doc, "getUndoManager"):
                self._log.warning("WriterCompoundUndo: doc has no getUndoManager, undo grouping skipped (title=%r)", title)
                return
            um = doc.getUndoManager()
            if um is None:
                self._log.warning("WriterCompoundUndo: getUndoManager() returned None, undo grouping skipped (title=%r)", title)
                return
            # Probe undo manager state to detect prior unclosed contexts (best-effort; UNO may not expose these).
            try:
                is_in_ctx = um.isInContext()
                undo_enabled = um.isUndoEnabled()
                self._log.info("WriterCompoundUndo: pre-enter state isInContext=%s isUndoEnabled=%s (title=%r)", is_in_ctx, undo_enabled, title)
            except Exception as probe_e:
                self._log.debug("WriterCompoundUndo: could not probe undo manager state: %s", probe_e)
            um.enterUndoContext(title)
            self._undo_manager = um
            self._open = True
            # Log after success so we always see this when the context is live.
            self._log.info("WriterCompoundUndo: context entered %r", title)
        except Exception as e:
            # Upgrade from debug to warning so failures are visible without debug logging.
            # "Insert $1" in the undo menu means this context was never opened.
            self._log.warning("WriterCompoundUndo: enterUndoContext failed, undo grouping disabled (title=%r): %s", title, e)

    def close(self) -> None:
        """End the compound undo context if :meth:`__init__` opened one."""
        if not self._open:
            self._log.debug("WriterCompoundUndo.close: already closed or never opened (title=%r)", self._title)
            return
        self._open = False
        um = self._undo_manager
        self._undo_manager = None
        if um is None:
            return
        try:
            self._log.info("WriterCompoundUndo: leaving context %r", self._title)
            um.leaveUndoContext()
        except Exception:
            self._log.exception("leaveUndoContext failed (title=%r)", self._title)


class WriterStreamedRewriteSession:
    """Manage a streamed Writer edit that collapses to one tracked change."""

    _UNDO_CONTEXT_TITLE = "WriterAgent: Edit selection"

    def __init__(self, doc, text_range, original_text: str, track_reviewable: bool = False):
        self.doc = doc
        self.text_range = text_range
        self.original_text = original_text
        self.generated_text = ""
        self.was_recording = False
        # When True (opt-in flag), the agent's edit is collapsed into one tracked
        # change for the user to review even if they did not have Track Changes on.
        self.track_reviewable = track_reviewable
        self._compound_undo = WriterCompoundUndo(doc, self._UNDO_CONTEXT_TITLE)

        try:
            self.was_recording = bool(self.doc.getPropertyValue("RecordChanges"))
        except Exception:
            self.was_recording = False

        _log = logging.getLogger(__name__)
        _log.info("WriterStreamedRewriteSession: was_recording=%s, compound_undo open=%s", self.was_recording, self._compound_undo._open)
        try:
            if self.was_recording:
                self.doc.setPropertyValue("RecordChanges", False)
            self.text_range.setString("")
        except Exception:
            if self.was_recording:
                try:
                    self.doc.setPropertyValue("RecordChanges", True)
                except Exception:
                    pass
            self._compound_undo.close()
            raise

    def append_chunk(self, chunk: str) -> None:
        """Append streamed text to the visible range and shadow buffer."""
        if not chunk:
            return
        self.generated_text += chunk
        self.text_range.setString(self.generated_text)

    def finish(self) -> str | None:
        """Finalize the rewrite. Returns a warning message on degraded success."""
        try:
            if not (self.was_recording or self.track_reviewable):
                return None

            try:
                # Review mode only (NOT when the user merely has their own Track Changes on):
                # snapshot the redlines so the collapsed change can be tagged as an agent change
                # afterward, and author it as the agent for the by-author coloring.
                before_ids = None
                before_ids_ok = False
                prior_author = None
                if self.track_reviewable:
                    try:
                        from plugin.framework.uno_context import get_ctx
                        from plugin.writer import review_authors
                        from plugin.writer.edit_review import snapshot_redline_ids

                        before_ids, before_ids_ok = snapshot_redline_ids(self.doc)
                        prior_author = review_authors.begin(get_ctx())
                        # Make the markup visible so a reviewable change isn't left invisible
                        # when the user has Track Changes display off (matches
                        # EditReviewSession.__enter__). Review mode only -- never when the user
                        # merely has their own Track Changes on (we respect their view setting).
                        try:
                            self.doc.setPropertyValue("ShowChanges", True)
                        except Exception:
                            logging.getLogger(__name__).debug("streamed rewrite: could not force ShowChanges", exc_info=True)
                    except Exception:
                        logging.getLogger(__name__).debug("streamed rewrite: review tagging setup failed", exc_info=True)
                try:
                    self.text_range.setString(self.original_text)
                    self.doc.setPropertyValue("RecordChanges", True)
                    self.text_range.setString(self.generated_text)
                finally:
                    if prior_author is not None:
                        try:
                            from plugin.framework.uno_context import get_ctx
                            from plugin.writer import review_authors

                            review_authors.end(get_ctx(), prior_author)
                        except Exception:
                            logging.getLogger(__name__).warning("streamed rewrite: author restore failed", exc_info=True)
                # Restore the user's prior recording state. If they had Track Changes
                # OFF and we only turned it ON to capture this edit as one reviewable
                # redline (track_reviewable flag), turn it back OFF so their later
                # manual typing is not tracked. Existing redlines persist regardless.
                if not self.was_recording:
                    self.doc.setPropertyValue("RecordChanges", False)
                # Tag the collapsed redline(s) with a session token so the inline review UI
                # (click popup / context menu) treats this streamed edit as an agent change.
                if before_ids is not None:
                    try:
                        from plugin.writer.edit_review import tag_agent_redlines

                        tag_agent_redlines(self.doc, before_ids, before_reliable=before_ids_ok)
                    except Exception:
                        logging.getLogger(__name__).debug("streamed rewrite: redline tagging failed", exc_info=True)
                return None
            except Exception:
                logging.getLogger(__name__).exception("Failed to collapse streamed edit into one tracked change")

                fallback_errors: list[str] = []
                try:
                    self.doc.setPropertyValue("RecordChanges", False)
                except Exception as e:
                    fallback_errors.append(f"disable tracking failed: {e}")
                try:
                    self.text_range.setString(self.generated_text)
                except Exception as e:
                    fallback_errors.append(f"restore generated text failed: {e}")
                try:
                    self.doc.setPropertyValue("RecordChanges", self.was_recording)
                except Exception as e:
                    fallback_errors.append(f"restore recording state failed: {e}")

                if fallback_errors:
                    return "Failed to finalize the tracked edit and preserve the generated text: " + "; ".join(fallback_errors)
                return "Failed to collapse the streamed edit into a single tracked change. The generated text was kept, but it may still appear as multiple tracked changes."
        finally:
            self._compound_undo.close()

    def abort_and_restore(self) -> None:
        """Restore the original text and recording state after an error."""
        try:
            if self.was_recording:
                try:
                    self.doc.setPropertyValue("RecordChanges", False)
                except Exception:
                    pass
            self.text_range.setString(self.original_text)
        finally:
            if self.was_recording:
                try:
                    self.doc.setPropertyValue("RecordChanges", True)
                except Exception:
                    pass
            self._compound_undo.close()


class WriterStreamedAppendSession:
    """Manage a streamed Writer APPEND (extend-selection) that collapses to one tracked insertion.

    Unlike :class:`WriterStreamedRewriteSession` (which REPLACES the range), extend-selection
    keeps the user's original text and streams the agent's continuation AFTER it. Streaming runs
    with tracking OFF (the user sees the text appear without a redline per chunk); ``finish()``
    then converts ONLY the appended continuation into a single tracked INSERTION -- the original
    is never struck through -- authored as the agent and tagged for the inline review UI.
    """

    _UNDO_CONTEXT_TITLE = "WriterAgent: Extend selection"

    def __init__(self, doc, text_range, original_text: str, track_reviewable: bool = False):
        self.doc = doc
        self.text_range = text_range
        self.original_text = original_text
        self.appended_text = ""
        self.track_reviewable = track_reviewable
        self._compound_undo = WriterCompoundUndo(doc, self._UNDO_CONTEXT_TITLE)

        try:
            self.was_recording = bool(self.doc.getPropertyValue("RecordChanges"))
        except Exception:
            self.was_recording = False
        # Stream with tracking OFF so the live continuation isn't recorded as a redline per
        # chunk; finish() re-records the whole appended run as one tracked insertion.
        try:
            if self.was_recording:
                self.doc.setPropertyValue("RecordChanges", False)
        except Exception:
            pass

    def append_chunk(self, chunk: str) -> None:
        """Append streamed text after the original (tracking off; one redline created at finish)."""
        if not chunk:
            return
        self.appended_text += chunk
        try:
            self.text_range.setString(self.original_text + self.appended_text)
        except Exception:
            logging.getLogger(__name__).debug("streamed append: chunk apply failed", exc_info=True)

    def finish(self) -> str | None:
        """Collapse the appended continuation into one tracked insertion. Returns a warning on degraded success."""
        try:
            if not self.appended_text:
                # We may have turned off the user's own Record Changes in __init__ to avoid a
                # redline per streamed chunk. If the model produced nothing, there is no edit to
                # collapse, but the user's prior tracking state still must be restored.
                if self.was_recording:
                    try:
                        self.doc.setPropertyValue("RecordChanges", True)
                    except Exception:
                        pass
                return None
            if not (self.was_recording or self.track_reviewable):
                return None

            before_ids = None
            before_ids_ok = False
            prior_author = None
            if self.track_reviewable:
                try:
                    from plugin.framework.uno_context import get_ctx
                    from plugin.writer import review_authors
                    from plugin.writer.edit_review import snapshot_redline_ids

                    before_ids, before_ids_ok = snapshot_redline_ids(self.doc)
                    prior_author = review_authors.begin(get_ctx())
                    # Make the markup visible so a reviewable change isn't invisible when the
                    # user has Track Changes display off (matches EditReviewSession.__enter__).
                    try:
                        self.doc.setPropertyValue("ShowChanges", True)
                    except Exception:
                        logging.getLogger(__name__).debug("streamed append: could not force ShowChanges", exc_info=True)
                except Exception:
                    logging.getLogger(__name__).debug("streamed append: review tagging setup failed", exc_info=True)
            try:
                # Drop the untracked appended run (back to just the original), then re-insert ONLY
                # that run as a tracked insertion at the end -- so the original carries no redline.
                self.text_range.setString(self.original_text)
                self.doc.setPropertyValue("RecordChanges", True)
                text = self.text_range.getText()
                end_cursor = text.createTextCursorByRange(self.text_range.getEnd())
                text.insertString(end_cursor, self.appended_text, False)
            finally:
                if prior_author is not None:
                    try:
                        from plugin.framework.uno_context import get_ctx
                        from plugin.writer import review_authors

                        review_authors.end(get_ctx(), prior_author)
                    except Exception:
                        logging.getLogger(__name__).warning("streamed append: author restore failed", exc_info=True)
            # Restore the user's prior recording state (existing redlines persist regardless).
            if not self.was_recording:
                try:
                    self.doc.setPropertyValue("RecordChanges", False)
                except Exception:
                    pass
            if before_ids is not None:
                try:
                    from plugin.writer.edit_review import tag_agent_redlines

                    tag_agent_redlines(self.doc, before_ids, before_reliable=before_ids_ok)
                except Exception:
                    logging.getLogger(__name__).debug("streamed append: redline tagging failed", exc_info=True)
            return None
        except Exception:
            logging.getLogger(__name__).exception("Failed to collapse streamed append into one tracked change")
            # Degrade: keep the user's continuation (untracked) rather than losing it.
            try:
                self.doc.setPropertyValue("RecordChanges", False)
            except Exception:
                pass
            try:
                self.text_range.setString(self.original_text + self.appended_text)
            except Exception:
                pass
            try:
                self.doc.setPropertyValue("RecordChanges", self.was_recording)
            except Exception:
                pass
            return "Failed to collapse the streamed continuation into a single tracked change. The text was kept, but may not be reviewable."
        finally:
            self._compound_undo.close()

    def abort_and_restore(self) -> None:
        """After a streaming error, restore the recording state and close the undo group.

        The partial continuation is left in place, matching the prior extend-selection behavior."""
        try:
            self.doc.setPropertyValue("RecordChanges", bool(self.was_recording))
        except Exception:
            pass
        finally:
            self._compound_undo.close()


def _user_defined_property_exists(props, name) -> bool:
    """Return True iff ``name`` is already defined on ``UserDefinedProperties``.

    ``UserDefinedProperties`` is a ``com.sun.star.beans.PropertyBag`` which
    implements ``XPropertySet`` (offering ``getPropertySetInfo().hasPropertyByName``)
    and ``XPropertyContainer`` (``addProperty``/``removeProperty``) — but not
    ``XNameAccess``. So ``hasattr(props, "hasByName")`` is False, the old
    ``not exists`` branch always fired, and the second save raised
    ``Property name or handle already used``.
    """
    if hasattr(props, "getPropertySetInfo"):
        try:
            info = props.getPropertySetInfo()
        except Exception:
            info = None
        if info is not None and hasattr(info, "hasPropertyByName"):
            try:
                return bool(info.hasPropertyByName(name))
            except Exception:
                pass
    if hasattr(props, "hasByName"):
        try:
            return bool(props.hasByName(name))
        except Exception:
            pass
    return False


def get_document_property(model, name, default=None):
    """Get a custom document property from the model."""
    try:
        check_disposed(model, "Document Model")
        if hasattr(model, "getDocumentProperties"):
            doc_props = safe_call(model.getDocumentProperties, "Get document properties")
            props = doc_props.UserDefinedProperties
            if props is None:
                return default

            check_disposed(props, "UserDefinedProperties")

            if _user_defined_property_exists(props, name):
                return safe_call(props.getPropertyValue, "Get property value", name)
            return default
    except UnoObjectError:
        # WriterAgentSessionID is created on first session setup; missing until then is normal (often hits fallback path).
        _lg = logging.getLogger(__name__)
        if name == "WriterAgentSessionID":
            _lg.debug("get_document_property (optional property not set yet)")
        else:
            _lg.exception("get_document_property failed")
    except Exception:
        logging.getLogger(__name__).exception("Unexpected error in get_document_property")
    return default


def set_document_property(model, name, value):
    """Set a custom document property in the model."""
    try:
        check_disposed(model, "Document Model")
        if hasattr(model, "getDocumentProperties"):
            doc_props = safe_call(model.getDocumentProperties, "Get document properties")
            props = doc_props.UserDefinedProperties
            if props is not None:
                check_disposed(props, "UserDefinedProperties")
                exists = _user_defined_property_exists(props, name)

                if exists and hasattr(props, "setPropertyValue"):
                    safe_call(props.setPropertyValue, "Set property value", name, str(value))
                elif hasattr(props, "addProperty"):
                    REMOVABLE = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.REMOVABLE")
                    safe_call(props.addProperty, "Add property", name, REMOVABLE, str(value))
                elif hasattr(props, "setPropertyValue"):
                    safe_call(props.setPropertyValue, "Set property value (no addProperty)", name, str(value))
    except UnoObjectError:
        # Fallback context enrichment
        doc_url = ""
        readonly = ""
        try:
            if hasattr(model, "getURL"):
                doc_url = model.getURL() or ""
            if hasattr(model, "isReadonly"):
                readonly = str(model.isReadonly())
        except Exception:
            pass

        logging.getLogger(__name__).exception("set_document_property error (url=%s, readonly=%s)", doc_url, readonly)
        raise


def _normalize_doc_url(url):
    """Normalize document URL for comparison (strip, optional trailing slash)."""
    if not url:
        return ""
    s = str(url).strip()
    if s.endswith("/") and len(s) > 1:
        s = s[:-1]
    return s


def get_runtime_uid(model):
    """Stable per-session id for an open component.

    Unlike the document URL, ``RuntimeUID`` exists even for unsaved/untitled
    documents, so it can address a document that has no file on disk yet.
    Returns "" if unavailable.

    Tries ``getRuntimeUID()``, attribute access, and ``getPropertyValue("RuntimeUID")`` in turn
    because LibreOffice builds expose the id through different UNO surfaces. Only plain ``str`` /
    ``int`` values are accepted so auto-mocked UNO attributes (e.g. ``MagicMock.RuntimeUID``)
    cannot masquerade as a real uid.
    """
    for accessor in (
        lambda m: m.getRuntimeUID() if callable(getattr(m, "getRuntimeUID", None)) else None,
        lambda m: getattr(m, "RuntimeUID", None),
        lambda m: m.getPropertyValue("RuntimeUID"),
    ):
        try:
            raw = accessor(model)
            if isinstance(raw, bool):
                continue
            if isinstance(raw, int):
                return str(raw)
            if isinstance(raw, str) and raw:
                return raw
        except Exception:
            continue
    return ""


def resolve_document_by_url(ctx, url):
    """Resolve an open document by URL or RuntimeUID. Must be called on the UNO main thread.

    ``url`` may be a document URL or a ``RuntimeUID`` (as returned by
    ``list_open_documents``); the RuntimeUID also matches unsaved/untitled
    documents that have no URL yet.
    Returns (doc, doc_type) or (None, None) if not found.
    doc_type is one of 'writer', 'calc', 'draw'.
    """
    assert_main_thread("document_helpers.resolve_document_by_url")
    if not url or not str(url).strip():
        return (None, None)
    from plugin.framework.uno_context import get_desktop

    target = _normalize_doc_url(url)
    try:
        desktop = get_desktop(ctx)
        comps = desktop.getComponents()
        if not comps:
            return (None, None)
        enum = comps.createEnumeration()
        if not enum:
            return (None, None)
        while enum and enum.hasMoreElements():
            elem = enum.nextElement()
            try:
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and elem.getController():
                    model = elem.getController().getModel()
                if model is not None:
                    doc_url = _normalize_doc_url(model.getURL()) if hasattr(model, "getURL") else ""
                    uid = get_runtime_uid(model)
                    if (doc_url and doc_url == target) or (uid and uid == target):
                        doc_type_enum = get_document_type(model)
                        doc_type = "writer"
                        if doc_type_enum == DocumentType.CALC:
                            doc_type = "calc"
                        elif doc_type_enum in (DocumentType.DRAW, DocumentType.IMPRESS):
                            doc_type = "draw"
                        return (_wrap_uno(model), doc_type)
            except Exception as e:
                logging.getLogger(__name__).debug("resolve_document_by_url element error: %s", type(e).__name__)
                continue
    except Exception:
        logging.getLogger(__name__).exception("resolve_document_by_url enumeration error")
    return (None, None)


def get_document_path(model):
    """Return the local filesystem path for the document, or None if not a file URL (e.g. untitled)."""
    try:
        url = model.getURL()
        if not url or not str(url).startswith("file://"):
            return None
        return str(uno.fileUrlToSystemPath(url))
    except Exception as e:
        logging.getLogger(__name__).debug("get_document_path exception: %s", type(e).__name__)
        return None


def get_full_document_text(model, max_chars=CHAT_DOCUMENT_CONTEXT_MAX_CHARS):
    """Get full document text for Writer or summary for Calc, truncated to max_chars."""
    assert_main_thread("document_helpers.get_full_document_text")
    try:
        check_disposed(model, "Document Model")
        doc_type = get_document_type(model)

        if doc_type == DocumentType.CALC:
            # Calc document
            bridge = CalcBridge(model)
            analyzer = SheetAnalyzer(bridge)
            summary = analyzer.get_sheet_summary()
            text = f"Sheet: {summary['sheet_name']}\nUsed Range: {summary['used_range']}\n"
            text += f"Columns: {', '.join(filter(None, summary['headers']))}\n"
            # Maybe add some preview rows?
            return text

        if doc_type == DocumentType.WRITER:
            doc_len = _writer_char_count(model)
            take = min(doc_len, max_chars)
            excerpt = _read_writer_text_slice(model, 0, take)
            if doc_len > max_chars:
                excerpt += "\n\n[... document truncated ...]"
            return excerpt

        if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
            return get_draw_context_for_chat(model, max_chars)

        return ""
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_full_document_text failed")
        return ""


def get_document_end(model, max_chars=4000):
    """Get the last max_chars of the document."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", False)
        safe_call(cursor.gotoStart, "Cursor gotoStart", True)  # expand backward to select from start to end
        full = get_string_without_tracked_deletions(cursor)
        if len(full) <= max_chars:
            return full
        return full[-max_chars:]
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_document_end failed")
        return ""


# goRight(nCount, bExpand) takes short; max 32767 per call
_GO_RIGHT_CHUNK = 8192


def _writer_char_count(model) -> int:
    """Writer document character count; prefers O(1) CharacterCount over full getString()."""
    try:
        check_disposed(model, "Document Model")
        count = getattr(model, "CharacterCount", None)
        if count is not None:
            return max(0, int(count))
    except Exception:
        pass
    try:
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
        return len(normalize_linebreaks(safe_call(cursor.getString, "Cursor getString")))
    except UnoObjectError:
        logging.getLogger(__name__).exception("_writer_char_count failed")
        return 0


def _read_writer_text_slice(model, start_offset: int, length: int) -> str:
    """Read up to *length* characters from *start_offset* without loading the full document."""
    if length <= 0:
        return ""
    end_offset = start_offset + length
    cursor = get_text_cursor_at_range(model, start_offset, end_offset)
    if cursor is None:
        return ""
    # cursor.getString() concatenates tracked deletions as plain text; enumerate portions instead.
    return normalize_linebreaks(get_string_without_tracked_deletions(cursor))


def _char_offset_of_position(model, target_start, doc_len: int) -> int:
    """Character offset of a UNO text position from document start (no prefix getString())."""
    if doc_len <= 0:
        return 0
    try:
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        offset = 0
        while offset < doc_len:
            cmp = safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart"))
            if cmp == 0:
                return offset
            if cmp > 0:
                if offset == 0:
                    return 0
                safe_call(cursor.goLeft, "Cursor goLeft", 1, False)
                offset -= 1
                continue
            step = min(_GO_RIGHT_CHUNK, doc_len - offset)
            if step <= 0:
                return offset
            safe_call(cursor.goRight, "Cursor goRight", step, False)
            offset += step
            cmp_after = safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart"))
            if cmp_after >= 0:
                while offset > 0 and safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart")) > 0:
                    safe_call(cursor.goLeft, "Cursor goLeft", 1, False)
                    offset -= 1
                while safe_call(text.compareRegionStarts, "compareRegionStarts", target_start, safe_call(cursor.getStart, "Cursor getStart")) < 0 and offset < doc_len:
                    safe_call(cursor.goRight, "Cursor goRight", 1, False)
                    offset += 1
                return offset
        return doc_len
    except UnoObjectError:
        logging.getLogger(__name__).exception("_char_offset_of_position failed")
        return 0


def _get_writer_selection_positions(model):
    """Return (text, sel_start_pos, sel_end_pos) or None when selection unavailable."""
    try:
        check_disposed(model, "Document Model")
        controller = safe_call(model.getCurrentController, "Get current controller")
        sel = safe_call(controller.getSelection, "Get selection")
        sel_count = 0
        if sel and hasattr(sel, "getCount"):
            sel_count = safe_call(sel.getCount, "Get selection count")
        if not sel or sel_count == 0:
            vc = safe_call(controller.getViewCursor, "Get view cursor")
            rng = vc
        else:
            rng = safe_call(sel.getByIndex, "Get selection by index", 0)
        if not rng or not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            return None
        text = safe_call(model.getText, "Get document text")
        return text, safe_call(rng.getStart, "Get range start"), safe_call(rng.getEnd, "Get range end")
    except UnoObjectError:
        return None


def _writer_excerpt_overlaps_selection(model, excerpt_start: int, excerpt_end: int, sel_start_pos, sel_end_pos) -> bool:
    """True when selection UNO range overlaps [excerpt_start, excerpt_end) character window."""
    exc_cursor = get_text_cursor_at_range(model, excerpt_start, excerpt_end)
    if exc_cursor is None:
        return False
    text = safe_call(model.getText, "Get document text")
    exc_start = safe_call(exc_cursor.getStart, "Excerpt getStart")
    exc_end = safe_call(exc_cursor.getEnd, "Excerpt getEnd")
    if safe_call(text.compareRegionStarts, "compareRegionStarts sel_end exc_start", sel_end_pos, exc_start) > 0:
        return False
    if safe_call(text.compareRegionStarts, "compareRegionStarts exc_end sel_start", exc_end, sel_start_pos) > 0:
        return False
    return True


def _writer_selection_overlaps_windows(model, windows: list[tuple[int, int]], sel_start_pos, sel_end_pos) -> bool:
    for win_start, win_end in windows:
        if _writer_excerpt_overlaps_selection(model, win_start, win_end, sel_start_pos, sel_end_pos):
            return True
    return False


def get_document_length(model):
    """Return total character length of the document. Returns 0 on error."""
    try:
        check_disposed(model, "Document Model")
        if get_document_type(model) == DocumentType.WRITER:
            return _writer_char_count(model)
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
        length = len(normalize_linebreaks(safe_call(cursor.getString, "Cursor getString")))
        return length
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_document_length failed")
        return 0


def get_text_cursor_at_range(model, start_offset, end_offset):
    """Return a text cursor that selects the character range [start_offset, end_offset).
    The cursor is positioned at start and expanded to end so caller can setString('') and insert.
    goRight is used in chunks because UNO's goRight takes short (max 32767).
    Returns None on error or invalid range."""
    try:
        check_disposed(model, "Document Model")
        doc_len = get_document_length(model)
        start_offset = max(0, min(start_offset, doc_len))
        end_offset = max(0, min(end_offset, doc_len))
        if start_offset > end_offset:
            start_offset, end_offset = end_offset, start_offset
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        # Move to start_offset in chunks
        remaining = start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            safe_call(cursor.goRight, "Cursor goRight", n, False)
            remaining -= n
        # Expand selection by (end_offset - start_offset)
        remaining = end_offset - start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            safe_call(cursor.goRight, "Cursor goRight", n, True)
            remaining -= n
        return cursor
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_text_cursor_at_range failed")
        return None


def get_selection_range(model):
    """Return (start_offset, end_offset) character positions into the document.
    Cursor (no selection) = same start and end. Returns (0, 0) on error or no text range."""
    assert_main_thread("document_helpers.get_selection_range")
    try:
        check_disposed(model, "Document Model")
        sel_positions = _get_writer_selection_positions(model)
        if sel_positions is None:
            return (0, 0)
        _text, sel_start_pos, sel_end_pos = sel_positions
        doc_len = _writer_char_count(model)
        start_offset = _char_offset_of_position(model, sel_start_pos, doc_len)
        end_offset = _char_offset_of_position(model, sel_end_pos, doc_len)
        return (start_offset, end_offset)
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_selection_range failed")
        return (0, 0)


def get_document_context_for_chat(model, max_context=CHAT_DOCUMENT_CONTEXT_MAX_CHARS, include_end=True, include_selection=True, ctx=None):
    """Build a single context string for chat. Handles Writer and Calc.
    ctx: component context (required for Calc and Draw documents)."""
    assert_main_thread("document_helpers.get_document_context_for_chat")
    doc_type = get_document_type(model)

    if doc_type == DocumentType.CALC:
        return get_calc_context_for_chat(model, max_context, ctx)

    if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
        return get_draw_context_for_chat(model, max_context, ctx)

    # Writer: read only the excerpt slice(s), not the full document text.
    if doc_type == DocumentType.WRITER:
        try:
            check_disposed(model, "Document Model")
            doc_len = _writer_char_count(model)
        except UnoObjectError:
            logging.getLogger(__name__).exception("get_document_context_for_chat Writer failed")
            return "[Unable to read Writer document context. The document may be locked or initializing.]"

        if include_end and doc_len > (max_context // 2):
            start_chars = max_context // 2
            end_chars = max_context - start_chars
            excerpt_windows = [(0, start_chars), (doc_len - end_chars, doc_len)]
        else:
            start_chars = 0
            end_chars = 0
            take = min(doc_len, max_context)
            excerpt_windows = [(0, take)]

        start_offset, end_offset = (0, 0)
        if include_selection:
            sel_positions = _get_writer_selection_positions(model)
            if sel_positions is not None and _writer_selection_overlaps_windows(model, excerpt_windows, sel_positions[1], sel_positions[2]):
                start_offset, end_offset = get_selection_range(model)
                start_offset = max(0, min(start_offset, doc_len))
                end_offset = max(0, min(end_offset, doc_len))
                if start_offset > end_offset:
                    start_offset, end_offset = end_offset, start_offset
                max_selection_span = 2000
                if end_offset - start_offset > max_selection_span:
                    end_offset = start_offset + max_selection_span

        if include_end and doc_len > (max_context // 2):
            start_excerpt = _read_writer_text_slice(model, 0, start_chars)
            end_excerpt = _read_writer_text_slice(model, doc_len - end_chars, end_chars)
            start_excerpt = _inject_markers_into_excerpt(start_excerpt, 0, start_chars, start_offset, end_offset, "[DOCUMENT START]\n", "\n[DOCUMENT END]")
            end_excerpt = _inject_markers_into_excerpt(end_excerpt, doc_len - end_chars, doc_len, start_offset, end_offset, "[DOCUMENT END]\n", "\n[END DOCUMENT]")
            middle_note = "\n\n[... middle of document omitted ...]\n\n" if doc_len > max_context else ""
            return "Document length: %d characters.\n\n%s%s%s" % (doc_len, start_excerpt, middle_note, end_excerpt)

        take = min(doc_len, max_context)
        excerpt = _read_writer_text_slice(model, 0, take)
        if doc_len > max_context:
            excerpt += "\n\n[... document truncated ...]"
        excerpt = _inject_markers_into_excerpt(excerpt, 0, take, start_offset, end_offset, "[DOCUMENT START]\n", "\n[END DOCUMENT]")
        return "Document length: %d characters.\n\n%s" % (doc_len, excerpt)

    return ""


def get_calc_context_for_chat(model, max_context=8000, ctx=None):
    """Get context summary for a Calc spreadsheet."""
    assert_main_thread("document_helpers.get_calc_context_for_chat")
    if ctx is None:
        raise ValueError("ctx is required for get_calc_context_for_chat")
    try:
        check_disposed(model, "Document Model")
        bridge = CalcBridge(model)
        analyzer = SheetAnalyzer(bridge)
        summary = analyzer.get_sheet_summary()

        ctx_str = f"Spreadsheet Document: {model.getURL() or 'Untitled'}\n"
        sheets = model.getSheets()
        sheet_names = list(sheets.getElementNames())
        ctx_str += f"Sheets: {sheet_names}\n"
        ctx_str += f"Active Sheet: {summary['sheet_name']}\n"
        ctx_str += f"Used Range: {summary['used_range']} ({summary['row_count']} rows x {summary['col_count']} columns)\n"
        ctx_str += f"Columns: {', '.join([str(h) for h in summary['headers'] if h])}\n"

        # Add selection context if available
        controller = safe_call(model.getCurrentController, "Get current controller")
        selection = safe_call(controller.getSelection, "Get selection")
        if selection:
            if hasattr(selection, "getRangeAddress"):
                addr = safe_call(selection.getRangeAddress, "Get range address")
                from plugin.calc.address_utils import index_to_column

                sel_range = f"{index_to_column(addr.StartColumn)}{addr.StartRow + 1}:{index_to_column(addr.EndColumn)}{addr.EndRow + 1}"
                ctx_str += f"Current Selection: {sel_range}\n"

                # Check for selected values if small
                if (addr.EndRow - addr.StartRow + 1) * (addr.EndColumn - addr.StartColumn + 1) < 100:
                    from plugin.calc.inspector import CellInspector

                    inspector = CellInspector(bridge)
                    cells = inspector.read_range(sel_range)
                    ctx_str += "Selection Content (CSV-like):\n"
                    for row in cells:
                        ctx_str += ", ".join([str(c["value"]) if c["value"] is not None else "" for c in row]) + "\n"

        return ctx_str
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_calc_context_for_chat error")
        return "[Unable to read Calc spreadsheet context. The document may be locked or initializing.]"
    except Exception:
        logging.getLogger(__name__).exception("get_calc_context_for_chat exception")
        return "[Unable to read Calc spreadsheet context. The document may be locked or initializing.]"


def get_draw_context_for_chat(model, max_context=8000, ctx=None):
    """Get context summary for a Draw/Impress document. ctx: component context (unused, kept for signature compat)."""
    assert_main_thread("document_helpers.get_draw_context_for_chat")
    try:
        check_disposed(model, "Document Model")
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(model)
        pages = bridge.get_pages()
        active_page = bridge.get_active_page()

        is_impress = safe_call(model.supportsService, "Check supportsService", "com.sun.star.presentation.PresentationDocument")
        doc_type = "Impress Presentation" if is_impress else "Draw Document"

        ctx_str = "%s: %s\n" % (doc_type, safe_call(model.getURL, "Get document URL") or "Untitled")
        ctx_str += "Total %s: %d\n" % ("Slides" if is_impress else "Pages", safe_call(pages.getCount, "Get page count"))

        # Get index of active page
        active_page_idx = -1
        for i in range(safe_call(pages.getCount, "Get page count")):
            if safe_call(pages.getByIndex, "Get page by index", i) == active_page:
                active_page_idx = i
                break

        ctx_str += "Active %s Index: %d\n" % ("Slide" if is_impress else "Page", active_page_idx)

        # Summarize shapes on active page
        if active_page:
            shapes = bridge.get_shapes(active_page)
            ctx_str += "\nShapes on %s %d:\n" % ("Slide" if is_impress else "Page", active_page_idx)
            for i, s in enumerate(shapes):
                type_name = safe_call(s.getShapeType, "Get shape type").split(".")[-1]
                pos = safe_call(s.getPosition, "Get position")
                size = safe_call(s.getSize, "Get size")
                ctx_str += "- [%d] %s: pos(%d, %d) size(%dx%d)" % (i, type_name, pos.X, pos.Y, size.Width, size.Height)
                if hasattr(s, "getString"):
                    text = normalize_linebreaks(safe_call(s.getString, "Get string"))
                    if text:
                        ctx_str += ' text: "%s"' % text[:200]
                ctx_str += "\n"

            # Impress-specific: Speaker Notes
            if is_impress and hasattr(active_page, "getNotesPage"):
                try:
                    notes_page = safe_call(active_page.getNotesPage, "Get notes page")
                    notes_text = ""
                    for i in range(safe_call(notes_page.getCount, "Get notes page count")):
                        shape = safe_call(notes_page.getByIndex, "Get notes shape by index", i)
                        if safe_call(shape.getShapeType, "Get notes shape type") == "com.sun.star.presentation.NotesShape":
                            notes_text += safe_call(shape.getString, "Get notes shape string") + "\n"
                    if notes_text.strip():
                        ctx_str += "\nSpeaker Notes:\n%s\n" % notes_text.strip()
                except UnoObjectError:
                    pass

        return ctx_str
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_draw_context_for_chat error")
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"
    except Exception:
        logging.getLogger(__name__).exception("get_draw_context_for_chat exception")
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"


def _inject_markers_into_excerpt(excerpt_text, excerpt_start, excerpt_end, sel_start, sel_end, prefix, suffix):
    # ...
    """Inject [SELECTION_START] and [SELECTION_END] at character positions relative to excerpt.
    excerpt_start/excerpt_end are the document character range this excerpt covers.
    sel_start/sel_end are the selection/cursor range in document coordinates."""
    if sel_start >= excerpt_end or sel_end <= excerpt_start:
        # Selection does not overlap this excerpt (or both markers in same position outside)
        return prefix + excerpt_text + suffix
    # Map to excerpt-relative indices
    local_start = max(0, sel_start - excerpt_start)
    local_end = min(len(excerpt_text), sel_end - excerpt_start)
    # Build result with markers inserted (order: text before start, START, text between, END, text after)
    before = excerpt_text[:local_start]
    between = excerpt_text[local_start:local_end]
    after = excerpt_text[local_end:]
    out = prefix + before + "[SELECTION_START]" + between + "[SELECTION_END]" + after + suffix
    return out


# ---------------------------------------------------------------------------
# Navigation & Outline (Ported from extension)
# ---------------------------------------------------------------------------

import uuid


def get_paragraph_ranges(model):
    """Return list of top-level paragraph elements."""
    text = model.getText()
    enum = text.createEnumeration()
    ranges = []
    while enum.hasMoreElements():
        ranges.append(enum.nextElement())
    return ranges


def find_paragraph_for_range(match_range, para_ranges, text_obj=None):
    """Return the 0-based paragraph index that contains match_range."""
    try:
        if text_obj is None:
            text_obj = safe_call(match_range.getText, "Get text object")
        match_start = safe_call(match_range.getStart, "Get match start")
        low = 0
        high = len(para_ranges) - 1

        while low <= high:
            mid = (low + high) // 2
            para = para_ranges[mid]
            # compareRegionStarts: -1 if first is after second, 1 if before, 0 if equal
            cmp_start = safe_call(text_obj.compareRegionStarts, "compareRegionStarts start", match_start, safe_call(para.getStart, "Get para start"))
            if cmp_start > 0:
                high = mid - 1
            else:
                cmp_end = safe_call(text_obj.compareRegionStarts, "compareRegionStarts end", match_start, safe_call(para.getEnd, "Get para end"))
                if cmp_end < 0:
                    low = mid + 1
                else:
                    return mid
    except UnoObjectError:
        logging.getLogger(__name__).exception("find_paragraph_for_range error")
    return 0


def build_heading_tree(model) -> HeadingTreeNode:
    """Build a hierarchical heading tree. Single pass enumeration."""
    assert_main_thread("document_helpers.build_heading_tree")
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        enum = safe_call(text.createEnumeration, "Create enumeration")
        root: HeadingTreeNode = {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}
        stack: list[HeadingTreeNode] = [root]
        para_index = 0

        while safe_call(enum.hasMoreElements, "Check more elements"):
            element = safe_call(enum.nextElement, "Get next element")
            if safe_call(element.supportsService, "Check supportsService Paragraph", "com.sun.star.text.Paragraph"):
                outline_level = 0
                try:
                    outline_level = safe_call(element.getPropertyValue, "Get OutlineLevel", "OutlineLevel")
                except UnoObjectError as e:
                    logging.getLogger(__name__).debug("build_heading_tree could not get OutlineLevel: %s", e)

                if isinstance(outline_level, int) and outline_level > 0:
                    while len(stack) > 1 and int(stack[-1]["level"]) >= outline_level:
                        stack.pop()
                    node: HeadingTreeNode = {"level": outline_level, "text": safe_call(element.getString, "Get paragraph string"), "para_index": para_index, "children": [], "body_paragraphs": 0}
                    stack[-1]["children"].append(node)
                    stack.append(node)
                else:
                    stack[-1]["body_paragraphs"] += 1
            elif safe_call(element.supportsService, "Check supportsService TextTable", "com.sun.star.text.TextTable"):
                stack[-1]["body_paragraphs"] += 1
            para_index += 1
        return root
    except UnoObjectError:
        logging.getLogger(__name__).exception("build_heading_tree error")
        return {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}


def ensure_heading_bookmarks(model):
    """Ensure every heading has an _mcp_ bookmark. Returns {para_index: bookmark_name}."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        para_ranges = get_paragraph_ranges(model)

        # 1. Map existing _mcp_ bookmarks
        existing_map = {}
        if hasattr(model, "getBookmarks"):
            bookmarks = safe_call(model.getBookmarks, "Get bookmarks")
            for name in safe_call(bookmarks.getElementNames, "Get element names"):
                if name.startswith("_mcp_"):
                    bm = safe_call(bookmarks.getByName, "Get bookmark by name", name)
                    idx = find_paragraph_for_range(safe_call(bm.getAnchor, "Get bookmark anchor"), para_ranges, text)
                    existing_map[idx] = name

        # 2. Scanthe document for headings
        enum = safe_call(text.createEnumeration, "Create enumeration")
        para_index = 0
        bookmark_map = {}
        needs_bookmark = []

        while safe_call(enum.hasMoreElements, "Check more elements"):
            element = safe_call(enum.nextElement, "Get next element")
            if safe_call(element.supportsService, "Check supportsService Paragraph", "com.sun.star.text.Paragraph"):
                try:
                    if safe_call(element.getPropertyValue, "Get OutlineLevel", "OutlineLevel") > 0:
                        if para_index in existing_map:
                            bookmark_map[para_index] = existing_map[para_index]
                        else:
                            needs_bookmark.append((para_index, safe_call(element.getStart, "Get element start")))
                except UnoObjectError as e:
                    logging.getLogger(__name__).debug("ensure_heading_bookmarks could not get OutlineLevel: %s", e)
            para_index += 1

        # 3. Add missing bookmarks
        for idx, start_range in needs_bookmark:
            name = f"_mcp_{uuid.uuid4().hex[:8]}"
            bookmark = safe_call(model.createInstance, "Create bookmark instance", "com.sun.star.text.Bookmark")
            bookmark.Name = name
            cursor = safe_call(text.createTextCursorByRange, "Create cursor by range", start_range)
            safe_call(text.insertTextContent, "Insert text content", cursor, bookmark, False)
            bookmark_map[idx] = name

        return bookmark_map
    except UnoObjectError:
        logging.getLogger(__name__).exception("ensure_heading_bookmarks error")
        return {}


def resolve_locator(model, locator: str):
    """Resolve a locator string to a paragraph index or other document position."""
    loc_type, sep, loc_value = locator.partition(":")
    if not sep:
        return {"para_index": 0}

    if loc_type == "paragraph":
        return {"para_index": int(loc_value)}

    if loc_type == "heading":
        parts = []
        try:
            parts = [int(p) for p in loc_value.split(".")]
        except Exception:
            logging.getLogger(__name__).exception("resolve_locator heading parse error")
            return {"para_index": 0}

        tree = build_heading_tree(model)
        node: HeadingTreeNode = tree
        for part in parts:
            children = node["children"]
            if 1 <= part <= len(children):
                node = children[part - 1]
            else:
                break
        return {"para_index": node["para_index"]}

    if loc_type == "bookmark":
        if hasattr(model, "getBookmarks"):
            bms = model.getBookmarks()
            if bms.hasByName(loc_value):
                anchor = bms.getByName(loc_value).getAnchor()
                para_ranges = get_paragraph_ranges(model)
                return {"para_index": find_paragraph_for_range(anchor, para_ranges, model.getText())}

    return {"para_index": 0}


from plugin.framework.service import ServiceBase
from plugin.framework.uno_context import get_ctx


class DocumentService(ServiceBase):
    name = "document"

    def initialize(self, ctx):
        pass

    def get_active_document(self):
        return get_active_doc()

    def resolve_document_by_url(self, url):
        """Resolve (doc, doc_type) by document URL; (None, None) if not found. Main-thread only."""
        return resolve_document_by_url(get_ctx(), url)

    def detect_doc_type(self, doc):
        doc_type = get_document_type(doc)
        if doc_type == DocumentType.CALC:
            return "calc"
        if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
            return "draw"
        return "writer"

    def is_writer(self, doc):
        return is_writer(doc)

    def is_calc(self, doc):
        return is_calc(doc)

    def is_draw(self, doc):
        return is_draw(doc)

    def get_full_text(self, doc, max_chars=8000):
        return get_full_document_text(doc, max_chars)

    def get_document_length(self, doc):
        return get_document_length(doc)

    def get_document_context_for_chat(self, doc, max_context=CHAT_DOCUMENT_CONTEXT_MAX_CHARS, include_end=True, include_selection=True):
        return get_document_context_for_chat(doc, max_context, include_end, include_selection, get_ctx())

    def get_page_for_paragraph(self, model, para_index):
        """Return page number for a paragraph by index.

        Uses lockControllers + cursor save/restore to prevent visible viewport jumping.
        """
        try:
            check_disposed(model, "Document Model")
            text = safe_call(model.getText, "Get document text")
            controller = safe_call(model.getCurrentController, "Get current controller")
            vc = safe_call(controller.getViewCursor, "Get view cursor")
            saved = safe_call(text.createTextCursorByRange, "Create text cursor by range", safe_call(vc.getStart, "Get view cursor start"))
            safe_call(model.lockControllers, "Lock controllers")
            try:
                cursor = safe_call(text.createTextCursor, "Create text cursor")
                safe_call(cursor.gotoStart, "Cursor gotoStart", False)
                for _ in range(para_index):
                    if not safe_call(cursor.gotoNextParagraph, "Cursor gotoNextParagraph", False):
                        break
                safe_call(vc.gotoRange, "View cursor gotoRange", cursor, False)
                page = safe_call(vc.getPage, "Get page")
            finally:
                safe_call(vc.gotoRange, "Restore view cursor", saved, False)
                safe_call(model.unlockControllers, "Unlock controllers")
            return page
        except UnoObjectError:
            logging.getLogger(__name__).exception("get_page_for_paragraph error")
            return 1

    def get_page_count(self, model):
        """Return page count of a Writer document."""
        try:
            check_disposed(model, "Document Model")
            text = safe_call(model.getText, "Get document text")
            controller = safe_call(model.getCurrentController, "Get current controller")
            vc = safe_call(controller.getViewCursor, "Get view cursor")
            saved = safe_call(text.createTextCursorByRange, "Create text cursor by range", safe_call(vc.getStart, "Get view cursor start"))
            safe_call(model.lockControllers, "Lock controllers")
            try:
                safe_call(vc.jumpToLastPage, "Jump to last page")
                count = safe_call(vc.getPage, "Get page")
            finally:
                safe_call(vc.gotoRange, "Restore view cursor", saved, False)
                safe_call(model.unlockControllers, "Unlock controllers")
            return count
        except UnoObjectError:
            logging.getLogger(__name__).exception("get_page_count error")
            return 0

    def doc_key(self, doc):
        """Return a stable key for the document for use in caches."""
        return id(doc)

    def get_paragraph_ranges(self, doc):
        """Return list of top-level paragraph elements."""
        return get_paragraph_ranges(doc)

    def find_paragraph_for_range(self, anchor, para_ranges, text_obj=None):
        """Return the 0-based paragraph index that contains anchor."""
        return find_paragraph_for_range(anchor, para_ranges, text_obj)

    def resolve_locator(self, doc, locator):
        """Resolve a locator string to a paragraph index or other document position."""
        return resolve_locator(doc, locator)

    def yield_to_gui(self):
        """Yield to the UI event loop (no-op here)."""
        pass

    def annotate_pages(self, children, doc):
        """Annotate tree children with page numbers (no-op here)."""
        pass

    def find_paragraph_element(self, doc, para_index):
        """Return (paragraph_element, None) for the given index, or (None, None) if out of range."""
        ranges = get_paragraph_ranges(doc)
        if 0 <= para_index < len(ranges):
            return (ranges[para_index], None)
        return (None, None)
