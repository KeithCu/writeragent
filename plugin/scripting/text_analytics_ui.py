"""Minimal modeless dialog for text analytics (a few buttons).

Powered exclusively by high-quality spaCy + textdescriptives (multilingual).
All heavy work runs in the user venv via the trusted worker.

The dialog extracts text on the host and ships it to the warm worker via the client.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import unohelper
from com.sun.star.awt import XActionListener, XTopWindowListener

from plugin.chatbot.dialogs import load_writeragent_dialog, msgbox
from plugin.doc.document_helpers import get_string_without_tracked_deletions, is_writer
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_active_document
from plugin.scripting.client import run_text_analytics
from plugin.scripting.text_analytics import get_doc_language, _result_to_html_table
from plugin.writer.format import insert_content_at_position

log = logging.getLogger(__name__)


class TextAnalyticsDialog:
    """Tiny floating modeless tool with a few direct high-quality buttons.

    Buttons:
      - Readability (doc) / (sel)   → textdescriptives readability + stats
      - Entities                    → spaCy NER (multilingual)
      - Key Phrases                 → noun chunks
      - Topics                      → NMF topic model (uses sections for structure)
      - Sentiment                   → multilingual transformers sentiment by section (whole doc)
      - Check Venv                    → reports status of spacy, textdescriptives, transformers (for new features)
      - Insert report here          → appends a clean table after the caret/selection
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._dlg: Any | None = None
        self._closed = False
        self._top_listener: Any | None = None
        self._last_result: dict[str, Any] | None = None
        self._open()

    @classmethod
    def show(cls, ctx: Any) -> None:
        cls(ctx)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._dlg is not None:
                self._dlg.dispose()
        except Exception:  # nosec B110 - best effort
            pass
        self._dlg = None

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
                def windowClosing(self, e):
                    owner.close()

                def windowClosed(self, e): pass
                def windowOpened(self, e): pass
                def windowMinimized(self, e): pass
                def windowNormalized(self, e): pass
                def windowActivated(self, e): pass
                def windowDeactivated(self, e): pass
                def disposing(self, Source): pass

            self._top_listener = _TopWindowListener()
            dlg.addTopWindowListener(self._top_listener)
            dlg.setVisible(True)

        except Exception:
            log.exception("TextAnalyticsDialog._open failed")
            self.close()

    def _wire(self, dlg: Any) -> None:
        owner = self

        class _Btn(unohelper.Base, XActionListener):
            def __init__(self, fn):
                self._fn = fn

            def actionPerformed(self, rEvent):
                try:
                    self._fn(dlg)
                except Exception:
                    log.exception("Text analytics button failed")

            def disposing(self, Source):
                pass

        dlg.getControl("BtnRead").addActionListener(_Btn(lambda d: owner._compute(d, "readability", "whole" if (d.getControl("ChkScope").State == 1) else "selection")))
        dlg.getControl("BtnEntities").addActionListener(_Btn(lambda d: owner._compute(d, "entities", "whole" if (d.getControl("ChkScope").State == 1) else "selection")))
        dlg.getControl("BtnPhrases").addActionListener(_Btn(lambda d: owner._compute(d, "key_phrases", "whole" if (d.getControl("ChkScope").State == 1) else "selection")))
        dlg.getControl("BtnTopics").addActionListener(_Btn(lambda d: owner._compute(d, "topics", "whole")))  # topics work best on whole-doc sections
        dlg.getControl("BtnSentiment").addActionListener(_Btn(lambda d: owner._compute(d, "sentiment", "whole")))  # sentiment works best on whole-doc sections
        dlg.getControl("BtnCheck").addActionListener(_Btn(lambda d: owner._compute(d, "diagnostics", "whole")))

        dlg.getControl("BtnInsert").addActionListener(_Btn(lambda d: owner._insert_report(d)))
        dlg.getControl("BtnClose").addActionListener(_Btn(lambda d: owner.close()))

    def _get_text(self, doc: Any, scope: str) -> str:
        try:
            if scope == "selection":
                controller = doc.getCurrentController()
                sel = controller.getSelection()
                if sel and hasattr(sel, "getCount") and sel.getCount() > 0:
                    rng = sel.getByIndex(0)
                    return get_string_without_tracked_deletions(rng) or ""
            text = doc.getText()
            return get_string_without_tracked_deletions(text) or ""
        except Exception:
            return ""

    def _compute(self, dlg: Any, helper: str, scope: str) -> None:
        ctx = self._ctx
        res_ctrl = dlg.getControl("ResultsEdit")
        if res_ctrl is None:
            return

        if helper in ("diagnostics", "check"):
            res_ctrl.getModel().Text = _("Checking spaCy / textdescriptives / transformers installation...")
            doc = get_active_document(ctx)
            doc_lang = get_doc_language(doc) if doc else None
            try:
                result = run_text_analytics(
                    ctx,
                    spec={"helper": helper},
                    context={"lang": doc_lang} if doc_lang else None
                )
            except Exception as e:
                log.exception("Text analytics worker call failed")
                # Show everything right here. No redirect to Settings.
                err_msg = str(e)
                install = (
                    "uv pip install spacy textdescriptives transformers torch "
                    "--index-url https://download.pytorch.org/whl/cpu && "
                    "python -m spacy download xx_sent_ud_sm"
                )
                res_ctrl.getModel().Text = _(
                    "Error from the Text Analytics worker:\n{err}\n\n"
                    "The features in this dialog require these packages in the python it is using:\n"
                    "  • spacy + a model          (entities, key phrases, basic stats)\n"
                    "  • textdescriptives         (real Readability scores)\n"
                    "  • transformers + torch     (Sentiment using the multilingual model)\n\n"
                    "Install them with:\n"
                    "  {install}\n\n"
                    "The worker reported the module as missing. Verify the packages are installed "
                    "in the exact python executable the worker resolved for your configured venv."
                ).format(err=err_msg, install=install)
                return

            self._last_result = result
            res_ctrl.getModel().Text = self._format_result_for_display(helper, result)
            return

        doc = get_active_document(ctx)
        if not doc or not is_writer(doc):
            res_ctrl.getModel().Text = _("Open a Writer document to analyze.")
            return

        raw: str | list[str] = self._get_text(doc, scope)
        # Topics and sentiment benefit enormously from section structure (heading + body groups).
        # Force whole-doc section extraction on the host before shipping the list to the trusted worker.
        if helper in ("topics", "sentiment"):
            try:
                from plugin.scripting.text_analytics import _get_writer_sections
                secs = _get_writer_sections(doc)
                if secs:
                    raw = secs
            except Exception:
                pass  # fall back to flat text

        if not raw or (isinstance(raw, str) and len(raw.strip()) < 20) or (isinstance(raw, list) and len(raw) == 0):
            res_ctrl.getModel().Text = _("Not enough text in the chosen scope.")
            return

        res_ctrl.getModel().Text = _("Analyzing...")

        try:
            lang = get_doc_language(doc)
            ctx_payload: dict[str, Any] = {}
            if lang:
                ctx_payload["lang"] = lang
                ctx_payload["doc_lang"] = lang
            result = run_text_analytics(
                ctx,
                spec={"helper": helper},
                text=raw,
                context=ctx_payload,
            )
        except Exception as e:
            log.exception("Text analytics worker call failed")
            res_ctrl.getModel().Text = _("Error: %s\n\nRequired packages may be missing in your configured venv (Settings → Python). Click Check Venv in this dialog or use the full Test in Settings → Python for details on spacy, textdescriptives, transformers, torch etc.") % e
            return

        self._last_result = result
        res_ctrl.getModel().Text = self._format_result_for_display(helper, result)

    def _format_result_for_display(self, helper: str, result: dict[str, Any]) -> str:
        if not result or result.get("status") != "ok":
            return str(result)

        data = result.get("result", {}) or {}
        lines: list[str] = []

        if helper in ("diagnostics", "check"):
            if data.get("status") == "error":
                install = data.get("text_analytics_install", "")
                py = data.get("python_used_by_worker", "unknown")
                msg = f"Diagnostics Error (worker python: {py}):\n{data.get('message')}"
                if install:
                    msg += f"\n\nFull requirements for Text Analytics features:\n  {install}"
                return msg
            
            lines.append("Text Analytics Diagnostics (inside the actual worker python):")
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
            
            # New multilingual Sentiment (transformers)
            lines.append(f"  transformers: {'Installed' if data.get('has_transformers') else 'Missing'}")
            if data.get('has_transformers'):
                lines.append(f"  transformers version: {data.get('transformers_version', 'N/A')}")
            lines.append(f"  torch: {'Installed' if data.get('has_torch') else 'Missing'}")
            if data.get('has_torch'):
                lines.append(f"  torch version: {data.get('torch_version', 'N/A')}")
            
            install = data.get("text_analytics_install")
            if install:
                lines.append("\nPackages needed for everything this dialog supports")
                lines.append("(Readability with real scores, Entities, Key Phrases, Topics, Sentiment, etc.):")
                lines.append(f"  {install}")
            
            # Self-contained instructions, no mention of other dialogs.
            lines.append("\nInstall the packages listed above into the Python the worker is using.")
            lines.append("Click Check Venv again after installing.")
            
            return "\n".join(lines)

        if helper == "readability":
            rd = data.get("readability") or {}
            ds = data.get("descriptive_stats") or {}
            if rd:
                lines.append("Readability (via textdescriptives):")
                for k, v in rd.items():
                    if isinstance(v, (int, float)):
                        lines.append(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")
                    else:
                        lines.append(f"  {k}: {v}")
            if ds:
                lines.append("\nDescriptive stats:")
                for k, v in ds.items():
                    lines.append(f"  {k}: {v}")
            meta = data.get("meta") or {}
            if meta:
                lines.append(f"\nModel: {meta.get('model')}  Lang: {meta.get('lang')}")
            return "\n".join(lines) if lines else "No readability metrics returned."

        if helper == "entities":
            ents = data.get("entities") or []
            if not ents:
                return "No entities found."
            lines.append(f"Entities ({len(ents)}):")
            for e in ents[:30]:
                lines.append(f"  {e.get('text')} [{e.get('label')}]")
            if len(ents) > 30:
                lines.append(f"  ... and {len(ents)-30} more")
            return "\n".join(lines)

        if helper in ("key_phrases", "chunks"):
            kps = data.get("key_phrases") or []
            if not kps:
                return "No key phrases found."
            lines.append(f"Key Phrases ({len(kps)}):")
            for kp in kps[:30]:
                lines.append(f"  {kp.get('text')} (lemma: {kp.get('lemma')})")
            if len(kps) > 30:
                lines.append(f"  ... and {len(kps)-30} more")
            return "\n".join(lines)

        if helper == "topics":
            tops = data.get("topics") or []
            if not tops:
                err = data.get("error") or data.get("note")
                if err:
                    return f"Topics: {err}\n\n(For topic modeling install scikit-learn from the analysis stack.)"
                return "No topics found."
            lines.append(f"Topics ({len(tops)}):")
            for t in tops:
                tid = t.get("id")
                terms = ", ".join(t.get("terms", [])[:6])
                lines.append(f"  Topic {tid}: {terms}")
            assigns = data.get("assignments") or []
            if assigns:
                lines.append(f"\nSection assignments ({len(assigns)}):")
                # Show a compact summary
                cnt = Counter(a.get("dominant_topic") for a in assigns)
                for tid, c in sorted(cnt.items()):
                    lines.append(f"  Topic {tid}: {c} section(s)")
            meta = data.get("meta") or {}
            if meta:
                lines.append(f"\nMeta: {meta}")
            return "\n".join(lines)

        if helper == "sentiment":
            sent = data.get("sentiment") or {}
            if not sent or "score" not in sent:
                err = data.get("error") or data.get("note")
                if err:
                    hint = data.get("install_hint", "")
                    install = data.get("install", "")
                    detail = data.get("detail", "")
                    msg = f"Sentiment error: {err}"
                    if "MISSING_PACKAGE" in str(err):
                        msg = "Sentiment: MISSING_PACKAGE (the transformers package or its dependencies could not be imported)"
                    elif "MODEL_LOAD_FAILED" in str(err) or "LOAD_FAILED" in str(err):
                        msg = "Sentiment: model load failed (package is present, but loading the model/tokenizer failed)"
                    if hint:
                        msg += f"\n\n{hint}"
                    if install:
                        msg += f"\n\nSuggested install: {install}"
                    if detail:
                        msg += f"\n\nReal error details from the worker process:\n{detail}"
                    return msg
                return "No sentiment data."
        if helper == "sentiment":
            sent = data.get("sentiment") or {}
            if not sent or "score" not in sent:
                err = data.get("error") or data.get("note")
                if err:
                    hint = data.get("install_hint", "")
                    install = data.get("install", "")
                    detail = data.get("detail", "")
                    msg = f"Sentiment error: {err}"
                    if "MISSING_PACKAGE" in str(err):
                        msg = "Sentiment: MISSING_PACKAGE (the transformers package or its dependencies could not be imported)"
                    elif "MODEL_LOAD_FAILED" in str(err) or "LOAD_FAILED" in str(err):
                        msg = "Sentiment: model load failed (package is present, but loading the model/tokenizer failed)"
                    if hint:
                        msg += f"\n\n{hint}"
                    if install:
                        msg += f"\n\nSuggested install: {install}"
                    if detail:
                        msg += f"\n\nReal error details from the worker process:\n{detail}"
                    return msg
                return "No sentiment data."
            lines.append(f"Sentiment: {sent.get('label')} (score: {sent.get('score')})")
            per = data.get("per_section") or []
            if per:
                lines.append(f"\nBy section ({len(per)}):")
                for p in per:
                    idx = p.get("section_index")
                    lines.append(f"  [{idx}] {p.get('label')} ({p.get('score')})")
            return "\n".join(lines)

        # Fallback pretty print
        import json
        return json.dumps(data, indent=2, ensure_ascii=False)[:2000]

    def _insert_report(self, dlg: Any) -> None:
        ctx = self._ctx
        doc = get_active_document(ctx)
        if not doc or not is_writer(doc):
            msgbox(ctx, _("Text Analytics"), _("No Writer document to insert into."))
            return

        if not self._last_result:
            msgbox(ctx, _("Text Analytics"), _("Run an analysis first."))
            return

        # Build a compact, useful HTML table from whatever we have.
        data = (self._last_result or {}).get("result") or {}
        html = _result_to_html_table(data)
        if not html:
            msgbox(ctx, _("Text Analytics"), _("Nothing useful to insert from the last result."))
            return

        html = "<h4>Text Analytics</h4>" + html

        # Position after selection/caret (non-destructive)
        controller = doc.getCurrentController()
        try:
            vc = controller.getViewCursor()
            sel = controller.getSelection()
            if sel and hasattr(sel, "getCount") and sel.getCount() > 0:
                end = sel.getByIndex(0).getEnd()
                vc.gotoRange(end, False)
            controller.select(vc)
        except Exception:
            pass  # nosec B110 - best effort cursor positioning, non-fatal

        try:
            insert_content_at_position(doc, ctx, html, "selection")
        except Exception as e:
            log.exception("Insert analytics report failed")
            msgbox(ctx, _("Text Analytics"), _("Insert failed: %s") % e)
            return

        res_ctrl = dlg.getControl("ResultsEdit")
        if res_ctrl is not None:
            res_ctrl.getModel().Text = (res_ctrl.getModel().Text or "") + "\n\n" + _("Inserted after selection/caret.")

    
