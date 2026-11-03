# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ask-box slash-command registry, routing, prefix filter, and LRU ranking.

Product commands include Tier 1 presets (rewrite / answer / local), plus
``/help``, ``/clear``, and ``/stop``. Echo-only ``[mock]`` stubs register only
through ``enable_mock_slash_commands()`` for the debug sidebar harness.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from plugin.framework.doc_type import DocumentType, get_document_type
from plugin.framework.i18n import N_, _
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS

log = logging.getLogger("writeragent.slash_commands")

SLASH_LRU_KEY = "slash_command_lru"

KEY_DOWN = 1024
KEY_UP = 1025
KEY_RETURN = 1280
KEY_ESCAPE = 1281
KEY_TAB = 1282
KEY_MODIFIER_SHIFT = 1

_KIND_LOCAL = "local"
_KIND_REWRITE = "rewrite"
_KIND_ANSWER = "answer"

_POPUP_IMMEDIATE = frozenset({"help", "clear", "stop", "undo"})


@dataclass(frozen=True)
class SlashCommand:
    """One registered command. ``name`` is without the leading slash."""

    name: str
    description: str
    kind: str = _KIND_LOCAL
    instruction: str = ""
    requires_arg: bool = False
    usage: str = ""


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", N_("Print the command list"), usage="/help"),
    SlashCommand("clear", N_("Start a new chat"), usage="/clear"),
    SlashCommand("stop", N_("Cancel the in-flight send"), usage="/stop"),
    SlashCommand(
        "summarize",
        N_("Summarize the selection or document in the sidebar"),
        kind=_KIND_ANSWER,
        instruction=N_("Summarize the following text concisely."),
        usage="/summarize [focus]",
    ),
    SlashCommand(
        "proofread",
        N_("Fix spelling, grammar, and punctuation without restyling"),
        kind=_KIND_REWRITE,
        instruction=N_("Fix spelling, grammar, and punctuation only. Do not change style, tone, or meaning."),
        usage="/proofread",
    ),
    SlashCommand(
        "shorten",
        N_("Shorten the selection"),
        kind=_KIND_REWRITE,
        instruction=N_("Make the text shorter while preserving the meaning."),
        usage="/shorten [target]",
    ),
    SlashCommand(
        "expand",
        N_("Expand the selection with more detail"),
        kind=_KIND_REWRITE,
        instruction=N_("Expand the text with more detail and clarity."),
        usage="/expand [what]",
    ),
    SlashCommand(
        "tone",
        N_("Rewrite the selection to match a described tone"),
        kind=_KIND_REWRITE,
        instruction=N_("Rewrite the text to match this tone or style: {arg}"),
        requires_arg=True,
        usage="/tone <description>",
    ),
    SlashCommand(
        "translate",
        N_("Translate the selection (sidebar when nothing is selected in Writer)"),
        kind=_KIND_REWRITE,
        instruction=N_("Translate the text into {arg}. Keep formatting and structure."),
        requires_arg=True,
        usage="/translate <language>",
    ),
    SlashCommand(
        "explain",
        N_("Explain the selection or active Calc formula"),
        kind=_KIND_ANSWER,
        instruction=N_("Explain the following in plain language."),
        usage="/explain",
    ),
    SlashCommand(
        "remember",
        N_("Save a fact to persistent memory"),
        kind=_KIND_LOCAL,
        requires_arg=True,
        usage="/remember <fact>",
    ),
    SlashCommand(
        "compact",
        N_("Compact older chat history now"),
        kind=_KIND_LOCAL,
        usage="/compact [focus]",
    ),
    SlashCommand(
        "undo",
        N_("Undo the last agent edit as one step"),
        kind=_KIND_LOCAL,
        usage="/undo",
    ),
    SlashCommand(
        "formula",
        N_("Write a Calc formula from a plain-English description"),
        kind=_KIND_LOCAL,
        requires_arg=True,
        usage="/formula <description>",
    ),
)

MOCK_SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("mock-alpha", N_("Mock command (echo only)")),
    SlashCommand("mock-bravo", N_("Mock command (echo only)")),
    SlashCommand("mock-charlie", N_("Mock command (echo only)")),
    SlashCommand("apply", N_("Apply last draft (mock)")),
    SlashCommand("web", N_("Web research (mock)")),
    SlashCommand("model", N_("Switch model (mock)")),
)

_mock_commands_enabled = False


def enable_mock_slash_commands() -> None:
    """Mock-LLM harness only: show the ``[mock]`` stubs in the popup and /help."""
    global _mock_commands_enabled
    _mock_commands_enabled = True


def registered_slash_commands() -> tuple[SlashCommand, ...]:
    """Commands the popup, /help and ``run_slash_command`` see right now."""
    if _mock_commands_enabled:
        return SLASH_COMMANDS + MOCK_SLASH_COMMANDS
    return SLASH_COMMANDS


def _registry_map() -> dict[str, SlashCommand]:
    return {c.name: c for c in registered_slash_commands()}


def parse_slash_text(text: str) -> tuple[SlashCommand, str] | None:
    """Return ``(command, arg)`` when the first token is a registered name, else None."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    token = parts[0].lstrip("/").lower()
    cmd = _registry_map().get(token)
    if cmd is None:
        return None
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def slash_typed_prefix(text: str) -> str | None:
    """Prefix after ``/`` for the first Ask-box token, or None if not a slash draft."""
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    first = stripped.split()[0]
    return first[1:].lower()


def filter_slash_commands(
    typed: str,
    lru: Sequence[str] | None = None,
) -> list[SlashCommand]:
    """Prefix-filter ``commands`` and rank by LRU then registry order."""
    registry = registered_slash_commands()
    prefix = slash_typed_prefix(typed)
    if prefix is None:
        return []
    matches = [cmd for cmd in registry if cmd.name.startswith(prefix)]
    seq = tuple(lru or ())
    lru_index = {n: i for i, n in enumerate(seq)}
    return sorted(matches, key=lambda cmd: lru_index.get(cmd.name, len(seq)))


def classify_slash_key(key_code: int, modifiers: int = 0) -> str | None:
    """Map a UNO key event to a popup action, or None if the Ask box should keep it."""
    if key_code == KEY_ESCAPE:
        return "escape"
    if key_code == KEY_UP:
        return "up"
    if key_code == KEY_DOWN:
        return "down"
    if key_code == KEY_TAB:
        return "tab"
    if key_code == KEY_RETURN and (modifiers & KEY_MODIFIER_SHIFT) == 0:
        return "enter"
    return None


def command_usage_hint(cmd: SlashCommand) -> str:
    if cmd.usage:
        return cmd.usage
    if cmd.requires_arg:
        return "/%s <…>" % cmd.name
    return "/%s" % cmd.name


def format_slash_item(cmd: SlashCommand) -> str:
    tag = " [mock]" if cmd in MOCK_SLASH_COMMANDS else ""
    hint = command_usage_hint(cmd)
    return "/%s%s — %s (%s)" % (cmd.name, tag, _(cmd.description), hint)


def format_help_text() -> str:
    lines = [_("Slash commands:")]
    for cmd in registered_slash_commands():
        lines.append("  " + format_slash_item(cmd))
    return "\n".join(lines)


def format_usage_line(cmd: SlashCommand) -> str:
    return _("Usage: {0}").format(command_usage_hint(cmd))


def slash_command_continues_chat_turn(cmd: SlashCommand) -> bool:
    """True when ``run_slash_command`` starts a normal chat stream (answer presets)."""
    return cmd.kind == _KIND_ANSWER


def load_slash_lru() -> list[str]:
    from plugin.framework.config import get_config

    raw = get_config(SLASH_LRU_KEY)
    if not isinstance(raw, list):
        return []
    return [str(item).lstrip("/").lower() for item in raw if str(item).strip()]


def record_slash_lru(name: str) -> None:
    from plugin.framework.errors import best_effort
    from plugin.framework.model_lru import update_lru_history

    clean = (name or "").lstrip("/").strip().lower()
    if not clean:
        return
    best_effort(
        lambda: update_lru_history(clean, SLASH_LRU_KEY, ""),
        f"record_slash_lru failed for {clean}",
    )


def _format_instruction(cmd: SlashCommand, arg: str) -> str:
    base = _(cmd.instruction)
    if "{arg}" in base:
        return base.format(arg=arg)
    if arg:
        return base + " " + arg
    return base


def _truncate_context(text: str) -> str:
    limit = CHAT_DOCUMENT_CONTEXT_MAX_CHARS
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _writer_has_selection(model: Any) -> bool:
    from plugin.doc.text_helpers import get_selection_text, selected_text_range

    controller = model.getCurrentController()
    if controller is None:
        return False
    rng = selected_text_range(controller)
    if rng is not None:
        from plugin.doc.text_helpers import get_string_without_tracked_deletions

        return bool(get_string_without_tracked_deletions(rng).strip())
    text = get_selection_text(model)
    return bool(text and str(text).strip())


def _writer_body_text(model: Any) -> str:
    from plugin.doc.text_helpers import get_string_without_tracked_deletions, normalize_linebreaks

    body = model.getText()
    cursor = body.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    return _truncate_context(normalize_linebreaks(get_string_without_tracked_deletions(cursor)))


def _calc_used_range_text(model: Any) -> str:
    sheet = model.CurrentController.ActiveSheet
    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    used = cursor.getRangeAddress()
    sel = model.CurrentController.Selection
    area = sel.getRangeAddress()
    start_col = max(area.StartColumn, used.StartColumn)
    end_col = min(area.EndColumn, used.EndColumn)
    start_row = max(area.StartRow, used.StartRow)
    end_row = min(area.EndRow, used.EndRow)
    if start_col > end_col or start_row > end_row:
        start_col, end_col = used.StartColumn, used.EndColumn
        start_row, end_row = used.StartRow, used.EndRow
    cell_range = sheet.getCellRangeByPosition(start_col, start_row, end_col, end_row)
    return _truncate_context(str(cell_range.getString() or ""))


def _calc_active_cell_explain_block(model: Any) -> str:
    view = model.CurrentController
    sheet = view.ActiveSheet
    selection = view.getSelection()
    addr = selection.getCellAddress()
    cell = sheet.getCellByPosition(addr.Column, addr.Row)
    formula = str(cell.getFormula() or "")
    col = addr.Column
    row = addr.Row

    def col_letters(c: int) -> str:
        letters = ""
        n = c + 1
        while n:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    address = "%s%d" % (col_letters(col), row + 1)
    if formula:
        return _("Cell {0} formula:\n{1}").format(address, formula)
    display = str(cell.getString() or cell.getValue())
    return _("Cell {0} value:\n{1}").format(address, display)


def quote_slash_answer_target(model: Any, command_name: str) -> str:
    """Build the quoted block appended to an answer preset instruction."""
    doc_type = get_document_type(model)
    if doc_type == DocumentType.CALC and command_name == "explain":
        return _calc_active_cell_explain_block(model)

    from plugin.doc.text_helpers import get_selection_text

    selection = get_selection_text(model)
    if selection and str(selection).strip():
        body = _truncate_context(str(selection).strip())
        return _("Selected text:\n{0}").format(body)

    if doc_type == DocumentType.WRITER:
        body = _writer_body_text(model)
        if body.strip():
            return _("Whole document:\n{0}").format(body)
        return _("The document is empty.")

    if doc_type == DocumentType.CALC:
        body = _calc_used_range_text(model)
        if body.strip():
            return _("Used range:\n{0}").format(body)
        return _("The sheet has no data in the used range.")

    if selection:
        return _("Selected text:\n{0}").format(_truncate_context(str(selection)))
    return _("No selection text is available for this document type.")


def build_slash_answer_query(cmd: SlashCommand, arg: str, model: Any) -> str:
    instruction = _format_instruction(cmd, arg)
    if cmd.name == "summarize" and arg and "{arg}" not in _(cmd.instruction):
        instruction = _("Summarize the following text. Focus on: {0}").format(arg)
    quoted = quote_slash_answer_target(model, cmd.name)
    return instruction + "\n\n" + quoted


def _remember_key(fact: str) -> str:
    digest = hashlib.sha256(fact.strip().encode("utf-8")).hexdigest()[:12]
    return "slash." + digest


def _complete_ask_draft(host: Any, name: str) -> None:
    from plugin.chatbot.dialogs import set_control_text
    from plugin.chatbot.send_state import SendEvent, SendEventKind

    query = getattr(host, "query_control", None)
    if query is not None:
        set_control_text(query, "/" + name + " ")
    dispatch = getattr(host, "dispatch", None)
    if callable(dispatch):
        dispatch(SendEvent(SendEventKind.TEXT_UPDATED, has_text=True))


def _run_remember(host: Any, fact: str) -> None:
    from plugin.chatbot.memory import MemoryStore

    try:
        store = MemoryStore()
    except Exception as exc:
        host.append_output("\n" + str(exc) + "\n")
        return
    result = store.upsert(_remember_key(fact), fact)
    if result.get("status") == "error":
        host.append_output("\n" + str(result.get("message", _("Failed to save memory."))) + "\n")
        return
    host.append_output("\n" + _("Saved to memory: {0}").format(fact) + "\n")


def _compact_result_line(result: Any) -> str:
    if result.compacted:
        return _("Conversation compacted.")
    reason = getattr(result, "reason", "") or ""
    if reason == "nothing_to_compact":
        return _("Nothing to compact.")
    if reason == "below_threshold":
        return _("Nothing to compact yet (history is still small).")
    if reason == "no_window":
        return _("Compaction is not available for this model.")
    return _("Compaction failed.")


def run_slash_command(
    name: str,
    host: Any,
    arg: str = "",
    *,
    from_popup: bool = False,
) -> bool:
    """Run a registry command. Returns False when ``name`` is unknown."""
    clean = name.lstrip("/").strip().lower()
    cmd = _registry_map().get(clean)
    if cmd is None:
        return False
    record_slash_lru(cmd.name)
    host.hide_popup()

    if from_popup and cmd.name not in _POPUP_IMMEDIATE and cmd not in MOCK_SLASH_COMMANDS:
        _complete_ask_draft(host, cmd.name)
        return True

    if cmd.requires_arg and not (arg or "").strip():
        host.append_output("\n" + format_usage_line(cmd) + "\n")
        return True

    if cmd.name == "stop":
        host.stop_in_flight()
        return True

    if cmd.name in _POPUP_IMMEDIATE or cmd in MOCK_SLASH_COMMANDS:
        if cmd.name == "help":
            host.clear_ask()
            host.append_output("\n" + format_help_text() + "\n")
            return True
        if cmd.name == "clear":
            host.clear_ask()
            host.clear_chat()
            return True
        if cmd in MOCK_SLASH_COMMANDS:
            host.clear_ask()
            host.append_output("\nslash: /%s\n" % cmd.name)
            return True

    if cmd.name == "remember":
        host.clear_ask()
        _run_remember(host, arg.strip())
        return True

    if cmd.name == "compact":
        host.clear_ask()
        run_compact = getattr(host, "run_slash_compact", None)
        if not callable(run_compact):
            host.append_output("\n" + _("Compaction is unavailable.") + "\n")
            return True
        run_compact(arg.strip())
        return True

    if cmd.name == "undo":
        host.clear_ask()
        in_flight = getattr(host, "slash_agent_edit_in_flight", None)
        if callable(in_flight) and in_flight():
            host.append_output("\n" + _("Wait for the current operation to finish before /undo.") + "\n")
            return True
        model_fn = getattr(host, "slash_document_model", None)
        model = model_fn() if callable(model_fn) else None
        if model is None:
            host.append_output("\n" + _("No document is open.") + "\n")
            return True
        from plugin.doc.agent_undo import undo_last_agent_edit

        host.append_output("\n" + undo_last_agent_edit(model) + "\n")
        return True

    if cmd.name == "formula":
        host.clear_ask()
        run_formula = getattr(host, "run_slash_formula", None)
        if not callable(run_formula):
            host.append_output("\n" + _("Formula generation is unavailable.") + "\n")
            return True
        run_formula(arg.strip())
        return True

    model_fn = getattr(host, "slash_document_model", None)
    model = model_fn() if callable(model_fn) else None

    effective_kind = cmd.kind
    if (
        cmd.name == "translate"
        and effective_kind == _KIND_REWRITE
        and model is not None
        and get_document_type(model) == DocumentType.WRITER
        and not _writer_has_selection(model)
    ):
        effective_kind = _KIND_ANSWER

    if effective_kind == _KIND_ANSWER:
        host.clear_ask()
        if model is None:
            host.append_output("\n" + _("No document is open.") + "\n")
            return True
        query = build_slash_answer_query(cmd, arg, model)
        send_answer = getattr(host, "slash_send_answer", None)
        if not callable(send_answer):
            host.append_output("\n" + query + "\n")
            return True
        send_answer(query)
        return True

    if effective_kind == _KIND_REWRITE:
        host.clear_ask()
        instruction = _format_instruction(cmd, arg)
        rewrite = getattr(host, "slash_run_rewrite", None)
        if not callable(rewrite):
            host.append_output("\n" + instruction + "\n")
            return True
        rewrite(instruction)
        return True

    host.clear_ask()
    host.append_output("\nslash: /%s\n" % cmd.name)
    return True


def run_slash_compact_worker(
    host: Any,
    *,
    focus: str,
    session: Any,
    ctx: Any,
) -> None:
    """Background job: ``compact_session(force=True)`` under ``llm_request_lane``."""
    from plugin.chatbot.compaction import compact_session
    from plugin.chatbot.selection import create_validated_client
    from plugin.framework.config import get_config
    from plugin.framework.client.model_fetcher import resolve_context_window
    from plugin.framework.llm_lanes import llm_request_lane
    from plugin.framework.queue_executor import main
    from plugin.main import get_tools

    title = _("WriterAgent")
    client = create_validated_client(ctx, title)
    if client is None:
        main.post(lambda: host.append_output("\n" + _("Configure an API key to compact.") + "\n"))
        return

    if not get_config("chat_compaction_enabled"):
        main.post(lambda: host.append_output("\n" + _("Nothing to compact.") + "\n"))
        return

    tools = get_tools()

    def status_cb(text: str) -> None:
        log.debug("slash compact status: %s", text)

    result_holder: list[Any] = []

    def worker() -> None:
        try:
            with llm_request_lane(status_callback=status_cb, resume_status=_("Thinking...")):
                result_holder.append(
                    compact_session(
                        session,
                        client,
                        window=resolve_context_window(client),
                        tools=tools,
                        force=True,
                        focus=focus or None,
                    )
                )
        except Exception:
            log.exception("slash /compact failed")
            result_holder.append(None)

        def finish() -> None:
            if not result_holder:
                host.append_output("\n" + _("Compaction failed.") + "\n")
                return
            item = result_holder[0]
            if item is None:
                host.append_output("\n" + _("Compaction failed.") + "\n")
            else:
                host.append_output("\n" + _compact_result_line(item) + "\n")

        main.post(finish)

    from plugin.framework.worker_pool import run_in_background

    run_in_background(worker, name="slash-compact", dedicated=True)


_FORMULA_UNDO_TITLE = N_("WriterAgent: Formula")


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


def build_slash_formula_prompt(model: Any, description: str) -> tuple[str, str, Any]:
    """Return (prompt, target_cell_address, cell) for the active Calc cell."""
    from plugin.calc.address_utils import format_address, format_range

    view = model.CurrentController
    sheet = view.ActiveSheet
    sheet_name = str(sheet.getName() or "")
    selection = view.getSelection()
    addr = selection.getCellAddress()
    cell = sheet.getCellByPosition(addr.Column, addr.Row)
    target = format_address(addr.Column, addr.Row)

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
    header_row = str(header_range.getString() or "").replace("\t", " | ")

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
            "Use English function names. Use comma separators. No prose or markdown."
        )
    )
    return prompt, target, cell


def run_slash_formula_worker(host: Any, *, ctx: Any, model: Any, description: str) -> None:
    """Background LLM call; formula insert and sidebar lines run on the main thread."""
    from plugin.chatbot.selection import create_validated_client, stream_completion
    from plugin.doc.agent_undo import record_agent_turn_after, record_agent_turn_before
    from plugin.doc.compound_undo import commit_or_rollback
    from plugin.framework.config import get_config
    from plugin.framework.errors import format_error_message
    from plugin.framework.llm_lanes import llm_request_lane
    from plugin.framework.queue_executor import main

    title = _("WriterAgent: Formula")
    client = create_validated_client(ctx, title)
    if client is None:
        return

    try:
        prompt, target_addr, cell = build_slash_formula_prompt(model, description)
    except Exception:
        log.exception("slash /formula prompt build failed")
        main.post(lambda: host.append_output("\n" + _("Could not read the spreadsheet.") + "\n"))
        return

    system_prompt = _(
        "You write LibreOffice Calc formulas. Output a single formula starting with =, nothing else."
    )
    max_tokens = get_config("chatbot.extend_selection_max_tokens")
    chunks: list[str] = []

    def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
        if not is_thinking:
            chunks.append(chunk_text)

    def insert_on_main() -> None:
        formula = clean_formula_model_reply("".join(chunks))
        if not formula.startswith("="):
            host.append_output("\n" + formula + "\n")
            return

        record_agent_turn_before(model)

        def mutate() -> None:
            cell.setFormula(formula)

        try:
            err = commit_or_rollback(model, _(_FORMULA_UNDO_TITLE), mutate)
        except Exception as exc:
            host.append_output("\n" + format_error_message(exc) + "\n")
            record_agent_turn_after(model)
            return
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

    def on_done() -> None:
        main.post(insert_on_main)

    def on_error(exc: Exception) -> None:
        main.post(lambda: host.append_output("\n" + format_error_message(exc) + "\n"))

    def worker() -> None:
        try:
            with llm_request_lane(resume_status=_("Thinking...")):
                stream_completion(
                    ctx,
                    client,
                    prompt,
                    system_prompt,
                    max_tokens,
                    apply_chunk,
                    on_done,
                    on_error,
                )
        except Exception:
            log.exception("slash /formula failed")
            main.post(lambda: host.append_output("\n" + _("Formula generation failed.") + "\n"))

    from plugin.framework.worker_pool import run_in_background

    run_in_background(worker, name="slash-formula", dedicated=True)
