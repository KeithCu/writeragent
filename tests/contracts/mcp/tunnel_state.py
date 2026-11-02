"""Contracts sidecar for plugin.mcp.tunnel_state."""

from __future__ import annotations

import math
import deal

from plugin.mcp import tunnel_state as _prod
from plugin.mcp.tunnel_state import (
    CancelRetryTimerEffect,
    NotifyUrlAcquiredEffect,
    ScheduleRetryTimerEffect,
    TunnelEventKind,
    TunnelStatus,
)
from tests.contracts.bounds import (
    DEAL_MAX_BACKOFF,
    DEAL_MAX_BACKOFF_FACTOR,
    DEAL_MAX_RETRY,
)

compute_backoff_delay = deal.pre(
    lambda retry_count, initial=_prod.DEFAULT_INITIAL_BACKOFF, factor=_prod.DEFAULT_BACKOFF_FACTOR, max_backoff=_prod.DEFAULT_MAX_BACKOFF: (
        isinstance(retry_count, int)
        and 0 <= retry_count <= DEAL_MAX_RETRY
        and isinstance(initial, (int, float))
        and math.isfinite(initial)
        and initial >= 0
        and isinstance(factor, (int, float))
        and math.isfinite(factor)
        and 0 <= factor <= DEAL_MAX_BACKOFF_FACTOR
        and isinstance(max_backoff, (int, float))
        and math.isfinite(max_backoff)
        and initial <= max_backoff <= DEAL_MAX_BACKOFF
    )
)(
    deal.post(lambda result: result >= 0)(_prod.compute_backoff_delay)
)

next_state = deal.ensure(
    lambda state, event, result: event.kind != TunnelEventKind.STOP_REQUESTED
    or (
        result.state.status == TunnelStatus.STOPPED
        and not result.state.desired_running
        and any(isinstance(e, CancelRetryTimerEffect) for e in result.effects)
    )
)(
    deal.ensure(
        lambda state, event, result: event.kind != TunnelEventKind.URL_ACQUIRED
        or (
            result.state.status == TunnelStatus.CONNECTED
            and result.state.retry_count == state.retry_count
            and any(isinstance(e, NotifyUrlAcquiredEffect) for e in result.effects)
        )
    )(
        deal.ensure(
            lambda state, event, result: event.kind != TunnelEventKind.PROCESS_EXITED
            or result.state.status != TunnelStatus.RECONNECTING
            or (
                result.state.desired_running
                and any(isinstance(e, ScheduleRetryTimerEffect) for e in result.effects)
            )
        )(_prod.next_state)
    )
)

PYTEST = [
    "compute_backoff_delay",
    "next_state",
]

TARGETS = [
    f"{__name__}.compute_backoff_delay",
]
