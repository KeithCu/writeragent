# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal modeless dialog for text analytics (a few buttons).

The dialog extracts text on the UI thread, waits in run_in_background(..., dedicated=True),
and applies with post_to_main_thread. Closing the dialog does not kill the shared worker.
Topics and sentiment honor Whole Document: selection is one string, whole document uses sections.
Check Venv does not read the document. The install command comes from the worker payload.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

import unohelper
from com.sun.star.awt import XActionListener, XTopWindowListener

if TYPE_CHECKING:
    from com.sun.star.awt import ActionEvent
    from com.sun.star.lang import EventObject

from plugin.chatbot.dialogs import get_checkbox_state, load_writeragent_dialog, msgbox, set_control_enabled
from plugin.doc.doc_type import is_writer
from plugin.framework.errors import DocumentDisposedError
from plugin.framework.i18n import _
from plugin.framework.queue_executor import execute_on_main_thread, post_to_main_thread
from plugin.framework.uno_context import get_active_document, get_runtime_uid
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.client import run_text_analytics
from plugin.scripting.text_analytics import (
    format_text_analytics_result_for_display,
    get_doc_language,
    insert_text_analytics_result_into_doc,
    resolve_text_analytics_document_inputs,
)

log = logging.getLogger(__name__)

_COMPUTE_BUTTONS: tuple[tuple[str, str, bool], ...] = (
    ("BtnRead", "readability", True),
    ("BtnEntities", "entities", True),
    ("BtnPhrases", "key_phrases", True),
    ("BtnTopics", "topics", True),
    ("BtnSentiment", "sentiment", True),
    ("BtnCheck", "diagnostics", False),
)

_BUSY_CONTROL_IDS: tuple[str, ...] = (
    "BtnRead",
    "BtnEntities",
    "BtnPhrases",
    "BtnTopics",
    "BtnSentiment",
    "BtnCheck",
    "BtnInsert",
)

_MISSING_IMPORT_MARKERS = ("No module named", "ModuleNotFoundError", "ImportError")


def _exception_suggests_missing_package(error: BaseException) -> bool:
    text = str(error)
    return any(marker in text for marker in _MISSING_IMPORT_MARKERS)


class TextAnalyticsDialog:
    """Tiny floating modeless tool with a few direct high-quality buttons.

    Buttons:
      - Readability (doc) / (sel)   → textdescriptives readability + stats
      - Entities                    → spaCy NER (multilingual)
      - Key Phrases                 → noun chunks
      - Topics                      → NMF topic model (uses sections for structure)
      - Sentiment                   → multilingual transformers sentiment by section (whole doc)
      - Check Venv                  → reports status of spacy, textdescriptives, transformers, torch, scikit-learn
      - Insert report here          → appends a clean table after the caret/selection
    """

    _ctx: Any
    _closed: bool
    _busy: bool
    _dlg: Any | None
    _top_listener: Any | None
    _last_result: dict[str, Any] | None
    _source_uid: str | None

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg = None
        self._closed = False
        self._busy = False
        self._top_listener = None
        self._last_result = None
        self._source_uid = None
        self._open()

    @classmethod
    def show(cls, ctx: Any) -> None:
        cls(ctx)

    def close(self, *, toolkit_teardown: bool = False) -> None:
        """Hide or dispose the dialog.

        Esc / title-bar close on a closeable modeless dialog already destroys
        the window. ``windowClosing`` must not ``dispose()`` again (native
        crash, no Python traceback). The Close button disposes once.
        """
        if self._closed:
            return
        self._closed = True
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        if toolkit_teardown:
            try:
                dlg.setVisible(False)
            except Exception:
                log.debug("text analytics: hide after windowClosing failed", exc_info=True)
            return
        try:
            dlg.setVisible(False)
        except Exception:
            log.debug("text analytics: hide before dispose failed", exc_info=True)
        try:
            dlg.dispose()
        except Exception:
            log.debug("text analytics: dispose failed", exc_info=True)

    def _open(self) -> None:
        ctx = self._ctx
        try:
            dlg = load_writeragent_dialog("TextAnalyticsDialog", ctx)
            if dlg is None:
                msgbox(ctx, _("Text Analytics"), _("Could not load the analytics dialog."))
                self.close()
                return
            self._dlg = dlg

            self._wire(dlg)

            owner = self

            class _TopWindowListener(unohelper.Base, XTopWindowListener):
                def windowClosing(self, e: Any) -> None:
                    owner.close(toolkit_teardown=True)

                def windowClosed(self, e: Any) -> None: pass
                def windowOpened(self, e: Any) -> None: pass
                def windowMinimized(self, e: Any) -> None: pass
                def windowNormalized(self, e: Any) -> None: pass
                def windowActivated(self, e: Any) -> None: pass
                def windowDeactivated(self, e: Any) -> None: pass
                def disposing(self, Source: EventObject) -> None: pass

            self._top_listener = _TopWindowListener()
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)

        except Exception:
            log.exception("TextAnalyticsDialog._open failed")
            self.close()

    def _wire(self, dlg: Any) -> None:
        owner = self

        class _Btn(unohelper.Base, XActionListener):
            _fn: Callable[[Any], Any]

            def __init__(self, fn: Callable[[Any], Any]) -> None:
                self._fn = fn

            def actionPerformed(self, rEvent: ActionEvent) -> None:
                try:
                    self._fn(dlg)
                except Exception:
                    log.exception("Text analytics button failed")

            def disposing(self, Source: EventObject) -> None:
                pass

        for btn_id, helper, uses_scope in _COMPUTE_BUTTONS:
            ctrl = dlg.getControl(btn_id)
            if ctrl is not None:
                h_name = helper

                def _cb(d: Any, h: str = h_name, scoped: bool = uses_scope) -> None:
                    scope = owner._scope_from_dialog(d) if scoped else "whole"
                    owner._compute(d, h, scope)

                ctrl.addActionListener(_Btn(_cb))

        btn_insert = dlg.getControl("BtnInsert")
        if btn_insert is not None:
            btn_insert.addActionListener(_Btn(lambda d: owner._insert_report(d)))
        btn_close = dlg.getControl("BtnClose")
        if btn_close is not None:
            btn_close.addActionListener(_Btn(lambda d: owner.close()))

    def _scope_from_dialog(self, dlg: Any) -> str:
        try:
            chk = dlg.getControl("ChkScope")
            if chk is None:
                return "whole"
            # getattr(chk, "State", 0) only saw a forwarded model attribute, and a missing attribute was unchecked.
            # get_checkbox_state uses getState() then model.State, which is what the other dialogs use.
            # getControl failure still means whole document.
            return "whole" if get_checkbox_state(chk) == 1 else "selection"
        except Exception:
            log.debug("text analytics: scope checkbox failed", exc_info=True)
            return "whole"

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if self._closed or self._dlg is None:
            return
        for cid in _BUSY_CONTROL_IDS:
            try:
                ctrl = self._dlg.getControl(cid)
            except Exception:
                log.debug("text analytics: set busy control %s failed", cid, exc_info=True)
                # return stopped the loop at the first getControl failure, so a later re-enable left every control after that id disabled. One missing or throwing id must not freeze the others.
                continue
            if ctrl is not None:
                set_control_enabled(ctrl, not busy)

    def _compute(self, dlg: Any, helper: str, scope: str) -> None:
        if self._busy or self._closed:
            return

        res_ctrl = dlg.getControl("ResultsEdit")
        if res_ctrl is None:
            return

        # Bugfix: stale insert bug
        # What was wrong: _last_result persisted across failed or invalid runs, allowing an old report
        # to be inserted after a failed analysis attempt.
        # How it happened: _last_result was only overwritten on success and never reset at compute start.
        # Why this change: clear _last_result before validation and background execution.
        self._last_result = None
        self._source_uid = None

        ctx = self._ctx
        doc = get_active_document(ctx)

        if helper in ("diagnostics", "check"):
            text: str | list[str] | None = None
            doc_lang = get_doc_language(doc) if doc else None
            context: dict[str, Any] | None = {"lang": doc_lang} if doc_lang else None
        else:
            if not doc or not is_writer(doc):
                res_ctrl.getModel().Text = _("Open a Writer document to analyze.")
                return

            try:
                raw, context = resolve_text_analytics_document_inputs(doc, helper, scope=scope)
            except DocumentDisposedError:
                res_ctrl.getModel().Text = _("The document was closed.")
                return

            if scope == "selection" and (not raw or (isinstance(raw, str) and not raw.strip())):
                res_ctrl.getModel().Text = _("Nothing is selected.")
                return

            if (
                not raw
                or (isinstance(raw, str) and len(raw.strip()) < 20)
                or (isinstance(raw, list) and len(raw) == 0)
            ):
                res_ctrl.getModel().Text = _("Not enough text in the chosen scope.")
                return

            text = raw
            self._source_uid = get_runtime_uid(doc) or None

        if helper in ("diagnostics", "check"):
            res_ctrl.getModel().Text = _("Checking spaCy / textdescriptives / transformers installation...")
        else:
            res_ctrl.getModel().Text = _("Analyzing...")

        self._set_busy(True)

        owner = self

        # Bugfix: worker blocking UI thread
        # What was wrong: worker RPC executed synchronously on the main VCL thread, freezing the UI.
        # How it happened: run_text_analytics was called directly inside _compute without thread dispatch.
        # Why this change: run the RPC in a dedicated background thread, keeping the UI responsive,
        # and post results back to the main thread via post_to_main_thread.
        def _worker() -> None:
            res: dict[str, Any] | None = None
            err: Exception | None = None
            try:
                res = run_text_analytics(
                    ctx,
                    spec={"helper": helper},
                    text=text,
                    context=context,
                )
            except Exception as e:
                log.exception("Text analytics worker call failed")
                err = e

            owner._deliver_compute_result(helper, res, err)

        try:
            run_in_background(_worker, name="text-analytics-dialog", dedicated=True)
        except Exception as e:
            log.exception("Failed to schedule text analytics background worker")
            self._apply_compute_result(helper, None, e)

    def _deliver_compute_result(
        self,
        helper: str,
        result: dict[str, Any] | None,
        error: Exception | None,
    ) -> None:
        def _post() -> None:
            self._apply_compute_result(helper, result, error)

        # a raised post_to_main_thread used to return after a log, so _busy stayed true and the compute buttons stayed disabled until the dialog was closed. post raises TimeoutError when AsyncCallback is missing and the pending list is full, and _flush_pending_posts can raise before this closure is queued. execute_on_main_thread is the retry that still runs _apply_compute_result on a live main thread. The flag clear is only for when that marshal also refuses, and it does not touch UNO.
        try:
            post_to_main_thread(_post)
            return
        except Exception:
            log.exception("Failed to post text analytics compute result to main thread")

        try:
            execute_on_main_thread(_post, timeout=5.0)
        except Exception:
            log.exception("text analytics: could not deliver result on the main thread")
            self._busy = False

    def _apply_compute_result(
        self,
        helper: str,
        result: dict[str, Any] | None,
        error: Exception | None,
    ) -> None:
        self._busy = False
        if self._closed or self._dlg is None:
            return
        self._set_busy(False)

        res_ctrl = self._dlg.getControl("ResultsEdit")
        if res_ctrl is None:
            return

        if error is not None:
            self._last_result = None
            self._source_uid = None
            if helper in ("diagnostics", "check"):
                text = _("Error from the Text Analytics worker:\n{err}").format(err=str(error))
            else:
                # every non-diagnostics exception, including Python worker failed: timed out after …, used this package paragraph. The paragraph stays for messages that are failed imports. Other failures show only Error: plus the exception text.
                if _exception_suggests_missing_package(error):
                    text = _(
                        "Error: %s\n\nRequired packages may be missing in your configured venv (Settings → Python). "
                        "Click Check Venv in this dialog or use the full Test in Settings → Python for details on "
                        "spacy, textdescriptives, transformers, torch etc."
                    ) % error
                else:
                    text = _("Error: %s") % error
            try:
                res_ctrl.getModel().Text = text
            except Exception:
                log.exception("Failed to set error text on ResultsEdit")
            return

        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and helper not in ("diagnostics", "check")
        ):
            self._last_result = result
        else:
            self._last_result = None
            self._source_uid = None

        try:
            display_text = format_text_analytics_result_for_display(helper, result)
        except Exception:
            log.exception("Text analytics: format failed")
            display_text = "Error: Text analytics failed."

        try:
            res_ctrl.getModel().Text = display_text
        except Exception:
            log.exception("Failed to set display text on ResultsEdit")

    def _insert_report(self, dlg: Any) -> None:
        if self._busy or self._closed:
            return
        ctx = self._ctx
        doc = get_active_document(ctx)
        if not doc or not is_writer(doc):
            msgbox(ctx, _("Text Analytics"), _("No Writer document to insert into."))
            return

        if not self._last_result:
            msgbox(ctx, _("Text Analytics"), _("Run an analysis first."))
            return

        source = self._source_uid or ""
        active = get_runtime_uid(doc) or ""
        # Insert used whatever document was active at click time, so a report computed from document A was written into document B. The uid is taken on the UI thread before the worker starts. Both ids must be present before a mismatch refuses; a missing id keeps the previous insert.
        if source and active and source != active:
            msgbox(ctx, _("Text Analytics"), _("Switch back to the document that was analyzed, or run the analysis again."))
            return

        try:
            insert_text_analytics_result_into_doc(ctx, doc, self._last_result)
        except Exception as e:
            log.exception("Insert analytics report failed")
            msgbox(ctx, _("Text Analytics"), _("Insert failed: %s") % e)
            return

        res_ctrl = dlg.getControl("ResultsEdit")
        if res_ctrl is not None:
            res_ctrl.getModel().Text = (res_ctrl.getModel().Text or "") + "\n\n" + _("Inserted after selection/caret.")
