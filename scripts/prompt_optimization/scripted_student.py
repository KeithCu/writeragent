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
    "<td>Critical: Stabilizes 24V battery voltage (which swings 20V-29V) to a clean 24V PoE for the Ubiquiti.</td></tr>"
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
    "<li><strong>Senior Developer</strong>, TechStart Inc (2023-present) — Microservices, CI/CD, on-call. Scaled to 100K users and 100M requests per month.</li>"
    "</ul>"
    "<h2>EDUCATION</h2>"
    "<p>State University, BS Computer Science, 2016, GPA 3.8</p>"
    "<h2>SKILLS</h2>"
    "<p>Python, Java, SQL, Docker, Kubernetes; certifications: AWS, Kubernetes</p>"
)

_TABLE_ENGINEERING = (
    "<table><thead><tr><th>Item</th><th>Price</th><th>Quantity</th></tr></thead><tbody>"
    '<tr><td>Apple</td><td align="right">1.20</td><td align="right">12</td></tr>'
    '<tr><td>Banana</td><td align="right">0.50</td><td align="right">24</td></tr>'
    '<tr><td>Orange</td><td align="right">0.80</td><td align="right">0</td></tr>'
    '<tr><td>Grape</td><td align="right">2.00</td><td align="right">8</td></tr>'
    '<tr><td>Mango</td><td align="right">1.50</td><td align="right">6</td></tr>'
    '<tr><td>Kiwi</td><td align="right">1.75</td><td align="right">0</td></tr>'
    '<tr><td>Total</td><td align="right">51.40</td><td align="right">50</td></tr>'
    "</tbody></table>"
)

_BULK_CLEANUP = (
    "<p>This sentence has extra spaces. So does this one.</p>"
    "<p>Another paragraph here, with spaces before commas. Fix all double spaces and ensure one space after sentences.</p>"
    '<p>https://example.com/test with URL. "Quoted text" should stay intact.</p>'
    "<p>Too many line breaks above. Normalize to single paragraph breaks. Also fix this one with trailing period.</p>"
    "<p>CMD: git  log  --oneline</p>"
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
    "<ul>"
    "<li>Pack the crate.</li>"
    "<li>Ship to Oslo.</li>"
    "<li>Call the depot.</li>"
    "<li>Label the pallet.</li>"
    "<li>Sweep the bay.</li>"
    "<li>File the docket.</li>"
    "<li>Seal the hatch.</li>"
    "</ul>"
    "<p>Note: do not bullet this line</p>"
)

_STYLE_CONSISTENCY = (
    '<p data-lo-style="Quotations">Default style paragraph one.</p>'
    "<h1>HEADING 2 text that should be upgraded.</h1>"
    '<p data-lo-style="Quotations">Another default paragraph.</p>'
    "<h1>Heading 2 again.</h1>"
)

_SMART_SUMMARIZATION = (
    "<h1>Findings</h1>"
    "<p>The system achieved 99.9% uptime. Latency averaged 45ms under load. "
    "Error rate was 0.01%. Scaling tests confirmed linear performance to 10k RPS. "
    "Cost per query dropped 40% after optimization. "
    "A canary deploy failed with a 12% error spike and was rolled back. "
    "The intern joked that p95 latency was 9001ms.</p>"
    "<h1>Executive Summary</h1>"
    "<ul>"
    "<li>99.9% uptime.</li>"
    "<li>45ms average latency under load.</li>"
    "<li>0.01% error rate.</li>"
    "<li>Linear scaling to 10k RPS.</li>"
    "<li>40% cost reduction after optimization.</li>"
    "</ul>"
)

_SECTION_REFACTOR = (
    "<h1>Introduction</h1>"
    "<p>Background info here.</p>"
    "<h1>Goal</h1>"
    "<p>Final thoughts and call to action.</p>"
    "<h1>Body</h1>"
    "<p>Main content goes here. See the Goal for next steps.</p>"
)

_COMMENT_MANAGEMENT = (
    "<p>The results are uncertain [Review this before finalizing] at this point "
    "in the analysis.</p>"
    "<p>Further testing is recommended before deployment.</p>"
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
    "style_consistency": [_apply_html(_STYLE_CONSISTENCY), _stop()],
    "smart_summarization": [_apply_html(_SMART_SUMMARIZATION), _stop()],
    "section_refactor": [_apply_html(_SECTION_REFACTOR), _stop()],
    "comment_management": [
        _tools(
            _tc(
                "add_comment",
                {
                    "search": "uncertain",
                    "content": "Review this before finalizing",
                },
                "cmt_1",
            )
        ),
        _stop(),
    ],
    "flowchart_gen": [
        _tools(
            _tc(
                "delegate_to_specialized_draw_toolset",
                {
                    "domain": "shapes",
                    "task": (
                        "Create a login flowchart: Start oval, Process for user login, "
                        "Decision credentials valid?, Yes to End, No back to Process."
                    ),
                },
                "del_draw_1",
            )
        ),
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
        _tools(
            _tc("shape_connect", {"start": 0, "end": 1}, "conn_1"),
            _tc("shape_connect", {"start": 1, "end": 2}, "conn_2"),
            _tc("shape_connect", {"start": 2, "end": 3, "label": "Yes"}, "conn_3"),
            _tc("shape_connect", {"start": 2, "end": 1, "label": "No"}, "conn_4"),
        ),
        _tools(
            _tc(
                "specialized_workflow_finished",
                {"answer": "flowchart created"},
                "fin_draw_1",
            )
        ),
        _tools(_tc("get_draw_tree", {}, "tree_1")),
        _stop(),
    ],
    "org_chart_gen": [
        _tools(
            _tc(
                "delegate_to_specialized_draw_toolset",
                {
                    "domain": "shapes",
                    "task": (
                        "Create org chart: Ava CEO; Ben CTO, Cara CFO, Dan COO; "
                        "Eli Fay under Ben; Gus Hal under Cara; Ivy Jay under Dan; "
                        "connect each manager to reports; verify with get_draw_tree."
                    ),
                },
                "del_org_1",
            )
        ),
        _tools(
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Ava (CEO)",
                    "x": 8000,
                    "y": 500,
                    "width": 4000,
                    "height": 1500,
                },
                "org_ava",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Ben (CTO)",
                    "x": 2000,
                    "y": 3000,
                    "width": 3500,
                    "height": 1500,
                },
                "org_ben",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Cara (CFO)",
                    "x": 8000,
                    "y": 3000,
                    "width": 3500,
                    "height": 1500,
                },
                "org_cara",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Dan (COO)",
                    "x": 14000,
                    "y": 3000,
                    "width": 3500,
                    "height": 1500,
                },
                "org_dan",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Eli (Engineer)",
                    "x": 500,
                    "y": 5500,
                    "width": 3000,
                    "height": 1400,
                },
                "org_eli",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Fay (Engineer)",
                    "x": 3700,
                    "y": 5500,
                    "width": 3000,
                    "height": 1400,
                },
                "org_fay",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Gus (Analyst)",
                    "x": 6500,
                    "y": 5500,
                    "width": 3000,
                    "height": 1400,
                },
                "org_gus",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Hal (Analyst)",
                    "x": 9700,
                    "y": 5500,
                    "width": 3000,
                    "height": 1400,
                },
                "org_hal",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Ivy (Lead)",
                    "x": 12500,
                    "y": 5500,
                    "width": 3000,
                    "height": 1400,
                },
                "org_ivy",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Jay (Lead)",
                    "x": 15700,
                    "y": 5500,
                    "width": 3000,
                    "height": 1400,
                },
                "org_jay",
            ),
        ),
        _tools(
            # Ava(0)→Ben(1), Cara(2), Dan(3)
            _tc("shape_connect", {"start": 0, "end": 1}, "org_c1"),
            _tc("shape_connect", {"start": 0, "end": 2}, "org_c2"),
            _tc("shape_connect", {"start": 0, "end": 3}, "org_c3"),
            # Ben→Eli(4), Fay(5)
            _tc("shape_connect", {"start": 1, "end": 4}, "org_c4"),
            _tc("shape_connect", {"start": 1, "end": 5}, "org_c5"),
            # Cara→Gus(6), Hal(7)
            _tc("shape_connect", {"start": 2, "end": 6}, "org_c6"),
            _tc("shape_connect", {"start": 2, "end": 7}, "org_c7"),
            # Dan→Ivy(8), Jay(9)
            _tc("shape_connect", {"start": 3, "end": 8}, "org_c8"),
            _tc("shape_connect", {"start": 3, "end": 9}, "org_c9"),
        ),
        _tools(
            _tc(
                "specialized_workflow_finished",
                {"answer": "org chart created"},
                "fin_org_1",
            )
        ),
        _tools(_tc("get_draw_tree", {}, "org_tree_1")),
        _stop(),
    ],
    "solar_sld_gen": [
        _tools(
            _tc(
                "delegate_to_specialized_draw_toolset",
                {
                    "domain": "shapes",
                    "task": (
                        "Partial-home SLD: Utility→Meter→Main panel; "
                        "Meter→Solar AC Disconnect→System controller (MID)→"
                        "Backup subpanel; PV+microinverters→Combiner→controller; "
                        "AC battery→controller; verify get_draw_tree."
                    ),
                },
                "del_sld_1",
            )
        ),
        _tools(
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Utility / Grid",
                    "x": 500,
                    "y": 3500,
                    "width": 3200,
                    "height": 1400,
                },
                "sld_util",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Meter",
                    "x": 4200,
                    "y": 3500,
                    "width": 2800,
                    "height": 1400,
                },
                "sld_meter",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Main panel\n(non-backup loads)",
                    "x": 4200,
                    "y": 6000,
                    "width": 3600,
                    "height": 1600,
                },
                "sld_main",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Solar AC Disconnect",
                    "x": 7600,
                    "y": 3500,
                    "width": 4000,
                    "height": 1400,
                },
                "sld_acd",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "System controller (MID)",
                    "x": 12200,
                    "y": 3300,
                    "width": 4200,
                    "height": 1800,
                },
                "sld_mid",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Backup subpanel\n(backup loads)",
                    "x": 17000,
                    "y": 3400,
                    "width": 3800,
                    "height": 1600,
                },
                "sld_backup",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "PV array\n(modules)",
                    "x": 7600,
                    "y": 500,
                    "width": 3200,
                    "height": 1400,
                },
                "sld_pv",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Microinverters",
                    "x": 11200,
                    "y": 500,
                    "width": 3200,
                    "height": 1400,
                },
                "sld_micro",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "Combiner",
                    "x": 14800,
                    "y": 500,
                    "width": 3000,
                    "height": 1400,
                },
                "sld_comb",
            ),
            _tc(
                "shape_upsert",
                {
                    "action": "create",
                    "shape_type": "rectangle",
                    "text": "AC battery",
                    "x": 12200,
                    "y": 6000,
                    "width": 3600,
                    "height": 1400,
                },
                "sld_batt",
            ),
        ),
        _tools(
            # 0 Utility, 1 Meter, 2 Main, 3 Solar AC Disconnect, 4 MID,
            # 5 Backup, 6 PV, 7 Microinverters, 8 Combiner, 9 AC battery
            _tc("shape_connect", {"start": 0, "end": 1}, "sld_c1"),
            _tc("shape_connect", {"start": 1, "end": 2}, "sld_c2"),
            _tc("shape_connect", {"start": 1, "end": 3}, "sld_c3"),
            _tc("shape_connect", {"start": 3, "end": 4}, "sld_c4"),
            _tc("shape_connect", {"start": 4, "end": 5}, "sld_c5"),
            _tc("shape_connect", {"start": 6, "end": 7}, "sld_c6"),
            _tc("shape_connect", {"start": 7, "end": 8}, "sld_c7"),
            _tc("shape_connect", {"start": 8, "end": 4}, "sld_c8"),
            _tc("shape_connect", {"start": 9, "end": 4}, "sld_c9"),
        ),
        _tools(
            _tc(
                "specialized_workflow_finished",
                {"answer": "solar SLD created"},
                "fin_sld_1",
            )
        ),
        _tools(_tc("get_draw_tree", {}, "sld_tree_1")),
        _stop(),
    ],
    "data_sorting": [
        _tools(
            _tc(
                "delegate_to_specialized_calc_toolset",
                {
                    "domain": "ranges",
                    "task": (
                        "Sort by Product ascending then Revenue descending; "
                        "leave non-numeric Revenue last."
                    ),
                },
                "del_calc_1",
            )
        ),
        _tools(
            _tc(
                "sort_range",
                {
                    "range": ["A1:B6"],
                    "sort_column": 0,
                    "ascending": True,
                    "has_header": True,
                },
                "sort_prod",
            )
        ),
        _tools(
            _tc(
                "sort_range",
                {
                    "range": ["A1:B6"],
                    "sort_column": 1,
                    "ascending": False,
                    "has_header": True,
                },
                "sort_rev",
            )
        ),
        _tools(
            _tc(
                "specialized_workflow_finished",
                {"answer": "sorted"},
                "fin_calc_1",
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
                {
                    "range": ["C2:C5"],
                    "values": '["=B2*0.08","=B3*0.08","=B4*0.08","=B5*0.08"]',
                },
                "tax_vals",
            ),
            _tc(
                "write_formula_range",
                {"range": ["C6:C7"], "values": '["", ""]'},
                "tax_skip",
            ),
        ),
        _tools(_tc("get_sheet_summary", {}, "sum_2")),
        _stop(),
    ],
}

_PY_FORMULA = '=PY("result = data.to_pandas().drop_duplicates()"; A1:H500)'
_PY_WRITE = _tools(
    _tc(
        "write_formula_range",
        {"range": ["J1"], "values": _PY_FORMULA},
        "py_1",
    )
)
SCRIPTS["py_refuse_overlap"] = [
    _PY_WRITE,
    _stop("H1 is inside A1:H500; wrote =PY at J1 instead"),
]
SCRIPTS["py_no_bulk_read"] = [_PY_WRITE, _stop("wrote =PY at J1 without reading the block")]


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
