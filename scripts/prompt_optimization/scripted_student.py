# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Deterministic eval student: replay fixed tool-call rounds (no LLM, no API key).

Each SCRIPTS[task_id] is a list of rounds in the same shape ``request_with_tools``
returns. A round with tool_calls is executed; a content-only round stops the loop.
Same JSON must replay on ``--backend string`` and ``--backend lo``.
"""
from __future__ import annotations

import json
from typing import Any


def _tc(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _tools(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"content": "", "tool_calls": list(calls), "usage": {}}


def _stop(text: str = "done") -> dict[str, Any]:
    return {"content": text, "tool_calls": None, "usage": {}}


def _apply_html(html: str, call_id: str = "apply_1") -> dict[str, Any]:
    return _tools(
        _tc(
            "apply_document_content",
            {"target": "full_document", "content": html},
            call_id,
        )
    )


# Compact HTML: no inter-tag whitespace so bulk_cleanup reject "  " stays clean
# after string apply, and LO export can be compacted the same way.
_TABLE_FROM_MESS = (
    "<table><thead><tr><th>Item</th><th>Description</th><th>Price</th><th>Notes</th></tr></thead>"
    "<tbody>"
    "<tr><td>Battery</td><td>Battle Born BB5024H (24V 50Ah Heated)</td><td>$999.00</td>"
    "<td>The heart of the system. 10-year warranty.</td></tr>"
    "<tr><td>Controller</td><td>Victron SmartSolar MPPT 100/30</td><td>$135.15</td>"
    "<td>Handles the 440W panel easily at 24V.</td></tr>"
    "<tr><td>USB Charger</td><td>Blue Sea Systems 1045 (4.8A)</td><td>$43.00</td>"
    "<td>Industrial grade. Accepts 24V input directly.</td></tr>"
    "<tr><td>PoE Converter</td><td>Tycon TP-DCDC-1224G-4P</td><td>$66.00</td>"
    "<td>Critical: Stabilizes 24V battery voltage to a clean 24V PoE.</td></tr>"
    "<tr><td>Enclosure</td><td>Saginaw SCE-202010ELJ</td><td>$215.31</td>"
    "<td>20x20x10 NEMA 4 steel box.</td></tr>"
    "<tr><td>Total</td><td></td><td>$1458.46</td><td></td></tr>"
    "</tbody></table>"
)

_REFORMAT_RESUME = (
    "<h1>John Doe</h1>"
    "<p>john@example.com | 555-1234</p>"
    "<p>Dedicated developer focused on Python APIs and leadership of small teams.</p>"
    "<h2>WORK HISTORY</h2>"
    "<ul>"
    "<li><strong>Developer</strong>, Acme Corp (2020-2023) — Built APIs and fixed bugs; led 2 junior devs.</li>"
    "<li><strong>Senior Developer</strong>, TechStart Inc (2023-present) — Microservices, CI/CD, on-call.</li>"
    "</ul>"
    "<h2>EDUCATION</h2>"
    "<p>State University, BS Computer Science, 2016, GPA 3.8</p>"
    "<h2>SKILLS</h2>"
    "<p>Python, Java, SQL, Docker, Kubernetes; certifications: AWS, Kubernetes</p>"
)

_TABLE_ENGINEERING = (
    "<table><thead><tr><th>Item</th><th>Price</th><th>Quantity</th></tr></thead><tbody>"
    "<tr><td>Apple</td><td>1.20</td><td>12</td></tr>"
    "<tr><td>Banana</td><td>0.50</td><td>24</td></tr>"
    "<tr><td>Orange</td><td>0.80</td><td></td></tr>"
    "<tr><td>Grape</td><td>2.00</td><td>8</td></tr>"
    "<tr><td>Mango</td><td>1.50</td><td>6 [note]</td></tr>"
    "<tr><td>Kiwi</td><td>1.75</td><td></td></tr>"
    "<tr><td>Total</td><td>7.75</td><td>50</td></tr>"
    "</tbody></table>"
)

_BULK_CLEANUP = (
    "<p>This sentence has extra spaces. So does this one.</p>"
    "<p>Another paragraph here, with spaces before commas. Fix all double spaces and ensure one space after sentences.</p>"
    '<p>https://example.com/test with URL. "Quoted text" should stay intact.</p>'
    "<p>Too many line breaks above. Normalize to single paragraph breaks. Also fix this one with trailing period.</p>"
)

_LOGICAL_REWRITING = (
    "<p>WriterAgent 2.0 adds a Dual-Mode judge using G-Eval and Prometheus for "
    "structural versus creative scoring, plus updated OpenRouter model support.</p>"
)

_FORMAT_PRESERVATION = (
    "<p>Jane Smith - Project Lead</p>"
    "<p>Contact person: John Doe (legacy ID JD-001). Do not change this legal name on this line.</p>"
)

_STYLE_APPLICATION = (
    "<p>Project Overview (draft)</p>"
    "<h1>Introduction</h1>"
    "<p>This section explains the scope. Do not promote Background or Summary to the same heading level.</p>"
    "<p>Background</p>"
    "<p>Earlier work used a monolith.</p>"
    "<p>Summary</p>"
    "<p>We will refactor in phases.</p>"
)

_BULLET_CONSISTENCY = (
    "<p>- First thing.</p>"
    "<p>- Second thing.</p>"
    "<p>- Third thing.</p>"
    "<p>- Fourth thing.</p>"
    "<p>- Fifth item.</p>"
    "<p>- Sixth with extra space.</p>"
    "<p>- Seventh (mixed).</p>"
)


SCRIPTS: dict[str, list[dict[str, Any]]] = {
    "table_from_mess": [_apply_html(_TABLE_FROM_MESS), _stop()],
    "reformat_resume": [_apply_html(_REFORMAT_RESUME), _stop()],
    "table_engineering": [_apply_html(_TABLE_ENGINEERING), _stop()],
    "bulk_cleanup": [_apply_html(_BULK_CLEANUP), _stop()],
    "logical_rewriting": [_apply_html(_LOGICAL_REWRITING), _stop()],
    "format_preservation": [_apply_html(_FORMAT_PRESERVATION), _stop()],
    "style_application": [_apply_html(_STYLE_APPLICATION), _stop()],
    "bullet_consistency": [_apply_html(_BULLET_CONSISTENCY), _stop()],
    "flowchart_gen": [
        _tools(
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "ellipse",
                    "text": "Start",
                    "x": 1000,
                    "y": 500,
                    "width": 3000,
                    "height": 1500,
                },
                "shape_1",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "flowchart-process",
                    "text": "Process: user login",
                    "x": 1000,
                    "y": 2500,
                    "width": 4000,
                    "height": 2000,
                },
                "shape_2",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "flowchart-decision",
                    "text": "Decision: credentials valid?",
                    "x": 1000,
                    "y": 5000,
                    "width": 4000,
                    "height": 2000,
                },
                "shape_3",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "flowchart-terminator",
                    "text": "End",
                    "x": 1000,
                    "y": 7500,
                    "width": 3000,
                    "height": 1500,
                },
                "shape_4",
            ),
        ),
        _tools(_tc("get_draw_tree", {}, "tree_1")),
        _stop(),
    ],
    "data_sorting": [
        _tools(
            _tc(
                "sort_range",
                {
                    "range": ["A1:B5"],
                    "sort_column": 1,
                    "ascending": False,
                    "has_header": True,
                },
                "sort_1",
            )
        ),
        _stop(),
    ],
    "tax_column": [
        _tools(_tc("get_sheet_summary", {}, "sum_1")),
        _tools(
            _tc(
                "write_formula_range",
                {"range": ["C1"], "values": "Tax"},
                "tax_hdr",
            ),
            _tc(
                "write_formula_range",
                {"range": ["C2:C5"], "values": "[0.8, 0.4, 0.64, 1.0]"},
                "tax_vals",
            ),
        ),
        _tools(_tc("get_sheet_summary", {}, "sum_2")),
        _stop(),
    ],
}


class ScriptedStudent:
    """Play the next scripted round; stop when a content-only round is returned."""

    def __init__(self, task_id: str) -> None:
        if task_id not in SCRIPTS:
            raise KeyError(f"No scripted student for task_id={task_id!r}")
        self.task_id = task_id
        self._rounds = list(SCRIPTS[task_id])
        self._i = 0

    def request_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        unused = (messages, tools, kwargs)
        del unused
        if self._i >= len(self._rounds):
            return _stop("")
        rnd = self._rounds[self._i]
        self._i += 1
        return {
            "content": rnd.get("content") or "",
            "tool_calls": rnd.get("tool_calls"),
            "usage": rnd.get("usage") or {},
        }
