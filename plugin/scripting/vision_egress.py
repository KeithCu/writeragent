# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert trusted vision helper HTML results into Writer and Calc."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.vision_common import resolve_vision_insert_mode

log = logging.getLogger(__name__)


def is_vision_result(value: Any) -> bool:
    """True when *value* matches the compact vision helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    return bool(value.get("helper")) or value.get("status") == "error"


def vision_html_from_result(result: dict[str, Any]) -> str:
    """Return Docling/Paddle HTML payload for LO import."""
    if result.get("status") == "error":
        code = str(result.get("code") or "VISION_ERROR")
        message = str(result.get("message") or "Vision helper failed.")
        raise ToolExecutionError(message, code=code, details={"vision_result": result})

    if result.get("status") != "ok":
        raise ToolExecutionError(
            "Vision helper returned an unexpected status.",
            code="VISION_ERROR",
            details={"vision_result": result},
        )

    html = result.get("html")
    if html is None:
        raise ToolExecutionError(
            "Vision helper result is missing html.",
            code="VISION_ERROR",
            details={"vision_result": result},
        )
    return str(html)


def insert_vision_result_into_writer(ctx: Any, doc: Any, result: dict[str, Any]) -> None:
    """Insert formatted vision HTML at the Writer text cursor."""
    from plugin.writer.format import insert_content_at_position

    html = vision_html_from_result(result)
    if not html.strip():
        raise ToolExecutionError(
            "Vision helper returned empty HTML.",
            code="VISION_ERROR",
            details={"vision_result": result},
        )
    log.debug(
        "insert_vision_result: helper=%s html_len=%d h_tags=%d style_attrs=%d snippet=%r",
        result.get("helper"),
        len(html),
        html.lower().count("<h"),
        html.count("style="),
        html[:120],
    )
    insert_content_at_position(doc, ctx, html, "selection")


def insert_vision_result(
    ctx: Any,
    doc: Any,
    result: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
) -> None:
    """Insert vision output into Writer or Calc."""
    from plugin.calc.vision_egress import insert_vision_html_into_calc, insert_vision_structure_into_calc
    from plugin.doc.document_helpers import is_calc, is_writer

    insert_mode = resolve_vision_insert_mode(ctx, params)
    helper = str(result.get("helper") or "")

    if is_writer(doc):
        insert_vision_result_into_writer(ctx, doc, result)
        return
    if is_calc(doc):
        if insert_mode == "structured" and helper == "extract_structure":
            try:
                row_count = insert_vision_structure_into_calc(doc, ctx, result)
                log.debug(
                    "insert_vision_result: helper=%s insert_mode=structured calc_rows=%d",
                    helper,
                    row_count,
                )
                return
            except ToolExecutionError as exc:
                if exc.code != "VISION_ERROR":
                    raise
                log.debug("structured Calc insert empty; falling back to HTML")
        html = vision_html_from_result(result)
        if not html.strip():
            raise ToolExecutionError(
                "Vision helper returned empty HTML.",
                code="VISION_ERROR",
                details={"vision_result": result},
            )
        log.debug("insert_vision_result: helper=%s insert_mode=%s calc=html", helper, insert_mode)
        insert_vision_html_into_calc(doc, ctx, html)
        return
    raise ToolExecutionError(
        "Vision helpers require a Writer or Calc document.",
        code="VISION_ERROR",
    )
