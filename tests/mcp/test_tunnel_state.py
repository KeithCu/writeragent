"""Unit and contract verification tests for the MCP Tunnel pure FSM (tunnel_state.py)."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import deal
from plugin.framework.deal_shim import DEAL_MAX_RETRY
from plugin.mcp.tunnel_state import (
    CancelRetryTimerEffect,
    NotifyUrlAcquiredEffect,
    ScheduleRetryTimerEffect,
    StartProcessEffect,
    TerminateProcessEffect,
    TunnelEvent,
    TunnelEventKind,
    TunnelState,
    TunnelStatus,
    compute_backoff_delay,
    next_state,
)
from tests.strip_bundle import deal_pre_present


def test_compute_backoff_delay_exponential_progression():
    assert compute_backoff_delay(0) == 1.0
    assert compute_backoff_delay(1) == 2.0
    assert compute_backoff_delay(2) == 4.0
    assert compute_backoff_delay(3) == 8.0
    assert compute_backoff_delay(4) == 16.0
    assert compute_backoff_delay(5) == 30.0  # Capped at max_backoff (30.0)
    assert compute_backoff_delay(DEAL_MAX_RETRY) == 30.0


def test_compute_backoff_delay_overflow_pre_fails_closed():
    if not deal_pre_present(compute_backoff_delay):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        compute_backoff_delay(DEAL_MAX_RETRY + 1)


def test_compute_backoff_delay_custom_parameters():
    assert compute_backoff_delay(0, initial=0.5, factor=3.0, max_backoff=10.0) == 0.5
    assert compute_backoff_delay(1, initial=0.5, factor=3.0, max_backoff=10.0) == 1.5
    assert compute_backoff_delay(2, initial=0.5, factor=3.0, max_backoff=10.0) == 4.5
    assert compute_backoff_delay(3, initial=0.5, factor=3.0, max_backoff=10.0) == 10.0


def test_start_requested_transition():
    state = TunnelState()
    event = TunnelEvent(
        TunnelEventKind.START_REQUESTED,
        {"port": 19000, "provider": "ngrok", "provider_token": "secret-tok", "max_retries": 4},
    )
    tr = next_state(state, event)

    assert tr.state.status == TunnelStatus.STARTING
    assert tr.state.port == 19000
    assert tr.state.provider == "ngrok"
    assert tr.state.provider_token == "secret-tok"
    assert tr.state.max_retries == 4
    assert tr.state.desired_running is True
    assert tr.state.retry_count == 0
    assert tr.state.last_error is None

    effects = tr.effects
    assert any(isinstance(e, CancelRetryTimerEffect) for e in effects)
    assert any(isinstance(e, TerminateProcessEffect) for e in effects)
    start_eff = [e for e in effects if isinstance(e, StartProcessEffect)][0]
    assert start_eff.port == 19000
    assert start_eff.provider == "ngrok"
    assert start_eff.provider_token == "secret-tok"


def test_url_acquired_transition():
    state = TunnelState(status=TunnelStatus.STARTING, provider="cloudflare", desired_running=True, retry_count=3)
    event = TunnelEvent(TunnelEventKind.URL_ACQUIRED, {"url": "https://quick.trycloudflare.com"})
    tr = next_state(state, event)

    assert tr.state.status == TunnelStatus.CONNECTED
    assert tr.state.public_url == "https://quick.trycloudflare.com"
    assert tr.state.retry_count == 0
    assert tr.state.last_error is None

    notify_eff = [e for e in tr.effects if isinstance(e, NotifyUrlAcquiredEffect)][0]
    assert notify_eff.provider == "cloudflare"
    assert notify_eff.url == "https://quick.trycloudflare.com"


def test_process_exited_triggers_exponential_backoff_reconnect():
    state = TunnelState(
        status=TunnelStatus.CONNECTED,
        provider="cloudflare",
        public_url="https://quick.trycloudflare.com",
        desired_running=True,
        retry_count=0,
        max_retries=5,
    )
    event = TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1})
    tr = next_state(state, event)

    assert tr.state.status == TunnelStatus.RECONNECTING
    assert tr.state.public_url is None
    assert tr.state.retry_count == 1
    assert "reconnecting (attempt 1/5 in 1.0s)" in (tr.state.last_error or "")

    sched_eff = [e for e in tr.effects if isinstance(e, ScheduleRetryTimerEffect)][0]
    assert sched_eff.delay_seconds == 1.0
    assert sched_eff.attempt == 1
    assert sched_eff.max_retries == 5


def test_process_exited_sequential_backoff_steps():
    state = TunnelState(
        status=TunnelStatus.STARTING,
        provider="bore",
        desired_running=True,
        retry_count=1,
        max_retries=5,
    )
    # Second retry attempt -> delay 2.0s
    tr = next_state(state, TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1}))
    assert tr.state.status == TunnelStatus.RECONNECTING
    assert tr.state.retry_count == 2
    sched_eff = [e for e in tr.effects if isinstance(e, ScheduleRetryTimerEffect)][0]
    assert sched_eff.delay_seconds == 2.0

    # Third retry attempt -> delay 4.0s
    tr2 = next_state(tr.state, TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 1}))
    assert tr2.state.status == TunnelStatus.RECONNECTING
    assert tr2.state.retry_count == 3
    sched_eff2 = [e for e in tr2.effects if isinstance(e, ScheduleRetryTimerEffect)][0]
    assert sched_eff2.delay_seconds == 4.0


def test_process_exited_exhausts_max_retries():
    state = TunnelState(
        status=TunnelStatus.STARTING,
        provider="ngrok",
        desired_running=True,
        retry_count=4,
        max_retries=4,
    )
    event = TunnelEvent(TunnelEventKind.PROCESS_EXITED, {"rc": 2})
    tr = next_state(state, event)

    assert tr.state.status == TunnelStatus.FAILED
    assert tr.state.desired_running is False
    assert "failed to reconnect after 4 attempts (code 2)" in (tr.state.last_error or "")
    assert not any(isinstance(e, ScheduleRetryTimerEffect) for e in tr.effects)


def test_process_exited_auth_error_fails_immediately_without_retry():
    state = TunnelState(
        status=TunnelStatus.STARTING,
        provider="ngrok",
        desired_running=True,
        retry_count=0,
        max_retries=5,
    )
    event = TunnelEvent(
        TunnelEventKind.PROCESS_EXITED,
        {"rc": 1, "auth_error": "ngrok authtoken required or invalid"},
    )
    tr = next_state(state, event)

    assert tr.state.status == TunnelStatus.FAILED
    assert tr.state.desired_running is False
    assert tr.state.last_error == "ngrok authtoken required or invalid"
    assert not any(isinstance(e, ScheduleRetryTimerEffect) for e in tr.effects)


def test_retry_timer_expired_starts_process_when_desired_running():
    state = TunnelState(
        status=TunnelStatus.RECONNECTING,
        port=18765,
        provider="cloudflare",
        provider_token="cf-tok",
        desired_running=True,
        retry_count=2,
    )
    event = TunnelEvent(TunnelEventKind.RETRY_TIMER_EXPIRED)
    tr = next_state(state, event)

    assert tr.state.status == TunnelStatus.STARTING
    start_eff = [e for e in tr.effects if isinstance(e, StartProcessEffect)][0]
    assert start_eff.port == 18765
    assert start_eff.provider == "cloudflare"
    assert start_eff.provider_token == "cf-tok"


def test_retry_timer_expired_ignored_when_not_desired_running():
    state = TunnelState(
        status=TunnelStatus.STOPPED,
        desired_running=False,
    )
    event = TunnelEvent(TunnelEventKind.RETRY_TIMER_EXPIRED)
    tr = next_state(state, event)

    assert tr.state.status == TunnelStatus.STOPPED
    assert len(tr.effects) == 0


def test_stop_requested_cleans_up_from_any_state():
    for initial_status in (
        TunnelStatus.STARTING,
        TunnelStatus.CONNECTED,
        TunnelStatus.RECONNECTING,
        TunnelStatus.FAILED,
    ):
        state = TunnelState(
            status=initial_status,
            public_url="https://active.example.com",
            desired_running=True,
            retry_count=3,
        )
        event = TunnelEvent(TunnelEventKind.STOP_REQUESTED)
        tr = next_state(state, event)

        assert tr.state.status == TunnelStatus.STOPPED
        assert tr.state.desired_running is False
        assert tr.state.public_url is None
        assert tr.state.retry_count == 0
        assert tr.state.last_error is None
        assert any(isinstance(e, CancelRetryTimerEffect) for e in tr.effects)
        assert any(isinstance(e, TerminateProcessEffect) for e in tr.effects)


# ── Hypothesis Property-Based Verification ─────────────────────────────


@given(
    retry_count=st.integers(min_value=0, max_value=DEAL_MAX_RETRY),
    initial=st.floats(min_value=0.1, max_value=10.0),
    factor=st.floats(min_value=1.0, max_value=5.0),
    max_backoff=st.floats(min_value=10.0, max_value=300.0),
)
@settings(max_examples=50)
def test_hypothesis_compute_backoff_delay_invariants(retry_count, initial, factor, max_backoff):
    delay = compute_backoff_delay(retry_count, initial, factor, max_backoff)
    assert 0 <= delay <= max_backoff
    if retry_count == 0:
        assert delay == min(initial, max_backoff)


@given(
    event_kind=st.sampled_from(list(TunnelEventKind)),
    current_status=st.sampled_from(list(TunnelStatus)),
    retry_count=st.integers(min_value=0, max_value=10),
    desired_running=st.booleans(),
)
@settings(max_examples=100)
def test_hypothesis_state_transition_invariants(event_kind, current_status, retry_count, desired_running):
    state = TunnelState(
        status=current_status,
        retry_count=retry_count,
        desired_running=desired_running,
    )
    event = TunnelEvent(event_kind, {"rc": 1, "url": "https://test.example.com"})
    tr = next_state(state, event)

    # Invariant 1: Stop event always results in STOPPED and not desired_running
    if event_kind == TunnelEventKind.STOP_REQUESTED:
        assert tr.state.status == TunnelStatus.STOPPED
        assert not tr.state.desired_running

    # Invariant 2: URL acquired always results in CONNECTED and retry_count == 0
    if event_kind == TunnelEventKind.URL_ACQUIRED:
        assert tr.state.status == TunnelStatus.CONNECTED
        assert tr.state.retry_count == 0

    # Invariant 3: RECONNECTING state always has desired_running == True
    if tr.state.status == TunnelStatus.RECONNECTING:
        assert tr.state.desired_running is True
