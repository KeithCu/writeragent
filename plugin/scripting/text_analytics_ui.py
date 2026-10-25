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

from collections import Counter
import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.uno_listeners import BaseActionListener

if TYPE_CHECKING:
    from com.sun.star.awt import ActionEvent

from plugin.chatbot.dialogs import get_checkbox_state, load_writeragent_dialog, msgbox, set_control_enabled
from plugin.doc.doc_type import is_writer
from plugin.framework.errors import DocumentDisposedError
from plugin.framework.i18n import _
from plugin.framework.queue_executor import execute_on_main_thread, post_to_main_thread
from plugin.framework.uno_context import get_active_document, get_runtime_uid
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.client import run_text_analytics
from plugin.scripting.python_runner_ui import (
    ModelessWindowClosingListener,
    close_modeless_dialog,
)
from plugin.scripting.text_analytics import (
    get_doc_language,
    insert_text_analytics_result_into_doc,
    resolve_text_analytics_document_inputs,
    text_analytics_payload_has_content,
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
        close_modeless_dialog(dlg, toolkit_teardown=toolkit_teardown, log_label="text analytics")

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

            self._top_listener = ModelessWindowClosingListener(self)
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)

        except Exception:
            log.exception("TextAnalyticsDialog._open failed")
            self.close()

    def _wire(self, dlg: Any) -> None:
        owner = self

        class _Btn(BaseActionListener):
            _fn: Callable[[Any], Any]

            def __init__(self, fn: Callable[[Any], Any]) -> None:
                super().__init__()
                self._fn = fn

            def actionPerformed(self, rEvent: ActionEvent) -> None:
                try:
                    self._fn(dlg)
                except Exception:
                    log.exception("Text analytics button failed")

        for btn_id, helper, uses_scope in _COMPUTE_BUTTONS:
            ctrl = dlg.getControl(btn_id)
            if ctrl is not None:
                h_name = helper

                def _cb(d: Any, h: str = h_name, scoped: bool = uses_scope) -> None:
                    if scoped:
                        scope = owner._scope_from_dialog(d)
                        if scope is None:
                            return
                    else:
                        scope = "whole"
                    owner._compute(d, h, scope)

                ctrl.addActionListener(_Btn(_cb))

        btn_insert = dlg.getControl("BtnInsert")
        if btn_insert is not None:
            btn_insert.addActionListener(_Btn(lambda d: owner._insert_report(d)))
        btn_close = dlg.getControl("BtnClose")
        if btn_close is not None:
            btn_close.addActionListener(_Btn(lambda d: owner.close()))

    def _scope_from_dialog(self, dlg: Any) -> str | None:
        try:
            chk = dlg.getControl("ChkScope")
            if chk is None:
                return "whole"
            # getattr(chk, "State", 0) only saw a forwarded model attribute, and a missing attribute was unchecked.
            # get_checkbox_state uses getState() then model.State, which is what the other dialogs use.
            return "whole" if get_checkbox_state(chk) == 1 else "selection"
        except Exception:
            # Bugfix: a failed scope read analyzed the whole document
            # What was wrong: Readability, Entities, Key Phrases, Topics, and Sentiment
            # still ran on the whole document when the scope checkbox could not be read.
            # How it happened: the except logged at debug and returned "whole", and
            # _compute treated that as an explicit Whole Document choice.
            # Why this change: write the error into ResultsEdit, the same place as the
            # other analysis messages, and do not start a run.
            log.exception("text analytics: scope checkbox failed")
            self._report_scope_read_failure(dlg)
            return None

    def _report_scope_read_failure(self, dlg: Any) -> None:
        self._last_result = None
        self._source_uid = None
        message = _("Could not read the scope checkbox.")
        try:
            res = dlg.getControl("ResultsEdit")
            if res is not None:
                res.getModel().Text = message
                return
        except Exception:
            log.exception("text analytics: could not show scope error in ResultsEdit")
        msgbox(self._ctx, _("Text Analytics"), message)

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

        # a raised post_to_main_thread used to return after a log, so _busy stayed true and the compute buttons stayed disabled until the dialog was closed. post raises TimeoutError when AsyncCallback is missing and the pending list is full; a flush of older pending posts logs and leaves that tail queued, and it does not raise, so this closure is still enqueued when AsyncCallback exists. execute_on_main_thread is the retry that still runs _apply_compute_result on a live main thread. The flag clear is only for when that marshal also refuses, and it does not touch UNO.
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
            # A raised ResultsEdit write is already logged by process_queue or the button listener.
            res_ctrl.getModel().Text = text
            return

        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and helper not in ("diagnostics", "check")
            and text_analytics_payload_has_content(result)
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

        # A raised ResultsEdit write is already logged by process_queue or the button listener.
        res_ctrl.getModel().Text = display_text

    def _insert_report(self, dlg: Any) -> None:
        if self._busy or self._closed:
            return
        ctx = self._ctx
        doc = get_active_document(ctx)
        if not doc or not is_writer(doc):
            msgbox(ctx, _("Text Analytics"), _("No Writer document to insert into."))
            return

        if not self._last_result:
            msgbox(ctx, _("Text Analytics"), _("Nothing to insert. Run an analysis first."))
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


def _text_analytics_error_line(result: dict[str, Any] | None) -> str:
    """Format generic error line from result dict."""
    if isinstance(result, dict):
        msg = result.get("message")
        if isinstance(msg, str) and msg.strip():
            return f"Error: {msg.strip()}"
    return "Error: Text analytics failed."


def _format_metric_lines(mapping: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for k, v in mapping.items():
        if isinstance(v, bool):
            lines.append(f"  {k}: {v}")
        elif isinstance(v, float):
            lines.append(f"  {k}: {v:.2f}")
        else:
            lines.append(f"  {k}: {v}")
    return lines


def format_text_analytics_result_for_display(helper: str, result: dict[str, Any] | None) -> str:
    """Format a text analytics result dict for UI display in the analytics dialog."""
    if not isinstance(result, dict):
        return "No result."

    raw_data = result.get("result")
    data = raw_data if isinstance(raw_data, dict) else {}

    if helper in ("diagnostics", "check"):
        if result.get("status") == "error" and data.get("status") != "error":
            return _text_analytics_error_line(result)

        if data.get("status") == "error":
            install = data.get("text_analytics_install", "")
            py = data.get("python_used_by_worker", "unknown")
            msg = f"Diagnostics Error (worker python: {py}):\n{data.get('message')}"
            if install:
                msg += f"\n\nFull requirements for Text Analytics features:\n  {install}"
            return msg

        lines = ["Text Analytics Diagnostics (inside the actual worker python):"]
        py = data.get("python_used_by_worker", "unknown")
        lines.append(f"  Python executable used: {py}")
        lines.append(f"  spaCy version: {data.get('spacy_version', 'N/A')}")
        lines.append(f"  textdescriptives: {'Installed' if data.get('has_textdescriptives') else 'Missing'}")

        models = data.get("models") or []
        if models:
            lines.append(f"  Installed Models ({len(models)}):")
            for m in models:
                lines.append(f"    - {m}")
        else:
            lines.append("  No models detected/loaded.")

        lines.append(f"  transformers: {'Installed' if data.get('has_transformers') else 'Missing'}")
        if data.get("has_transformers"):
            lines.append(f"  transformers version: {data.get('transformers_version', 'N/A')}")
        lines.append(f"  torch: {'Installed' if data.get('has_torch') else 'Missing'}")
        if data.get("has_torch"):
            lines.append(f"  torch version: {data.get('torch_version', 'N/A')}")
        has_sklearn = bool(data.get("has_sklearn"))
        lines.append(f"  scikit-learn: {'Installed' if has_sklearn else 'Missing'}")
        if has_sklearn:
            lines.append(f"  scikit-learn version: {data.get('sklearn_version', 'N/A')}")

        install = data.get("text_analytics_install")
        if install:
            lines.append("\nPackages needed for everything this dialog supports")
            lines.append("(Readability with real scores, Entities, Key Phrases, Topics, Sentiment, etc.):")
            lines.append(f"  {install}")

        lines.append("\nInstall the packages listed above into the Python the worker is using.")
        lines.append("Click Check Venv again after installing.")

        return "\n".join(lines)

    if helper in ("readability", "entities", "key_phrases", "chunks"):
        if result.get("status") != "ok":
            return _text_analytics_error_line(result)

        if helper == "readability":
            lines = []
            rd = data.get("readability") or {}
            ds = data.get("descriptive_stats") or {}
            if rd:
                lines.append("Readability (via textdescriptives):")
                lines.extend(_format_metric_lines(rd))
            if ds:
                lines.append("\nDescriptive stats:")
                lines.extend(_format_metric_lines(ds))
            meta = data.get("meta") or {}
            if meta:
                lines.append(f"\nModel: {meta.get('model')}  Lang: {meta.get('lang')}")
            return "\n".join(lines) if lines else "No readability metrics returned."

        if helper == "entities":
            ents = data.get("entities") or []
            if not ents:
                return "No entities found."
            lines = [f"Entities ({len(ents)}):"]
            for e in ents[:30]:
                lines.append(f"  {e.get('text')} [{e.get('label')}]")
            if len(ents) > 30:
                lines.append(f"  ... and {len(ents) - 30} more")
            return "\n".join(lines)

        if helper in ("key_phrases", "chunks"):
            kps = data.get("key_phrases") or []
            if not kps:
                return "No key phrases found."
            lines = [f"Key Phrases ({len(kps)}):"]
            for kp in kps[:30]:
                lines.append(f"  {kp.get('text')} (lemma: {kp.get('lemma')})")
            if len(kps) > 30:
                lines.append(f"  ... and {len(kps) - 30} more")
            return "\n".join(lines)

    if helper == "topics":
        tops = data.get("topics") or []
        if result.get("status") == "ok" and tops:
            lines = [f"Topics ({len(tops)}):"]
            for t in tops:
                tid = t.get("id")
                terms = ", ".join(t.get("terms", [])[:6])
                lines.append(f"  Topic {tid}: {terms}")
            assigns = data.get("assignments") or []
            if assigns:
                lines.append(f"\nSection assignments ({len(assigns)}):")
                cnt = Counter(a.get("dominant_topic") for a in assigns)
                for tid, c in sorted(cnt.items()):
                    lines.append(f"  Topic {tid}: {c} section(s)")
            meta = data.get("meta") or {}
            if meta:
                lines.append(f"\nMeta: {meta}")
            return "\n".join(lines)

        # Bugfix: topics error formatting
        # What was wrong: topics MISSING_PACKAGE in venv returned a raw dict because of early status != "ok" return,
        # hiding the install hint and suggested install command.
        # How it happened: venv status was "error" for missing packages, which bypassed helper formatting.
        # Why this change: inspect payload error and hint fields even when status is not ok.
        err = data.get("error") or data.get("note")
        if err and "MISSING_PACKAGE" in str(err):
            parts = ["Topics: MISSING_PACKAGE"]
            hint = str(data.get("install_hint") or "").strip()
            install = str(data.get("install") or "").strip()
            if hint:
                parts.append(hint)
            if install:
                parts.append(f"Suggested install: {install}")
            return "\n\n".join(parts)
        if err:
            return f"Topics: {err}"
        if result.get("status") != "ok":
            return _text_analytics_error_line(result)
        return "No topics found."

    if helper == "sentiment":
        # Bugfix: sentiment error formatting
        # What was wrong: sentiment MISSING_PACKAGE and load failures returned a raw dict because of
        # early status != "ok" return, or displayed neutral (score: 0.0) if payload had default scores.
        # How it happened: status: "error" was not handled by sentiment formatter, and payload error
        # wasn't checked before score.
        # Why this change: inspect payload error first; format install hints, install commands, and details;
        # fall back to generic line when outer status is error without payload error.
        err = data.get("error")
        if result.get("status") != "ok" or err is not None:
            if err is None:
                return _text_analytics_error_line(result)
            err_str = str(err)
            if "MISSING_PACKAGE" in err_str:
                msg = "Sentiment: MISSING_PACKAGE (the transformers package or its dependencies could not be imported)"
            elif "MODEL_LOAD_FAILED" in err_str or "LOAD_FAILED" in err_str:
                msg = "Sentiment: model load failed (package is present, but loading the model/tokenizer failed)"
            else:
                msg = f"Sentiment error: {err}"
            hint = str(data.get("install_hint") or "").strip()
            if hint:
                msg += f"\n\n{hint}"
            install = str(data.get("install") or "").strip()
            if install:
                msg += f"\n\nSuggested install: {install}"
            detail = str(data.get("detail") or "").strip()
            if detail:
                msg += f"\n\nReal error details from the worker process:\n{detail}"
            return msg

        sent = data.get("sentiment") or {}
        if not sent or "score" not in sent:
            return "No sentiment data."
        lines = [f"Sentiment: {sent.get('label')} (score: {sent.get('score')})"]
        per = data.get("per_section") or []
        if per:
            lines.append(f"\nBy section ({len(per)}):")
            for p in per:
                idx = p.get("section_index")
                lines.append(f"  [{idx}] {p.get('label')} ({p.get('score')})")
        return "\n".join(lines)

    if result.get("status") != "ok":
        return _text_analytics_error_line(result)

    try:
        return json.dumps(data, indent=2, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return "Error: Text analytics failed."
