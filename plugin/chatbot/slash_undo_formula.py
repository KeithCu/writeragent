# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Slash commands /undo and /formula (handlers extracted from slash_commands)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.framework.errors import format_error_message, reraise_if_disposed
from plugin.framework.i18n import N_, _

if TYPE_CHECKING:
    from plugin.chatbot.slash_commands import SlashHost

log = logging.getLogger(__name__)

FORMULA_UNDO_TITLE = N_("WriterAgent: Formula")


class SlashFormulaPromptError(Exception):
    """User-visible /formula prompt build failure."""


def run_slash_undo(host: SlashHost) -> None:
    if host.slash_agent_edit_in_flight():
        host.append_output("\n" + _("Wait for the current operation to finish before /undo.") + "\n")
        return
    model = host.slash_document_model()
    if model is None:
        host.append_output("\n" + _("No document is open.") + "\n")
        return
    from plugin.doc.agent_undo import undo_last_agent_edit

    host.append_output("\n" + undo_last_agent_edit(model) + "\n")


def clean_formula_model_reply(text: str) -> str:
    """Strip fences and whitespace from a one-line formula completion."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()
        else:
            raw = raw.strip("`").strip()
    return raw.strip()


def _header_row_text(header_range: Any) -> str:
    data = header_range.getDataArray()
    if not data or not data[0]:
        return ""
    parts: list[str] = []
    for offset, val in enumerate(data[0]):
        if isinstance(val, float):
            val = header_range.getCellByPosition(offset, 0).getString()
        text = str(val or "").strip()
        if text:
            parts.append(text)
    return " | ".join(parts)


def build_slash_formula_prompt(model: Any, description: str) -> tuple[str, str, Any]:
    """Return (prompt, target_cell_address, cell) for the active Calc cell."""
    from plugin.calc.address_utils import format_address, format_range

    view = model.CurrentController
    sheet = view.ActiveSheet
    sheet_name = str(sheet.getName() or "")
    selection = view.getSelection()
    if selection is None:
        raise SlashFormulaPromptError(_("Select a cell first."))
    try:
        range_addr = selection.getRangeAddress()
    except Exception as exc:
        reraise_if_disposed(exc)
        raise SlashFormulaPromptError(_("Select a cell first.")) from exc

    col = int(range_addr.StartColumn)
    row = int(range_addr.StartRow)
    cell = sheet.getCellByPosition(col, row)
    target = format_address(col, row)

    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    used = cursor.getRangeAddress()
    used_label = format_range(used.StartColumn, used.StartRow, used.EndColumn, used.EndRow)
    header_range = sheet.getCellRangeByPosition(
        used.StartColumn,
        used.StartRow,
        used.EndColumn,
        used.StartRow,
    )
    header_row = _header_row_text(header_range)

    prompt = (
        _("Write exactly one LibreOffice Calc formula for this request: {0}").format(description.strip())
        + "\n\n"
        + _("Target cell: {0} on sheet \"{1}\".").format(target, sheet_name)
        + "\n"
        + _("Used range: {0}.").format(used_label)
        + "\n"
        + _("Header row: {0}.").format(header_row or _("(empty)"))
        + "\n\n"
        + _(
            "Reply with the formula only. It must start with =. "
            "Use English function names. Use semicolon (;) between function arguments "
            "(Calc setFormula API grammar). No prose or markdown."
        )
    )
    return prompt, target, cell


def insert_slash_formula_on_main(
    host: SlashHost,
    *,
    model: Any,
    cell: Any,
    target_addr: str,
    chunks: list[str],
) -> None:
    from plugin.doc.agent_undo import record_agent_turn_after, record_agent_turn_before
    from plugin.doc.compound_undo import commit_or_rollback

    formula = clean_formula_model_reply("".join(chunks))
    if not formula.startswith("="):
        host.append_output("\n" + formula + "\n")
        return

    record_agent_turn_before(model)

    def mutate() -> None:
        cell.setFormula(formula)

    try:
        outcome = commit_or_rollback(model, _(FORMULA_UNDO_TITLE), mutate)
        outcome.raise_mutate_error()
    except Exception as exc:
        log.exception("slash /formula insert failed")
        host.append_output("\n" + format_error_message(exc) + "\n")
        record_agent_turn_after(model)
        return
    err = outcome.error
    if err is not None:
        host.append_output("\n" + str(err.get("message", _("Could not insert the formula."))) + "\n")
        record_agent_turn_after(model)
        return

    record_agent_turn_after(model)
    lines = [_("Formula for {0}: {1}").format(target_addr, formula)]
    try:
        if int(cell.getError()) != 0:
            lines.append(_("The cell shows an error"))
    except Exception:
        log.debug("slash /formula: could not read cell error", exc_info=True)
    host.append_output("\n" + "\n".join(lines) + "\n")


def run_slash_formula_worker(host: SlashHost, *, ctx: Any, model: Any, description: str) -> None:
    """LLM stream under send cancellation; formula insert runs on the main thread."""
    from plugin.chatbot.selection import create_validated_client, stream_completion
    from plugin.framework.config import get_config
    from plugin.framework.send_cancellation import capture_send_stop

    title = _(FORMULA_UNDO_TITLE)
    client = create_validated_client(ctx, title)
    if client is None:
        return

    try:
        prompt, target_addr, cell = build_slash_formula_prompt(model, description)
    except SlashFormulaPromptError as exc:
        host.append_output("\n" + str(exc) + "\n")
        return
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("slash /formula prompt build failed")
        host.append_output("\n" + format_error_message(exc) + "\n")
        return

    system_prompt = _(
        "You write LibreOffice Calc formulas. Output a single formula starting with =, nothing else."
    )
    max_tokens = get_config("chatbot.extend_selection_max_tokens")
    chunks: list[str] = []
    cancel = capture_send_stop(host._send_cancellation)

    def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
        if not is_thinking:
            chunks.append(chunk_text)

    def on_done() -> None:
        insert_slash_formula_on_main(
            host,
            model=model,
            cell=cell,
            target_addr=target_addr,
            chunks=chunks,
        )

    def on_error(exc: Exception) -> None:
        host.append_output("\n" + format_error_message(exc) + "\n")

    def on_stopped() -> None:
        host.append_output("\n" + _("Stopped.") + "\n")

    try:
        stream_completion(
            ctx,
            client,
            prompt,
            system_prompt,
            max_tokens,
            apply_chunk,
            on_done,
            on_error,
            cancel=cancel,
            on_stopped_fn=on_stopped,
        )
    except Exception:
        log.exception("slash /formula failed")
        host.append_output("\n" + _("Formula generation failed.") + "\n")
