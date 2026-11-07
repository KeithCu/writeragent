# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""DEBUG observability and sidebar status helpers for the grammar worker."""

from __future__ import annotations

import logging
import threading
from typing import Any, cast

from plugin.framework.event_bus import get_event_bus
from plugin.framework.uno_context import desktop_create_is_unsafe

log = logging.getLogger("writeragent.grammar")


def grammar_obs(event: str, **fields: Any) -> None:
    """DEBUG-only observability for queue / worker (grep ``[grammar] obs`` in logs)."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.debug("[grammar] obs %s %s", event, kv)


def slice_preview_debug(text: str, max_len: int = 72) -> str:
    """Compact one-line preview for DEBUG logs (avoid dumping huge paragraphs)."""
    if not text:
        return ""
    chars: list[str] = []
    in_whitespace = False
    started = False
    limit = max_len + 1
    for ch in text:
        if ch.isspace():
            if started and not in_whitespace:
                chars.append(" ")
                in_whitespace = True
                if len(chars) == limit:
                    break
        else:
            started = True
            in_whitespace = False
            chars.append(ch)
            if len(chars) == limit:
                break
    if not chars:
        return ""
    if len(chars) > max_len:
        return f"{''.join(chars[:max_len])}\u2026"
    if chars[-1] == " ":
        chars.pop()
    return "".join(chars)


_TERMINAL_STATUS_PHASES = ("done", "failed")


def _frame_from_controller(obj: Any) -> Any | None:
    if obj is None:
        return None
    try:
        controller = obj.getCurrentController()
        if controller is None:
            return None
        return cast("Any", controller).getFrame()
    except Exception:
        return None


def _frame_from_bound_grammar_docs() -> Any | None:
    """Frame from a Writer model already bound by proofreading — no Desktop create."""
    from plugin.writer.locale.grammar_persistence import grammar_registry

    with grammar_registry.lock:
        persistences = list(grammar_registry.doc_persistence_instances.values())
    for persistence in persistences:
        frame = _frame_from_controller(persistence._model)
        if frame is not None:
            return frame
    return None


def _frame_from_existing_desktop_singleton(ctx: Any) -> Any | None:
    """Look up ``theDesktop`` only when it cannot SEGV; never create Desktop.

    Register-time uno.bin has no VCL. ``get_desktop`` fail-softs there, but
    ``getValueByName(theDesktop)`` can still instantiate and SEGV (#768).
    Skip singleton lookup on helper ctxs.
    No existing Desktop → fail soft (status paint is optional).
    """
    if ctx is None or desktop_create_is_unsafe():
        return None
    try:
        desktop = cast("Any", ctx.getValueByName("/singletons/com.sun.star.frame.theDesktop"))
        if desktop is None:
            return None
        frame = desktop.getCurrentFrame()
        if frame is not None:
            return frame
        component = desktop.getCurrentComponent()
        return _frame_from_controller(component) if component is not None else None
    except Exception:
        return None


def resolve_status_bar_frame(ctx: Any) -> Any | None:
    """Resolve a frame for XStatusIndicator without creating Desktop.

    Prefer a bound proofreading document. If none, use an already-live Desktop
    singleton in GUI soffice. Missing frame → skip paint (do not call
    ``get_desktop`` / ``get_active_document``).
    """
    frame = _frame_from_bound_grammar_docs()
    if frame is not None:
        return frame
    return _frame_from_existing_desktop_singleton(ctx)


class GrammarStatus:
    """Base sink for grammar proofreading status updates."""

    _lock: threading.Lock
    _status_inflight: int
    _pending_done: tuple[str, dict[str, Any]] | None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status_inflight = 0
        self._pending_done = None

    def _deliver(
        self,
        phase: str,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
        routine: bool = False,
    ) -> None:
        """Deliver a status update. ``routine=True`` marks steady-state per-keystroke text; the
        LibreHarper status-bar sink must not paint it unless the phase is terminal and an
        indicator is already active (issue #768).
        """
        pass

    def start(self, text: str, *, result: str = "", routine: bool = False) -> None:
        self._deliver("start", text, result=result, routine=routine)

    def request(
        self,
        text: str,
        *,
        result: str = "",
        preview_source: str | None = None,
        routine: bool = False,
    ) -> None:
        self._deliver("request", text, result=result, preview_source=preview_source, routine=routine)

    def done(
        self,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
        routine: bool = False,
    ) -> None:
        self._deliver(
            "done",
            text,
            result=result,
            elapsed_ms=elapsed_ms,
            preview_source=preview_source,
            length_hint=length_hint,
            routine=routine,
        )

    def failed(self, text: str, *, result: str = "") -> None:
        self._deliver("failed", text, result=result)

    def relay_status_update(
        self,
        phase: str,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
        routine: bool = False,
    ) -> None:
        """Deliver one status phase without touching begin/end cycle counters (composite sinks)."""
        self._deliver(
            phase,
            text,
            result=result,
            elapsed_ms=elapsed_ms,
            preview_source=preview_source,
            length_hint=length_hint,
            routine=routine,
        )

    def begin_status_cycle(self) -> None:
        """Mark one batch in flight (sidebar/status-bar done is deferred)."""
        with self._lock:
            self._status_inflight += 1

    def record_done_status(
        self,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
        routine: bool = False,
    ) -> None:
        """Remember the latest chunk result; emitted when the last in-flight batch finishes."""
        with self._lock:
            self._pending_done = (
                text,
                {
                    "result": result,
                    "elapsed_ms": elapsed_ms,
                    "preview_source": preview_source,
                    "length_hint": length_hint,
                    "routine": routine,
                },
            )

    def end_status_cycle(self) -> None:
        """Drop in-flight count; emit one terminal done when all parallel batches finish."""
        pending: tuple[str, dict[str, Any]] | None = None
        with self._lock:
            had_cycle = self._status_inflight > 0
            if had_cycle:
                self._status_inflight -= 1
            last = self._status_inflight == 0
            if last:
                pending = self._pending_done
                self._pending_done = None
        if pending is not None:
            text, kw = pending
            self.done(text, **kw)
        elif had_cycle and last:
            self.done("batch", result="skipped")


class SidebarGrammarStatus(GrammarStatus):
    """WriterAgent: relay status to the sidebar event bus."""

    def _deliver(
        self,
        phase: str,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
        routine: bool = False,
    ) -> None:
        try:
            if preview_source is not None:
                raw = preview_source.strip() or "(empty)"
                preview = slice_preview_debug(raw, 10)
                length = len(raw) if length_hint is None else length_hint
            else:
                preview = slice_preview_debug(text.strip() or "(empty)", 10)
                length = len(text)
            get_event_bus().emit(
                "grammar:status",
                phase=phase,
                preview=preview,
                length=length,
                result=result,
                elapsed_ms=elapsed_ms,
            )
        except Exception as e:
            log.debug("[grammar] sidebar status emit failed: %s", e, exc_info=True)


class StatusBarGrammarStatus(GrammarStatus):
    """LibreHarper: update the LibreOffice window status bar using XStatusIndicator."""

    _indicator: Any
    _label: str

    def __init__(self, label: str = "LibreHarper") -> None:
        super().__init__()
        self._indicator = None
        self._label = label

    def is_active(self) -> bool:
        """True when an XStatusIndicator session is open on the status bar."""
        return self._indicator is not None

    def _deliver(
        self,
        phase: str,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
        routine: bool = False,
    ) -> None:
        try:
            if routine and (
                phase not in _TERMINAL_STATUS_PHASES or self._indicator is None
            ):
                return
            if desktop_create_is_unsafe():
                return
            from plugin.framework.queue_executor import main, pump_main_thread_work_queue
            from plugin.framework.uno_context import get_ctx, process_events_to_idle

            try:
                main.post(self._paint, phase, result, routine)
            except Exception:
                log.debug("Failed to post status bar update to main thread", exc_info=True)
                return

            # The grammar worker is blocked in harper-ls, so the queued setText paints only after main drains its queue and idles VCL.
            # VCL idle pumping uses main.post, not main.call, so a blocked grammar worker
            # does not time out and abort the lint; force=False respects chat drain ownership.
            def _pump() -> None:
                pump_main_thread_work_queue(max_items=8)
                process_events_to_idle(get_ctx(), force=False)

            try:
                main.post(_pump)
            except Exception as e:
                log.warning("[grammar] Harper status UI pump skipped: %s", e)
        except Exception as e:
            log.debug("[grammar] status bar emit failed: %s", e, exc_info=True)

    def _paint(self, phase: str, result: str = "", routine: bool = False) -> None:
        """Update the LibreOffice window status bar using XStatusIndicator."""
        prefix = self._label
        msg = f"{prefix}: {result or 'Checking...'}"
        if phase == "done":
            msg = f"{prefix}: Grammar check complete"
        elif phase == "failed":
            msg = f"{prefix}: Failed ({result})" if result else f"{prefix}: Grammar check failed"

        try:
            if routine:
                if self._indicator is not None:
                    self._indicator.end()
                    self._indicator = None
                return

            from plugin.framework.uno_context import get_ctx

            frame = resolve_status_bar_frame(get_ctx())
            if frame is None:
                return

            if phase == "done":
                if self._indicator is not None:
                    self._indicator.setText(msg)
                    self._indicator.setValue(100)
                    self._indicator.end()
                    self._indicator = None
            elif self._indicator is None:
                indicator = frame.createStatusIndicator()
                if indicator is not None:
                    indicator.start(msg, 100)
                self._indicator = indicator
            else:
                self._indicator.setText(msg)
                self._indicator.setValue(100 if phase == "failed" else 50)
        except Exception as e:
            log.debug("[grammar] _paint failed: %s", e)
            self._indicator = None


def _should_paint_writeragent_status_bar(
    phase: str,
    routine: bool,
    status_bar: StatusBarGrammarStatus,
    *,
    bus_has_subscribers: bool,
) -> bool:
    """Whether WriterAgent should mirror this grammar update on the LO status bar.

    Failures always paint. When nobody listens on ``grammar:status`` (e.g. Calc
    sidebar drops the event), paint so progress is still visible. For routine
    terminal phases, paint only when a bar indicator is already active so a
    progress line can end cleanly instead of leaving a stale bar (issue #768).

    ``bus_has_subscribers`` can change before delivery; an extra or missed bar
    paint is benign.
    """
    if phase == "failed":
        return True
    if not bus_has_subscribers:
        return True
    if routine and phase in _TERMINAL_STATUS_PHASES and status_bar.is_active():
        return True
    return False


class WriterAgentGrammarStatus(GrammarStatus):
    """WriterAgent: sidebar bus plus optional status-bar fallback."""

    _sidebar: SidebarGrammarStatus
    _status_bar: StatusBarGrammarStatus

    def __init__(self) -> None:
        super().__init__()
        self._sidebar = SidebarGrammarStatus()
        self._status_bar = StatusBarGrammarStatus(label="Grammar")

    def _deliver(
        self,
        phase: str,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
        routine: bool = False,
    ) -> None:
        self._sidebar.relay_status_update(
            phase,
            text,
            result=result,
            elapsed_ms=elapsed_ms,
            preview_source=preview_source,
            length_hint=length_hint,
            routine=routine,
        )
        if _should_paint_writeragent_status_bar(
            phase,
            routine,
            self._status_bar,
            bus_has_subscribers=get_event_bus().has_subscribers("grammar:status"),
        ):
            self._status_bar.relay_status_update(
                phase,
                text,
                result=result,
                elapsed_ms=elapsed_ms,
                preview_source=preview_source,
                length_hint=length_hint,
                routine=routine,
            )


_STATUS_SINK: GrammarStatus | None = None


def get_grammar_status() -> GrammarStatus:
    """Return the active GrammarStatus sink (chosen once per product)."""
    global _STATUS_SINK
    if _STATUS_SINK is None:
        from plugin.framework.uno_context import is_libreharper

        _STATUS_SINK = StatusBarGrammarStatus() if is_libreharper() else WriterAgentGrammarStatus()
    return _STATUS_SINK


def set_grammar_status(sink: GrammarStatus | None) -> None:
    """Override or reset status sink (for tests and teardown)."""
    global _STATUS_SINK
    _STATUS_SINK = sink
