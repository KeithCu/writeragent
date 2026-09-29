# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Soft notes for eval-1 task ``solar_sld_gen``.

Hard pass/fail stays in ``oracles.oracle_solar_sld_gen`` (labels, topology
edges, microinverters vs string inverter). Soft notes cover readable
source→load order, main panel on the utility side of the Solar AC Disconnect,
MID/downstream on the post-disconnect path, and a single emphasized Solar AC
Disconnect. Does not invent a partial score; ``agent_score`` stays binary from
hard fails.
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
    _SOLAR_BOX_FLOOR,
    _SOLAR_CONN_FLOOR,
    _flatten_draw_nodes,
    _solar_connectors,
    _solar_labeled_boxes,
    _solar_role_keys,
    check_oracle,
    parse_json_export,
)

SOLAR_SLD_TASK_ID = "solar_sld_gen"
_STRING_FAILURE = (
    "solar_sld_gen requires the headless LO backend; "
    "the string simulator does not export production connector nodes"
)
_WANT_BOXES = 10
_WANT_CONNECTORS = 9


@dataclass
class SolarSldScore:
    failures: list[str]
    soft: list[str] = field(default_factory=list)
    summary: str = ""
    passed: bool = False
    box_count: int = 0
    connector_count: int = 0


def _geometry(node: dict[str, Any]) -> tuple[float, float] | None:
    geo = node.get("geometry")
    if isinstance(geo, dict) and "x" in geo and "y" in geo:
        try:
            return float(geo["x"]), float(geo["y"])
        except (TypeError, ValueError):
            return None
    if "x" in node and "y" in node:
        try:
            return float(node["x"]), float(node["y"])
        except (TypeError, ValueError):
            return None
    return None


def _role_positions(
    nodes: list[dict[str, Any]],
) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = str(node.get("text") or "")
        roles = _solar_role_keys(text)
        pos = _geometry(node)
        if not roles or pos is None:
            continue
        for role in roles:
            out.setdefault(role, []).append(pos)
    return out


def _counts(doc: str) -> tuple[int, int]:
    data = parse_json_export(doc) or {}
    tree = data.get("tree")
    nodes: list[dict[str, Any]] = (
        _flatten_draw_nodes([n for n in tree if isinstance(n, dict)])
        if isinstance(tree, list)
        else []
    )
    boxes = len(_solar_labeled_boxes(nodes))
    connectors = len(_solar_connectors(nodes))
    if connectors == 0:
        raw = data.get("connections")
        if isinstance(raw, list):
            connectors = len(raw)
    return boxes, connectors


def _soft_layout(nodes: list[dict[str, Any]]) -> list[str]:
    soft: list[str] = []
    positions = _role_positions(nodes)
    if not positions:
        return soft

    def _mean_x(role: str) -> float | None:
        pts = positions.get(role) or []
        if not pts:
            return None
        return sum(p[0] for p in pts) / len(pts)

    util_x = _mean_x("utility")
    meter_x = _mean_x("meter")
    source_xs = [x for x in (util_x, meter_x) if x is not None]
    source_x = min(source_xs) if source_xs else None
    load_xs = [
        x
        for x in (
            _mean_x("backup_subpanel"),
            _mean_x("main_panel"),
        )
        if x is not None
    ]
    load_x = max(load_xs) if load_xs else None
    if source_x is not None and load_x is not None and source_x > load_x + 500:
        # Also accept top→bottom (source above loads).
        util_ys = [p[1] for p in (positions.get("utility") or [])]
        load_ys = [
            p[1]
            for role in ("backup_subpanel", "main_panel")
            for p in (positions.get(role) or [])
        ]
        top_down = (
            util_ys
            and load_ys
            and (sum(util_ys) / len(util_ys)) < (sum(load_ys) / len(load_ys)) - 500
        )
        if not top_down:
            soft.append(
                "layout soft: source (utility/meter) not clearly before loads "
                "(prefer left→right or top→bottom source→load)"
            )

    main_x = _mean_x("main_panel")
    mid_x = _mean_x("controller")
    acd_x = _mean_x("ac_disconnect")
    if source_x is not None and main_x is not None and mid_x is not None:
        # Main should sit closer to utility than the MID does (utility side of
        # the Solar AC Disconnect / post-disconnect MID path).
        if abs(main_x - source_x) > abs(mid_x - source_x) + 1500:
            soft.append(
                "layout soft: main panel not on utility side of MID "
                f"(main_x={main_x:.0f}, mid_x={mid_x:.0f}, source_x={source_x:.0f})"
            )
    if (
        main_x is not None
        and acd_x is not None
        and mid_x is not None
        and source_x is not None
    ):
        # Prefer Main → ACD → MID along the source→load axis.
        if not (main_x <= acd_x + 500 and acd_x <= mid_x + 500) and not (
            mid_x <= acd_x + 500 and acd_x <= main_x + 500
        ):
            # Accept top→bottom stacking as an alternate readable order.
            main_ys = [p[1] for p in (positions.get("main_panel") or [])]
            acd_ys = [p[1] for p in (positions.get("ac_disconnect") or [])]
            mid_ys = [p[1] for p in (positions.get("controller") or [])]
            stacked = False
            if main_ys and acd_ys and mid_ys:
                my = sum(main_ys) / len(main_ys)
                ay = sum(acd_ys) / len(acd_ys)
                iy = sum(mid_ys) / len(mid_ys)
                stacked = (my <= ay + 500 and ay <= iy + 500) or (
                    iy <= ay + 500 and ay <= my + 500
                )
            if not stacked:
                soft.append(
                    "layout soft: Solar AC Disconnect not between main panel "
                    "and MID on the post-disconnect path "
                    f"(main_x={main_x:.0f}, acd_x={acd_x:.0f}, mid_x={mid_x:.0f})"
                )

    # Single emphasized Solar AC Disconnect (ignore long legend/notes boxes).
    ac_boxes = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        text = str(n.get("text") or "")
        if "ac_disconnect" not in _solar_role_keys(text):
            continue
        # Title-sized equipment label, not a topology-notes paragraph.
        if len(text) > 160 or "topology notes" in text.casefold():
            continue
        ac_boxes.append(n)
    if len(ac_boxes) == 0:
        soft.append("layout soft: no Solar AC Disconnect box counted for emphasis")
    elif len(ac_boxes) > 1:
        soft.append(
            f"layout soft: {len(ac_boxes)} Solar AC Disconnect labels "
            "(prefer a single emphasized disconnect)"
        )
    return soft


def score_solar_sld_example(
    final_document: str,
    *,
    backend: str,
) -> SolarSldScore:
    """Hard oracle + soft layout notes. String backend fails closed."""
    if backend != "lo":
        return SolarSldScore(
            failures=[_STRING_FAILURE],
            summary=_STRING_FAILURE,
        )
    failures = check_oracle(SOLAR_SLD_TASK_ID, final_document or "")
    boxes, connectors = _counts(final_document or "")
    soft: list[str] = []
    if boxes != _WANT_BOXES and boxes >= _SOLAR_BOX_FLOOR:
        soft.append(
            f"labeled boxes {boxes} != {_WANT_BOXES} "
            f"(soft: >= {_SOLAR_BOX_FLOOR} still passes)"
        )
    if connectors != _WANT_CONNECTORS and connectors >= _SOLAR_CONN_FLOOR:
        soft.append(
            f"connectors {connectors} != {_WANT_CONNECTORS} "
            f"(soft: >= {_SOLAR_CONN_FLOOR} still passes)"
        )
    data = parse_json_export(final_document or "") or {}
    tree = data.get("tree")
    nodes: list[dict[str, Any]] = (
        _flatten_draw_nodes([n for n in tree if isinstance(n, dict)])
        if isinstance(tree, list)
        else []
    )
    soft.extend(_soft_layout(nodes))
    summary = (
        f"solar_sld_gen passed={not failures} boxes={boxes} "
        f"connectors={connectors}"
    )
    if soft:
        summary += " soft=" + "; ".join(soft)
    if failures:
        summary += " failures=" + "; ".join(failures)
    return SolarSldScore(
        failures=failures,
        soft=soft,
        summary=summary,
        passed=not failures,
        box_count=boxes,
        connector_count=connectors,
    )
