# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Low-level pacing and retry policy helpers for outbound LLM requests.

Concurrency: ``RequestPacer`` (minimum gap between sends) and
``LocalHttpsCertificateFallback`` (which local hosts retried without TLS
verify) are fields on one transport. They are not locked: each
``LlmClient`` has its own transport and typically one in-flight stream.
Chat and grammar do not share a pacer. Adding a lock here would not
protect a connection you must not share in the first place.

Backoff helpers port OpenClaw ``packages/retry`` delay math (jitter,
Retry-After floor, delay cap) without the RetrySupervisor / retryAsync
runner. After a retry wait we also remember that delay per host so later
requests (new ``LlmClient`` per job) space themselves instead of immediately
re-hitting a busy local server. OpenClaw does not persist the gap; we do.
"""

import datetime
import email.utils
import json
import logging
import math
import random
import threading
import time
from typing import Callable, Literal

from .ssl_helpers import _is_certificate_verify_error
from .provider_detection import is_local_host

log = logging.getLogger(__name__)

# Minimum wall time between consecutive HTTP sends on one client. Grammar queue
# workers also use this value to stagger parallel drains so they do not burst.
LLM_MIN_REQUEST_INTERVAL_SEC = 0.05

# Fallback delay when the provider does not send Retry-After (connection errors,
# 429/503 with no header). Copied from OpenClaw packages/retry (300ms / 30s /
# 3 attempts) — do not treat those numbers as gospel; they are small for a
# busy hosted API. Retry-After (capped at RETRY_MAX_DELAY_SEC) is what we
# actually honor when present.
RETRY_MIN_DELAY_SEC = 0.3
RETRY_MAX_DELAY_SEC = 30.0
RETRY_WAIT_CHUNK_SEC = 0.05
RETRY_MAX_ATTEMPTS = 3
RETRYABLE_HTTP_STATUS = frozenset({429, 503})


def format_retry_wait_status(delay_sec: float) -> str:
    """Short sidebar status line for the abortable retry wait."""
    from plugin.framework.i18n import _

    shown = int(round(delay_sec)) if delay_sec >= 1 else round(max(delay_sec, 0.0), 1)
    return _("Provider busy, retrying in {0}s…").format(shown)


def emit_retry_status(status_callback: Callable[[str], None] | None, delay_sec: float) -> None:
    if status_callback is None:
        return
    status_callback(format_retry_wait_status(delay_sec))


def parse_retry_after(header: str | None) -> float | None:
    """Parse Retry-After as delta-seconds or HTTP-date. Invalid/missing -> None."""
    if not header:
        return None
    text = header.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    delay = parsed.timestamp() - time.time()
    if not math.isfinite(delay):
        return None
    return delay


def _apply_jitter_ms(delay_ms: float, jitter: float | Literal["full"], mode: Literal["symmetric", "positive"], rng: Callable[[], float]) -> float:
    # OpenClaw packages/retry applyJitter: Retry-After is a lower bound, so
    # positive jitter ceils. Over-cap hints spread downward to avoid lockstep.
    if jitter == "full":
        if mode == "symmetric":
            return max(0.0, round(delay_ms * (0.5 + rng() * 0.5)))
        return max(0.0, math.ceil(delay_ms * (1.0 + rng())))
    if jitter <= 0:
        return math.ceil(delay_ms) if mode == "positive" else delay_ms
    fraction = rng()
    offset = fraction * jitter if mode == "positive" else (fraction * 2.0 - 1.0) * jitter
    raw = delay_ms * (1.0 + offset)
    return max(0.0, math.ceil(raw) if mode == "positive" else round(raw))


def backoff_delay_sec(
    *,
    attempt: int = 1,
    retry_after_sec: float | None = None,
    min_delay: float = RETRY_MIN_DELAY_SEC,
    max_delay: float = RETRY_MAX_DELAY_SEC,
    jitter: float | Literal["full"] = "full",
    random: Callable[[], float] = random.random,
) -> float:
    """Jittered delay before the next attempt. ``attempt`` is 1-based (first wait = min_delay)."""
    retry_after_value = retry_after_sec if retry_after_sec is not None and math.isfinite(retry_after_sec) else None
    if retry_after_value is not None:
        base = max(retry_after_value, min_delay)
    else:
        base = min_delay * (2 ** max(attempt - 1, 0))
    delay_cap = max_delay
    delay = min(base, delay_cap)
    can_honor = retry_after_value is not None and retry_after_value <= delay_cap
    has_retry_after = retry_after_value is not None
    wants_positive = (not has_retry_after or can_honor) if jitter == "full" else can_honor
    delay_ms = delay * 1000.0
    delay_ms = _apply_jitter_ms(delay_ms, jitter, "positive" if wants_positive else "symmetric", random)
    delay = min(max(delay_ms / 1000.0, min_delay), delay_cap)
    return delay


def wait_abortable(
    delay_sec: float,
    stop_checker: Callable[[], bool] | None = None,
    *,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    chunk_sec: float = RETRY_WAIT_CHUNK_SEC,
) -> bool:
    """Sleep ``delay_sec`` in small chunks so Stop aborts the wait. False if stopped."""
    sleeper = sleep or time.sleep
    now = monotonic or time.monotonic
    if stop_checker and stop_checker():
        return False
    if not math.isfinite(delay_sec) or delay_sec <= 0:
        return not (stop_checker and stop_checker())
    deadline = now() + delay_sec
    while True:
        if stop_checker and stop_checker():
            return False
        remaining = deadline - now()
        if remaining <= 0:
            return not (stop_checker and stop_checker())
        sleeper(min(chunk_sec, remaining))


# Process-wide spacing after a retry: keyed by host so chat, grammar, and
# Calc =PROMPT() share one cooldown. last_sent starts unset so the first
# request of a session is never delayed by a stale epoch of 0.
# OpenRouter :free / openrouter/free are 20 rpm; those jobs use ``{host}:free``
# so paid OpenRouter traffic is not slowed.
OPENROUTER_FREE_MIN_GAP_SEC = 3.0
_host_gap_lock = threading.Lock()
_host_gap_sec: dict[str, float] = {}
_host_last_sent: dict[str, float] = {}


def is_openrouter_free_model(model: str | None) -> bool:
    mid = str(model or "").strip()
    # Catalog auto-router is ``openrouter/free``; per-model free variants end in ``:free``.
    return bool(mid) and (mid.endswith(":free") or mid == "openrouter/free")


def pacing_key(host: str, model: str | None = None) -> str:
    """Host key for gap maps; ``{host}:free`` for OpenRouter free models."""
    if host and is_openrouter_free_model(model):
        return f"{host}:free"
    return host


def request_model_from_body(body: object) -> str | None:
    """Read the chat ``model`` field from a JSON request body."""
    if isinstance(body, bytes):
        raw: str | bytes = body
    elif isinstance(body, str):
        raw = body
    else:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict):
        model = payload.get("model")
        return model if isinstance(model, str) else None
    return None


def ensure_free_model_pacing(host: str, model: str | None) -> str:
    """Return the pacing key and seed the 3s :free floor when applicable."""
    key = pacing_key(host, model)
    if is_openrouter_free_model(model):
        remember_host_gap(key, OPENROUTER_FREE_MIN_GAP_SEC)
    return key


def reset_host_pacing_for_tests() -> None:
    with _host_gap_lock:
        _host_gap_sec.clear()
        _host_last_sent.clear()


def remember_host_gap(host: str, delay_sec: float) -> None:
    """Stick the retry delay as the minimum gap before the next send to *host*."""
    if not host or not math.isfinite(delay_sec) or delay_sec <= 0:
        return
    gap = min(delay_sec, RETRY_MAX_DELAY_SEC)
    updated = False
    with _host_gap_lock:
        prev = _host_gap_sec.get(host, 0.0)
        if gap > prev:
            _host_gap_sec[host] = gap
            updated = True
    if updated:
        log.debug("Host %s retry gap now %.3fs", host, gap)


def clear_host_gap(host: str) -> None:
    """Drop a learned gap after a first-try success (server is no longer busy).

    ``:free`` keys keep the 20 rpm floor so a 200 does not return to bursting.
    """
    if not host:
        return
    with _host_gap_lock:
        if host.endswith(":free"):
            _host_gap_sec[host] = OPENROUTER_FREE_MIN_GAP_SEC
        else:
            _host_gap_sec.pop(host, None)


def mark_host_sent(host: str, *, monotonic: Callable[[], float] | None = None) -> None:
    if not host:
        return
    now = (monotonic or time.monotonic)()
    with _host_gap_lock:
        _host_last_sent[host] = now


def remaining_host_gap(host: str, *, monotonic: Callable[[], float] | None = None) -> float:
    if not host:
        return 0.0
    now = (monotonic or time.monotonic)()
    with _host_gap_lock:
        gap = _host_gap_sec.get(host, 0.0)
        last = _host_last_sent.get(host)
    if gap <= 0 or last is None:
        return 0.0
    return max(0.0, gap - (now - last))


def wait_host_gap(
    host: str,
    stop_checker: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> bool:
    """Wait out a learned per-host gap. False if Stop fired."""
    remaining = remaining_host_gap(host)
    if remaining <= 0:
        return True
    emit_retry_status(status_callback, remaining)
    return wait_abortable(remaining, stop_checker)


class RequestPacer:
    """Sleep before rapid repeat sends on the same client."""

    min_interval_sec: float
    _monotonic: Callable[[], float] | None
    _sleep: Callable[[float], None] | None
    last_sent_monotonic: float

    def __init__(self, min_interval_sec: float = LLM_MIN_REQUEST_INTERVAL_SEC, *, monotonic: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self.min_interval_sec = min_interval_sec
        self._monotonic = monotonic
        self._sleep = sleep
        self.last_sent_monotonic = 0.0

    def _now(self) -> float:
        return (self._monotonic or time.monotonic)()

    def wait_before_send(self) -> None:
        """Sleep if needed so consecutive sends are not back-to-back."""
        wait = self.min_interval_sec - (self._now() - self.last_sent_monotonic)
        if wait > 0:
            (self._sleep or time.sleep)(wait)

    def mark_sent(self) -> None:
        self.last_sent_monotonic = self._now()


class LocalHttpsCertificateFallback:
    """Track local HTTPS hosts that should retry with certificate verification disabled."""

    def __init__(self) -> None:
        self._fallback_hosts: set[str] = set()

    def ssl_mode_for(self, scheme: str, host: str) -> str:
        """Return ``verified``, ``unverified``, or ``plain`` for the next connection.

        Public HTTPS is always verified. Local HTTPS starts verified and
        switches to unverified only after ``enable_if_applicable`` records a
        certificate failure (self-signed Ollama/LM Studio). Previously this
        returned ``unverified`` for every non-local host, which silently
        disabled TLS checks for OpenAI, Anthropic, OpenRouter, and similar.
        """
        if scheme != "https":
            return "plain"
        if is_local_host(host) and host in self._fallback_hosts:
            return "unverified"
        return "verified"

    def enable_if_applicable(self, host: str, err: BaseException) -> bool:
        """Enable unverified retry for a local host after certificate validation fails."""
        if not host or not is_local_host(host) or not _is_certificate_verify_error(err):
            return False
        if host in self._fallback_hosts:
            return False
        self._fallback_hosts.add(host)
        log.error("Local HTTPS certificate verification failed for %s; retrying unverified." % host)
        return True

    def has_fallback(self, host: str) -> bool:
        return host in self._fallback_hosts
