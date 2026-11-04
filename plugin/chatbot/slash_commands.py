# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ask-box slash-command registry, routing, prefix filter, and LRU ranking.

Product commands include Tier 1 presets (rewrite / answer / local), plus
``/help``, ``/clear``, and ``/stop``. Echo-only ``[mock]`` stubs register only
through ``enable_mock_slash_commands()`` for the debug sidebar harness.
Ranking is prefix match, then ``slash_command_lru`` in ``writeragent.json``.
Do not implement the consider-list in ``docs/chat/slash-commands.md`` here.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

from plugin.framework.doc_type import DocumentType, get_document_type
from plugin.framework.i18n import N_, _

log = logging.getLogger(__name__)

# Persist via the same LRU helper as model/prompt history (not a sidecar file).
SLASH_LRU_KEY = "slash_command_lru"

# com.sun.star.awt.Key — integers so unit tests need no soffice.
KEY_DOWN = 1024
KEY_UP = 1025
KEY_RETURN = 1280
KEY_ESCAPE = 1281
KEY_TAB = 1282
KEY_MODIFIER_SHIFT = 1


class SlashRun(Enum):
    LOCAL = "local"
    REWRITE = "rewrite"
    ANSWER = "answer"
    COMPACT = "compact"
    FORMULA = "formula"


class SlashResult(Enum):
    HANDLED = "handled"
    CONTINUE_SEND = "continue_send"


_SEND_TURN_RUNS = frozenset({SlashRun.ANSWER, SlashRun.COMPACT, SlashRun.FORMULA})


class SlashHost(Protocol):
    ctx: Any
    session: Any
    sidebar_state: Any
    cached_doc_type: Any
    cached_uno_services: Any
    _send_cancellation: Any
    query_control: Any

    def dispatch(self, event: Any) -> None: ...
    def _set_status(self, text: str) -> None: ...
    def hide_popup(self) -> None: ...
    def clear_ask(self) -> None: ...
    def append_output(self, text: str) -> None: ...
    def clear_chat(self) -> None: ...
    def stop_in_flight(self) -> None: ...
    def slash_document_model(self) -> Any: ...
    def slash_run_rewrite(self, instruction: str) -> None: ...
    def slash_agent_edit_in_flight(self) -> bool: ...
    def run_slash_formula(self, description: str) -> None: ...


# Local slash commands safe to run during an in-flight tool loop (no new agent turn).
_SLASH_SAFE_WHILE_BUSY = frozenset({"help", "stop", "remember"})


@dataclass(frozen=True)
class SlashCommand:
    """One registered command. ``name`` is without the leading slash."""

    name: str
    description: str
    run: SlashRun = SlashRun.LOCAL
    instruction: str = ""
    requires_arg: bool = False
    usage: str = ""
    handler: Callable[[SlashHost, SlashCommand, str], None] | None = None


def _run_help(host: SlashHost, _cmd: SlashCommand, _arg: str) -> None:
    host.clear_ask()
    host.append_output("\n" + format_help_text() + "\n")


def _run_clear(host: SlashHost, _cmd: SlashCommand, _arg: str) -> None:
    host.clear_ask()
    host.clear_chat()


def _run_stop(host: SlashHost, _cmd: SlashCommand, _arg: str) -> None:
    host.stop_in_flight()
    # Bugfix (QA B11): partial slash drafts like "/sto" stayed in Ask after Stop.
    host.clear_ask()


def _run_remember(host: SlashHost, _cmd: SlashCommand, arg: str) -> None:
    from plugin.chatbot.memory import MemoryStore

    fact = arg.strip()
    host.clear_ask()
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


def _run_mock_echo(host: SlashHost, cmd: SlashCommand, _arg: str) -> None:
    host.clear_ask()
    host.append_output("\nslash: /%s\n" % cmd.name)


def _run_rewrite(host: SlashHost, cmd: SlashCommand, arg: str) -> None:
    host.clear_ask()
    host.slash_run_rewrite(_format_instruction(cmd, arg))


def _run_undo(host: SlashHost, _cmd: SlashCommand, _arg: str) -> None:
    host.clear_ask()
    from plugin.chatbot.slash_undo_formula import run_slash_undo

    run_slash_undo(host)


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", N_("Print the command list"), usage="/help", handler=_run_help),
    SlashCommand("clear", N_("Start a new chat"), usage="/clear", handler=_run_clear),
    SlashCommand("stop", N_("Cancel the in-flight send"), usage="/stop", handler=_run_stop),
    SlashCommand(
        "summarize",
        N_("Summarize the selection or document in the sidebar"),
        run=SlashRun.ANSWER,
        instruction=N_("Summarize the selected text, or the whole document if nothing is selected."),
        usage="/summarize [focus]",
    ),
    SlashCommand(
        "proofread",
        N_("Fix spelling, grammar, and punctuation without restyling"),
        run=SlashRun.REWRITE,
        instruction=N_("Fix spelling, grammar, and punctuation only. Do not change style, tone, or meaning."),
        usage="/proofread",
    ),
    SlashCommand(
        "shorten",
        N_("Shorten the selection"),
        run=SlashRun.REWRITE,
        instruction=N_("Make the text shorter while preserving the meaning."),
        usage="/shorten [target]",
    ),
    SlashCommand(
        "expand",
        N_("Expand the selection with more detail"),
        run=SlashRun.REWRITE,
        instruction=N_("Expand the text with more detail and clarity."),
        usage="/expand [what]",
    ),
    SlashCommand(
        "tone",
        N_("Rewrite the selection to match a described tone"),
        run=SlashRun.REWRITE,
        instruction=N_("Rewrite the text to match this tone or style: {arg}"),
        requires_arg=True,
        usage="/tone <description>",
    ),
    SlashCommand(
        "translate",
        N_("Translate the selection (sidebar when nothing is selected in Writer)"),
        run=SlashRun.REWRITE,
        instruction=N_("Translate the text into {arg}. Keep formatting and structure."),
        requires_arg=True,
        usage="/translate <language>",
    ),
    SlashCommand(
        "explain",
        N_("Explain the selection or active Calc formula"),
        run=SlashRun.ANSWER,
        instruction=N_("Explain the following in plain language."),
        usage="/explain",
    ),
    SlashCommand(
        "remember",
        N_("Save a fact to persistent memory"),
        requires_arg=True,
        usage="/remember <fact>",
        handler=_run_remember,
    ),
    SlashCommand(
        "compact",
        N_("Compact older chat history now"),
        run=SlashRun.COMPACT,  # Send after _begin_send; run_slash_compact, not run_slash_command handler
        usage="/compact [focus]",
        handler=None,  # COMPACT: no popup/local handler
    ),
    SlashCommand(
        "undo",
        N_("Undo the last agent edit as one step"),
        usage="/undo",
        handler=_run_undo,
    ),
    SlashCommand(
        "formula",
        N_("Write a Calc formula from a plain-English description"),
        run=SlashRun.FORMULA,  # Send after _begin_send; panel run_slash_formula
        requires_arg=True,
        usage="/formula <description>",
        handler=None,  # FORMULA: no popup/local handler
    ),
)

# Echo-only stubs for the mock-LLM sidebar harness and prefix/LRU tests.
# Never in the product list: only ``enable_mock_slash_commands()`` (called by
# the debug-only sidebar test hooks) adds them to ``registered_slash_commands``.
MOCK_SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("mock-alpha", N_("Mock command (echo only)"), handler=_run_mock_echo),
    SlashCommand("mock-bravo", N_("Mock command (echo only)"), handler=_run_mock_echo),
    SlashCommand("mock-charlie", N_("Mock command (echo only)"), handler=_run_mock_echo),
    SlashCommand("apply", N_("Apply last draft (mock)"), handler=_run_mock_echo),
    SlashCommand("web", N_("Web research (mock)"), handler=_run_mock_echo),
    SlashCommand("model", N_("Switch model (mock)"), handler=_run_mock_echo),
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


def get_slash_command(name: str) -> SlashCommand | None:
    """Lookup a registered command by name (without leading slash)."""
    clean = (name or "").lstrip("/").strip().lower()
    if not clean:
        return None
    return _registry_map().get(clean)


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


def slash_ask_past_command_name(text: str) -> bool:
    """True when Ask has a full command token plus trailing space or arguments.

    The slash popup should stay closed so Enter runs Send, not ``accept_selected``
    (QA B2: ``/tone very formal`` must not lose the argument to a usage line).
    """
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return False
    parts = stripped.split(maxsplit=1)
    token = parts[0].lstrip("/").lower()
    if token not in _registry_map():
        return False
    if len(parts) > 1:
        return True
    suffix = stripped[len(parts[0]) :]
    return suffix.startswith((" ", "\t"))


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
    """Prefix-filter ``commands`` and rank by LRU then registry order.

    ``typed`` is raw Ask-box text (``/he``). Recently used names float to
    the top when the filter is empty and as a tie-break among equal
    prefix matches.
    """
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


def _popup_shows_usage_hint(cmd: SlashCommand) -> bool:
    return command_usage_hint(cmd) != ("/%s" % cmd.name)


def format_slash_item(cmd: SlashCommand) -> str:
    tag = " [mock]" if cmd in MOCK_SLASH_COMMANDS else ""
    desc = _(cmd.description)
    if _popup_shows_usage_hint(cmd):
        return "/%s%s — %s (%s)" % (cmd.name, tag, desc, command_usage_hint(cmd))
    return "/%s%s — %s" % (cmd.name, tag, desc)


_POPUP_DESC_MAX = 52


def format_slash_popup_item(cmd: SlashCommand) -> str:
    """Single-line list row without usage hints (avoids horizontal scroll, QA B7)."""
    tag = " [mock]" if cmd in MOCK_SLASH_COMMANDS else ""
    desc = _(cmd.description)
    if len(desc) > _POPUP_DESC_MAX:
        desc = desc[: _POPUP_DESC_MAX - 1] + "\u2026"
    return "/%s%s — %s" % (cmd.name, tag, desc)


def format_help_text() -> str:
    lines = [_("Slash commands:")]
    for cmd in registered_slash_commands():
        lines.append("  " + format_slash_item(cmd))
    return "\n".join(lines)


def format_usage_line(cmd: SlashCommand) -> str:
    return _("Usage: {0}").format(command_usage_hint(cmd))


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
    arg_text = (arg or "").strip()
    if "{arg}" in base:
        # Bugfix (QA N6): "/translate Spanish: Hello." plus a template period after {arg} became "..".
        # Strip at most one trailing char when it duplicates punctuation immediately after {arg}.
        punct_after = re.search(r"\{arg\}([.!?])", base)
        if arg_text and punct_after and arg_text.endswith(punct_after.group(1)):
            arg_text = arg_text[:-1]
        return base.format(arg=arg_text)
    if arg_text:
        return base + " " + _("Additional instruction: {0}").format(arg_text)
    return base


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


def _writer_selection_explain_block(model: Any) -> str | None:
    """Selected Writer text for /explain; None when nothing is selected."""
    if not _writer_has_selection(model):
        return None
    from plugin.doc.text_helpers import get_selection_text, get_string_without_tracked_deletions, selected_text_range

    controller = model.getCurrentController()
    if controller is None:
        return None
    rng = selected_text_range(controller)
    if rng is not None:
        text = get_string_without_tracked_deletions(rng).strip()
    else:
        text = (get_selection_text(model) or "").strip()
    if not text:
        return None
    return _("Selected text:\n{0}").format(text)


def _calc_active_cell_explain_line(model: Any) -> str:
    """One short line for Calc /explain: cell address and formula text."""
    from plugin.calc.address_utils import index_to_column

    view = model.CurrentController
    sheet = view.ActiveSheet
    selection = view.getSelection()
    addr = selection.getCellAddress()
    cell = sheet.getCellByPosition(addr.Column, addr.Row)
    formula = str(cell.getFormula() or "")
    address = "%s%d" % (index_to_column(addr.Column), addr.Row + 1)
    if formula:
        return _("Cell {0} formula: {1}").format(address, formula)
    display = str(cell.getString() or cell.getValue())
    return _("Cell {0} value: {1}").format(address, display)


def slash_answer_is_local_only(cmd: SlashCommand, model: Any | None) -> bool:
    """Writer /translate with no selection: sidebar answer only, no edit tools on the wire."""
    return (
        cmd.name == "translate"
        and cmd.run is SlashRun.REWRITE
        and model is not None
        and get_document_type(model) == DocumentType.WRITER
        and not _writer_has_selection(model)
    )


def effective_slash_run(cmd: SlashCommand, model: Any | None) -> SlashRun:
    if slash_answer_is_local_only(cmd, model):
        return SlashRun.ANSWER
    return cmd.run


def plan_slash(cmd: SlashCommand, host: SlashHost) -> SlashRun:
    """Resolved run for routing (Send turn vs local handler vs rewrite)."""
    if cmd.run is not SlashRun.REWRITE:
        return cmd.run
    return effective_slash_run(cmd, host.slash_document_model())


def slash_popup_runs_now(cmd: SlashCommand) -> bool:
    return cmd.run is SlashRun.LOCAL and not cmd.requires_arg


def slash_continues_send(cmd: SlashCommand, host: SlashHost) -> bool:
    """True when Send must call ``_begin_send`` before finishing the command."""
    return plan_slash(cmd, host) in _SEND_TURN_RUNS


def build_slash_answer_query(cmd: SlashCommand, arg: str, model: Any) -> str:
    """Instruction-only user message; document snapshot is appended at send time."""
    instruction = _format_instruction(cmd, arg)
    if cmd.name == "explain":
        doc_type = get_document_type(model)
        if doc_type == DocumentType.CALC:
            return instruction + "\n\n" + _calc_active_cell_explain_line(model)
        if doc_type == DocumentType.WRITER:
            block = _writer_selection_explain_block(model)
            if block is not None:
                return instruction + "\n\n" + block
    return instruction


def _peek_slash_from_ask(host: SlashHost) -> tuple[SlashCommand, str] | None:
    query = host.query_control
    if query is None or not query.getModel():
        return None
    from plugin.chatbot.dialogs import get_control_text

    peek = (get_control_text(query) or "").strip()
    parsed = parse_slash_text(peek)
    if parsed is None:
        return None
    return parsed


def try_dispatch_slash_on_send_click(host: SlashHost) -> bool:
    """Run a local slash command without ``SEND_CLICKED`` / a send turn.

    Typed Send and Enter call this before the FSM enters BUSY so ``/undo`` is
    not blocked by ``slash_agent_edit_in_flight``. While a tool loop runs,
    only ``_SLASH_SAFE_WHILE_BUSY`` commands are handled (Send is disabled).
    """
    parsed = _peek_slash_from_ask(host)
    if parsed is None:
        return False
    cmd, arg = parsed
    busy = host.sidebar_state.send.is_busy
    if busy:
        if cmd.name not in _SLASH_SAFE_WHILE_BUSY:
            return False
        return run_slash_command(cmd, host, arg, from_popup=False) is SlashResult.HANDLED
    if slash_continues_send(cmd, host):
        return False
    return run_slash_command(cmd, host, arg, from_popup=False) is SlashResult.HANDLED


def tool_schemas_for_slash_compact(host: SlashHost, ctx: Any, session: Any) -> list[Any]:
    """OpenAI tool schemas for ``/compact`` — same shape as the main tool loop."""
    from plugin.main import get_tools

    model = host.slash_document_model()
    doc_type_str = host.cached_doc_type or "writer"
    active_domain = getattr(session, "active_specialized_domain", None)
    return get_tools().get_schemas(
        "openai",
        doc_type=doc_type_str,
        uno_services_supported=host.cached_uno_services,
        active_domain=active_domain,
        ctx=ctx,
        doc=model,
    )


def prepare_slash_compact_on_ui_thread(
    host: SlashHost, ctx: Any, session: Any
) -> tuple[Any | None, list[Any]]:
    """Resolve the LLM client and tool schemas on the UI thread before the worker starts.

    ``create_validated_client`` may show a config ``msgbox`` (UNO); schemas need the
    document model. ``run_blocking_in_thread`` holds SolarMutex while it waits, so UNO
    on the worker can deadlock — pass only plain ``client`` and ``tools`` into the worker.
    """
    from plugin.chatbot.selection import create_validated_client

    title = _("WriterAgent")
    client = create_validated_client(ctx, title)
    if client is None:
        return None, []
    tools = tool_schemas_for_slash_compact(host, ctx, session)
    return client, tools


def _remember_key(fact: str) -> str:
    digest = hashlib.sha256(fact.strip().encode("utf-8")).hexdigest()[:12]
    return "slash." + digest


def _complete_ask_draft(host: SlashHost, name: str) -> None:
    from plugin.chatbot.dialogs import set_control_text
    from plugin.chatbot.send_state import SendEvent, SendEventKind

    query = host.query_control
    if query is not None:
        set_control_text(query, "/" + name + " ")
    host.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, has_text=True))


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


def _commit_slash_command(cmd: SlashCommand, host: SlashHost) -> None:
    record_slash_lru(cmd.name)
    host.hide_popup()


def run_slash_command(
    cmd: SlashCommand,
    host: SlashHost,
    arg: str = "",
    *,
    from_popup: bool = False,
) -> SlashResult:
    """Run a registry command.

    Returns ``CONTINUE_SEND`` when Send must continue after ``_begin_send``
    (see ``plan_slash`` and ``_SEND_TURN_RUNS``). Returns ``HANDLED`` when the
    command is fully handled (including usage lines and rewrites).
    """
    if from_popup and not slash_popup_runs_now(cmd):
        _commit_slash_command(cmd, host)
        _complete_ask_draft(host, cmd.name)
        return SlashResult.HANDLED

    if cmd.requires_arg and not (arg or "").strip():
        _commit_slash_command(cmd, host)
        host.append_output("\n" + format_usage_line(cmd) + "\n")
        return SlashResult.HANDLED

    planned = plan_slash(cmd, host)

    if planned in _SEND_TURN_RUNS:
        return SlashResult.CONTINUE_SEND

    if planned is SlashRun.REWRITE:
        _commit_slash_command(cmd, host)
        _run_rewrite(host, cmd, arg)
        return SlashResult.HANDLED

    if cmd.handler is not None:
        _commit_slash_command(cmd, host)
        cmd.handler(host, cmd, arg)
        return SlashResult.HANDLED

    raise RuntimeError("slash command %r has no handler" % (cmd.name,))


def run_slash_compact(host: SlashHost, focus: str, *, cancel: Any | None = None) -> None:
    from plugin.framework.blocking_wait import run_blocking_in_thread

    if host.sidebar_state.tool_loop is not None:
        host.append_output("\n" + _("Wait for the current reply to finish before /compact.") + "\n")
        return
    if cancel is None:
        cancel = host._send_cancellation
    host._set_status(_("Thinking..."))
    # Resolve UNO here: the caller holds SolarMutex while it waits for the worker
    # (run_blocking_in_thread with pump_idle), so UNO on the worker can deadlock.
    client, tools = prepare_slash_compact_on_ui_thread(host, host.ctx, host.session)
    if client is None:
        host.append_output("\n" + _("Configure an API key to compact.") + "\n")
        return

    def worker() -> str:
        return run_slash_compact_sync(
            focus=focus,
            session=host.session,
            cancel=cancel,
            client=client,
            tools=tools,
        )

    line = run_blocking_in_thread(host.ctx, worker, pump_idle=True)
    host.append_output(line)


def run_slash_compact_sync(
    *,
    focus: str,
    session: Any,
    cancel: Any,
    client: Any,
    tools: list[Any],
) -> str:
    """Run ``compact_session(force=True)`` off-thread; return one status line.

    ``client`` and ``tools`` must come from ``prepare_slash_compact_on_ui_thread`` on
    the UI thread; this worker must not touch UNO, ``msgbox``, or the document.
    """
    from plugin.chatbot.compaction import compact_session
    from plugin.framework.config import get_config
    from plugin.framework.client.model_fetcher import resolve_context_window
    from plugin.framework.llm_lanes import llm_request_lane
    from plugin.framework.send_cancellation import is_cancelled

    if not get_config("chat_compaction_enabled"):
        return "\n" + _("Chat compaction is disabled in settings.") + "\n"

    def status_cb(text: str) -> None:
        log.debug("slash compact status: %s", text)

    if is_cancelled(cancel):
        return "\n" + _("Compaction cancelled.") + "\n"

    try:
        with llm_request_lane(status_callback=status_cb, resume_status=_("Thinking...")):
            result = compact_session(
                session,
                client,
                window=resolve_context_window(client),
                tools=tools,
                force=True,
                focus=focus or None,
                cancel=cancel,
            )
    except Exception:
        log.exception("slash /compact failed")
        return "\n" + _("Compaction failed.") + "\n"

    if is_cancelled(cancel):
        return "\n" + _("Compaction cancelled.") + "\n"
    return "\n" + _compact_result_line(result) + "\n"
