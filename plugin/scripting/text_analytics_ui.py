"""Minimal modeless dialog for text analytics (a few buttons).

Powered exclusively by high-quality spaCy + textdescriptives (multilingual).
All heavy work runs in the user venv via the trusted worker.

The dialog extracts text on the host and ships it to the warm worker via the client.
"""

from __future__ import annotations

import logging
from typing import Any

import unohelper
from com.sun.star.awt import XActionListener, XTopWindowListener

from plugin.chatbot.dialogs import load_writeragent_dialog, msgbox
from plugin.doc.document_helpers import get_string_without_tracked_deletions, is_writer
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_active_document
from plugin.scripting.client import run_text_analytics
from plugin.writer.format import insert_content_at_position

log = logging.getLogger(__name__)


class TextAnalyticsDialog:
    """Tiny floating modeless tool with a few direct high-quality buttons.

    Buttons:
      - Readability (doc) / (sel)   → textdescriptives readability + stats
      - Entities                    → spaCy NER (multilingual)
      - Key Phrases                 → noun chunks
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

        dlg.getControl("BtnReadDoc").addActionListener(_Btn(lambda d: owner._compute(d, "readability", "whole")))
        dlg.getControl("BtnReadSel").addActionListener(_Btn(lambda d: owner._compute(d, "readability", "selection")))
        dlg.getControl("BtnStats").addActionListener(_Btn(lambda d: owner._compute(d, "entities", "whole")))  # repurposed button for Entities for minimal UI

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

        doc = get_active_document(ctx)
        if not doc or not is_writer(doc):
            res_ctrl.getModel().Text = _("Open a Writer document to analyze.")
            return

        raw = self._get_text(doc, scope)
        if not raw or len(raw.strip()) < 20:
            res_ctrl.getModel().Text = _("Not enough text in the chosen scope.")
            return

        res_ctrl.getModel().Text = _("Analyzing with spaCy...")

        try:
            # This goes to the venv worker. Model loading happens there.
            result = run_text_analytics(
                ctx,
                spec={"helper": helper},
                text=raw,
                context={},
            )
        except Exception as e:
            log.exception("Text analytics worker call failed")
            res_ctrl.getModel().Text = _("Error: %s\n\n(Make sure spaCy + textdescriptives and a model are installed in your Python venv.)") % e
            return

        self._last_result = result
        # Present the interesting data directly in the dialog.
        res_ctrl.getModel().Text = self._format_result_for_display(helper, result)

    def _format_result_for_display(self, helper: str, result: dict[str, Any]) -> str:
        if not result or result.get("status") != "ok":
            return str(result)

        data = result.get("result", {}) or {}
        lines: list[str] = []

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
            # Show a compact sample
            for e in ents[:30]:
                lines.append(f"  {e.get('text')} [{e.get('label')}]")
            if len(ents) > 30:
                lines.append(f"  ... and {len(ents)-30} more")
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
        html = self._result_to_html_table(data)
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

    def _result_to_html_table(self, data: dict[str, Any]) -> str:
        """Turn the structured result into a simple bordered table (or two)."""
        rows: list[str] = []

        # Readability + stats if present
        rd = data.get("readability") or {}
        ds = data.get("descriptive_stats") or {}
        for k, v in {**rd, **ds}.items():
            if isinstance(v, (int, float, str)):
                val = f"{v:.3f}" if isinstance(v, float) else str(v)
                rows.append(f"<tr><td>{k}</td><td>{val}</td></tr>")

        # Entities (compact)
        ents = data.get("entities") or []
        if ents:
            labels = {}
            for e in ents:
                labels[e.get("label", "?")] = labels.get(e.get("label", "?"), 0) + 1
            for lab, cnt in sorted(labels.items(), key=lambda x: -x[1])[:12]:
                rows.append(f"<tr><td>entity:{lab}</td><td>{cnt}</td></tr>")

        # Key phrases (top few)
        kps = data.get("key_phrases") or []
        if kps:
            top = ", ".join(kp.get("lemma") or kp.get("text") for kp in kps[:8])
            rows.append(f"<tr><td>key_phrases</td><td>{top}</td></tr>")

        if not rows:
            return ""
        return '<table border="1" style="border-collapse:collapse"><tbody>' + "".join(rows) + "</tbody></table>"
