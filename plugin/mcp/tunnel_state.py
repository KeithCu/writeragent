# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Pure state machine and contracts for MCP public tunnel lifecycle and reconnection."""

from __future__ import annotations

import dataclasses
import math
from enum import Enum, auto
from typing import Any, List, Optional

from plugin.framework.deal_shim import DEAL_MAX_BACKOFF, DEAL_MAX_BACKOFF_FACTOR, DEAL_MAX_RETRY, deal
from plugin.framework.service import BaseState, FsmTransition

DEFAULT_INITIAL_BACKOFF: float = 1.0
DEFAULT_BACKOFF_FACTOR: float = 2.0
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_MAX_BACKOFF: float = 30.0


# ── States ────────────────────────────────────────────────────────────


class TunnelStatus(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class TunnelState(BaseState):
    status: TunnelStatus = TunnelStatus.STOPPED
    port: int = 18765
    provider: str = "cloudflare"
    provider_token: str = ""
    public_url: Optional[str] = None
    retry_count: int = 0
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_initial: float = DEFAULT_INITIAL_BACKOFF
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR
    max_backoff: float = DEFAULT_MAX_BACKOFF
    last_error: Optional[str] = None
    desired_running: bool = False


# ── Events ────────────────────────────────────────────────────────────


class TunnelEventKind(Enum):
    START_REQUESTED = auto()
    PROCESS_STARTED = auto()
    URL_ACQUIRED = auto()
    PROCESS_EXITED = auto()
    RETRY_TIMER_EXPIRED = auto()
    STOP_REQUESTED = auto()


@dataclasses.dataclass(frozen=True)
class TunnelEvent:
    kind: TunnelEventKind
    data: dict[str, Any] = dataclasses.field(default_factory=dict)


# ── Effects ───────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class StartProcessEffect:
    port: int
    provider: str
    provider_token: str


@dataclasses.dataclass(frozen=True)
class TerminateProcessEffect:
    pass


@dataclasses.dataclass(frozen=True)
class ScheduleRetryTimerEffect:
    delay_seconds: float
    attempt: int
    max_retries: int


@dataclasses.dataclass(frozen=True)
class CancelRetryTimerEffect:
    pass


@dataclasses.dataclass(frozen=True)
class NotifyUrlAcquiredEffect:
    provider: str
    url: str


# ── Pure Backoff Calculator ───────────────────────────────────────────


@deal.pre(
    lambda retry_count, initial=DEFAULT_INITIAL_BACKOFF, factor=DEFAULT_BACKOFF_FACTOR, max_backoff=DEFAULT_MAX_BACKOFF: (
        isinstance(retry_count, int)
        and 0 <= retry_count <= DEAL_MAX_RETRY
        and isinstance(initial, (int, float))
        and math.isfinite(initial)
        and 0 <= initial <= DEAL_MAX_BACKOFF
        and isinstance(factor, (int, float))
        and math.isfinite(factor)
        and 1.0 <= factor <= DEAL_MAX_BACKOFF_FACTOR
        and isinstance(max_backoff, (int, float))
        and math.isfinite(max_backoff)
        and initial <= max_backoff <= DEAL_MAX_BACKOFF
    )
)
@deal.post(lambda result: result >= 0)
def compute_backoff_delay(
    retry_count: int,
    initial: float = DEFAULT_INITIAL_BACKOFF,
    factor: float = DEFAULT_BACKOFF_FACTOR,
    max_backoff: float = DEFAULT_MAX_BACKOFF,
) -> float:
    """Compute exponential backoff delay capped at max_backoff."""
    delay = float(initial) * (float(factor) ** retry_count)
    return min(delay, float(max_backoff))


# ── Pure State Transition Function ────────────────────────────────────


@deal.ensure(
    lambda state, event, result: (
        event.kind != TunnelEventKind.STOP_REQUESTED
        or (
            result.state.status == TunnelStatus.STOPPED
            and not result.state.desired_running
            and any(isinstance(e, CancelRetryTimerEffect) for e in result.effects)
        )
    )
)
@deal.ensure(
    lambda state, event, result: (
        event.kind != TunnelEventKind.URL_ACQUIRED
        or (
            result.state.status == TunnelStatus.CONNECTED
            and result.state.retry_count == 0
            and any(isinstance(e, NotifyUrlAcquiredEffect) for e in result.effects)
        )
    )
)
@deal.ensure(
    lambda state, event, result: (
        event.kind != TunnelEventKind.PROCESS_EXITED
        or result.state.status != TunnelStatus.RECONNECTING
        or (
            result.state.desired_running
            and any(isinstance(e, ScheduleRetryTimerEffect) for e in result.effects)
        )
    )
)
def next_state(state: TunnelState, event: TunnelEvent) -> FsmTransition[TunnelState]:
    """Pure transition function for the MCP tunnel lifecycle and reconnection."""
    effects: List[Any] = []

    if event.kind == TunnelEventKind.START_REQUESTED:
        port = int(event.data.get("port", state.port))
        provider = str(event.data.get("provider", state.provider)).strip().lower()
        provider_token = str(event.data.get("provider_token", ""))
        max_retries = int(event.data.get("max_retries", state.max_retries))

        # Cancel any previous timer / process if re-starting
        effects.append(CancelRetryTimerEffect())
        effects.append(TerminateProcessEffect())

        effects.append(StartProcessEffect(port=port, provider=provider, provider_token=provider_token))
        new_state = dataclasses.replace(
            state,
            status=TunnelStatus.STARTING,
            port=port,
            provider=provider,
            provider_token=provider_token,
            public_url=None,
            retry_count=0,
            max_retries=max_retries,
            last_error=None,
            desired_running=True,
        )
        return FsmTransition(new_state, effects)

    elif event.kind == TunnelEventKind.PROCESS_STARTED:
        new_state = dataclasses.replace(state, status=TunnelStatus.STARTING)
        return FsmTransition(new_state, effects)

    elif event.kind == TunnelEventKind.URL_ACQUIRED:
        url = str(event.data.get("url", "")).strip()
        effects.append(NotifyUrlAcquiredEffect(provider=state.provider, url=url))
        new_state = dataclasses.replace(
            state,
            status=TunnelStatus.CONNECTED,
            public_url=url,
            retry_count=0,
            last_error=None,
        )
        return FsmTransition(new_state, effects)

    elif event.kind == TunnelEventKind.PROCESS_EXITED:
        rc = event.data.get("rc", 0)
        auth_error = event.data.get("auth_error")
        had_url = state.public_url is not None

        # If user stopped the tunnel, ignore unexpected exit handling
        if not state.desired_running:
            new_state = dataclasses.replace(
                state,
                status=TunnelStatus.STOPPED,
                public_url=None,
            )
            return FsmTransition(new_state, effects)

        # Fatal errors: auth error or binary missing — do not retry
        if auth_error:
            new_state = dataclasses.replace(
                state,
                status=TunnelStatus.FAILED,
                public_url=None,
                last_error=auth_error,
                desired_running=False,
            )
            return FsmTransition(new_state, effects)

        # Non-auth process drop: check if retries remain
        if state.retry_count < state.max_retries:
            attempt = state.retry_count + 1
            delay = compute_backoff_delay(
                retry_count=state.retry_count,
                initial=state.backoff_initial,
                factor=state.backoff_factor,
                max_backoff=state.max_backoff,
            )
            effects.append(
                ScheduleRetryTimerEffect(
                    delay_seconds=delay,
                    attempt=attempt,
                    max_retries=state.max_retries,
                )
            )
            reason = "connection dropped (code %s)" % rc if had_url else "tunnel process exited (code %s)" % rc
            err_msg = "%s; reconnecting (attempt %s/%s in %.1fs)…" % (
                reason,
                attempt,
                state.max_retries,
                delay,
            )
            new_state = dataclasses.replace(
                state,
                status=TunnelStatus.RECONNECTING,
                public_url=None,
                retry_count=attempt,
                last_error=err_msg,
            )
            return FsmTransition(new_state, effects)
        else:
            # Max retries exhausted
            err_msg = "tunnel disconnected; failed to reconnect after %s attempts (code %s)" % (
                state.max_retries,
                rc,
            )
            new_state = dataclasses.replace(
                state,
                status=TunnelStatus.FAILED,
                public_url=None,
                last_error=err_msg,
                desired_running=False,
            )
            return FsmTransition(new_state, effects)

    elif event.kind == TunnelEventKind.RETRY_TIMER_EXPIRED:
        if not state.desired_running:
            new_state = dataclasses.replace(state, status=TunnelStatus.STOPPED)
            return FsmTransition(new_state, effects)
        effects.append(
            StartProcessEffect(
                port=state.port,
                provider=state.provider,
                provider_token=state.provider_token,
            )
        )
        new_state = dataclasses.replace(state, status=TunnelStatus.STARTING)
        return FsmTransition(new_state, effects)

    elif event.kind == TunnelEventKind.STOP_REQUESTED:
        effects.append(CancelRetryTimerEffect())
        effects.append(TerminateProcessEffect())
        new_state = dataclasses.replace(
            state,
            status=TunnelStatus.STOPPED,
            public_url=None,
            retry_count=0,
            last_error=None,
            desired_running=False,
        )
        return FsmTransition(new_state, effects)

    return FsmTransition(state, effects)
