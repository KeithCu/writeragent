# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Soft notes for eval-1 task ``org_chart_gen``.

Hard pass/fail stays in ``oracles.oracle_org_chart_gen`` (names, ~8/10 boxes,
~7/9 linked connectors, manager→report edges). Exact 10-box / 9-connector
shortfalls are soft — same idea as under-counted stars on the flag row.
Does not invent a partial score; ``agent_score`` stays binary from hard fails.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from oracles import (  # noqa: E402
    _ORG_CHART_BOX_FLOOR,
    _ORG_CHART_CONN_FLOOR,
    _org_chart_connectors,
    _org_chart_labeled_boxes,
    check_oracle,
    parse_json_export,
)

ORG_CHART_TASK_ID = "org_chart_gen"
_STRING_FAILURE = (
    "org_chart_gen requires the headless LO backend; "
    "the string simulator does not export production connector nodes"
)
_WANT_BOXES = 10
_WANT_CONNECTORS = 9


@dataclass
class OrgChartScore:
    failures: list[str]
    soft: list[str] = field(default_factory=list)
    summary: str = ""
    passed: bool = False
    box_count: int = 0
    connector_count: int = 0


def _counts(doc: str) -> tuple[int, int]:
    data = parse_json_export(doc) or {}
    tree = data.get("tree")
    nodes: list[dict[str, Any]] = (
        [n for n in tree if isinstance(n, dict)] if isinstance(tree, list) else []
    )
    boxes = len(_org_chart_labeled_boxes(nodes))
    connectors = len(_org_chart_connectors(nodes))
    if connectors == 0:
        raw = data.get("connections")
        if isinstance(raw, list):
            connectors = len(raw)
    return boxes, connectors


def score_org_chart_example(
    final_document: str,
    *,
    backend: str,
) -> OrgChartScore:
    """Hard oracle + soft exact-count notes. String backend fails closed."""
    if backend != "lo":
        return OrgChartScore(
            failures=[_STRING_FAILURE],
            summary=_STRING_FAILURE,
        )
    failures = check_oracle(ORG_CHART_TASK_ID, final_document or "")
    boxes, connectors = _counts(final_document or "")
    soft: list[str] = []
    if boxes != _WANT_BOXES and boxes >= _ORG_CHART_BOX_FLOOR:
        soft.append(
            f"labeled boxes {boxes} != {_WANT_BOXES} "
            f"(soft: >= {_ORG_CHART_BOX_FLOOR} still passes)"
        )
    if connectors != _WANT_CONNECTORS and connectors >= _ORG_CHART_CONN_FLOOR:
        soft.append(
            f"connectors {connectors} != {_WANT_CONNECTORS} "
            f"(soft: >= {_ORG_CHART_CONN_FLOOR} still passes)"
        )
    summary = (
        f"org_chart_gen passed={not failures} boxes={boxes} "
        f"connectors={connectors}"
    )
    if soft:
        summary += " soft=" + "; ".join(soft)
    if failures:
        summary += " failures=" + "; ".join(failures)
    return OrgChartScore(
        failures=failures,
        soft=soft,
        summary=summary,
        passed=not failures,
        box_count=boxes,
        connector_count=connectors,
    )
