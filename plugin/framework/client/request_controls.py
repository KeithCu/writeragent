# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Low-level pacing and retry policy helpers for outbound LLM requests.

Concurrency: ``RequestPacer`` (minimum gap between sends) is a field on one
transport and is not locked. Each ``LlmClient`` has its own transport.
Local HTTPS unverified fallback remembers local hosts process-wide (same
locking style as the host-gap cache) so the next client does not pay a
failed handshake for a self-signed localhost cert. Public hosts stay verified.

Backoff helpers port OpenClaw ``packages/retry`` delay math (jitter,
Retry-After floor, delay cap) without the RetrySupervisor / retryAsync
runner. After a retry wait we also remember that delay per host so later
requests (new ``LlmClient`` per job) space themselves instead of immediately
re-hitting a busy local server. OpenClaw does not persist the gap; we do.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import logging
import math
import random
import re
import threading
import time
from typing import Any, Callable

from .errors import stream_error_message
from .ssl_helpers import is_certificate_verify_error
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
# 529 is Anthropic "overloaded". A 200 SSE body that says overload is
# already retried; the HTTP status was not, so that path never ran.
RETRYABLE_HTTP_STATUS = frozenset({429, 503, 529})

# We match structured codes first and only narrow phrases here, not bare
# "too many" / "unavailable", because those also match fatal errors such as
# "too many tokens" (context overflow) or "model unavailable", which a retry
# cannot fix. The full HTTP reason phrases for 429 and 503 stay.
_OVERLOAD_TEXT_MARKERS = ("overload", "rate_limit", "rate limit", "too many requests", "service unavailable")

# Explicit HTTP status indicator (e.g. "HTTP 429", "status 503", "HTTP status 429", "error code 503").
# Bare 429 or 503 (such as "limit is 503 tokens" or "4290 tokens") must never match.
# Bugfix: previously _OVERLOAD_STATUS_RE matched any bare \b(?:429|503)\b, which caused non-overload
# errors like "limit is 503 tokens" to be wrongly identified as overload and retried until timeout.
# We fix this by checking structured error codes (including OpenRouter error.code) and explicit
# HTTP status indicators first, while the text fallback matches only phrases.
_HTTP_STATUS_OVERLOAD_RE = re.compile(
    r"\b(?:https?|status(?:\s+code)?|http\s+error|error\s+code)\s*(?:is\s*|:\s*)?(?:429|503)\b",
    re.IGNORECASE,
)


def _shown_seconds(delay_sec: float) -> int | float:
    return int(round(delay_sec)) if delay_sec >= 1 else round(max(delay_sec, 0.0), 1)


def emit_retry_status(status_callback: Callable[[str], None] | None, delay_sec: float) -> None:
    if status_callback is None:
        return
    from plugin.framework.i18n import _

    status_callback(_("Provider busy, retrying in {0}s…").format(_shown_seconds(delay_sec)))


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


def parse_retry_after_ms(header: str | None) -> float | None:
    """Parse retry-after-ms header in milliseconds as seconds. Invalid/missing/negative -> None."""
    if not header:
        return None
    text = header.strip()
    if not text:
        return None
    try:
        val = float(text)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(val) or val < 0:
        return None
    return val / 1000.0


def retry_after_exceeds_cap(retry_after_sec: float | None) -> bool:
    """True only when retry_after_sec is not None, finite, and > RETRY_MAX_DELAY_SEC."""
    return retry_after_sec is not None and math.isfinite(retry_after_sec) and retry_after_sec > RETRY_MAX_DELAY_SEC


def over_cap_retry_message(provider_message: str, retry_after_sec: float) -> str:
    from plugin.framework.i18n import _

    return provider_message + " " + _("Provider asked to wait {0}s, which is above the {1}s retry cap.").format(_shown_seconds(retry_after_sec), _shown_seconds(RETRY_MAX_DELAY_SEC))


def _apply_jitter_ms(delay_ms: float, *, positive: bool) -> float:
    # OpenClaw packages/retry applyJitter: Retry-After is a lower bound, so
    # positive jitter ceils. Over-cap hints spread downward to avoid lockstep.
    roll = random.random()
    if positive:
        return max(0.0, math.ceil(delay_ms * (1.0 + roll)))
    return max(0.0, round(delay_ms * (0.5 + roll * 0.5)))


def backoff_delay_sec(*, attempt: int = 1, retry_after_sec: float | None = None) -> float:
    """Jittered delay before the next attempt. ``attempt`` is 1-based (first wait = min delay)."""
    retry_after_value = retry_after_sec if retry_after_sec is not None and math.isfinite(retry_after_sec) else None
    if retry_after_value is not None:
        base = max(retry_after_value, RETRY_MIN_DELAY_SEC)
    else:
        base = RETRY_MIN_DELAY_SEC * (2 ** max(attempt - 1, 0))
    delay = min(base, RETRY_MAX_DELAY_SEC)
    can_honor = retry_after_value is not None and retry_after_value <= RETRY_MAX_DELAY_SEC
    # Full jitter: positive unless a Retry-After above the cap must spread downward.
    wants_positive = retry_after_value is None or can_honor
    delay_ms = _apply_jitter_ms(delay * 1000.0, positive=wants_positive)
    return min(max(delay_ms / 1000.0, RETRY_MIN_DELAY_SEC), RETRY_MAX_DELAY_SEC)


def wait_abortable(delay_sec: float, stop_checker: Callable[[], bool] | None = None, *, sleep: Callable[[float], None] | None = None, monotonic: Callable[[], float] | None = None, chunk_sec: float = RETRY_WAIT_CHUNK_SEC) -> bool:
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


def _error_code_is_overload(value: Any) -> bool:
    """True when a structured provider code is itself an overload signal."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value in RETRYABLE_HTTP_STATUS
    text = str(value).strip().lower()
    if text in {"429", "503", "529"}:
        return True
    return any(marker in text for marker in _OVERLOAD_TEXT_MARKERS)


def _structured_error_is_overload(chunk: dict[str, Any]) -> bool:
    """Prefer error.code / type / status over scanning the message text."""
    if any(_error_code_is_overload(chunk.get(key)) for key in ("code", "status")):
        return True
    err = chunk.get("error")
    if isinstance(err, dict):
        if any(_error_code_is_overload(err.get(key)) for key in ("code", "type", "status")):
            return True
        metadata = err.get("metadata")
        if isinstance(metadata, dict) and any(_error_code_is_overload(metadata.get(key)) for key in ("code", "status", "http_status")):
            return True
    elif _error_code_is_overload(err):
        return True
    return False


def _text_is_overload(message: Any) -> bool:
    """Check if an error indicates an overload/rate-limit condition.

    Checks HTTP status / structured error code (including OpenRouter error.code
    in the body) first; the text fallback matches only phrases like 'overloaded' /
    'rate limit', no bare 429/503.
    """
    if not message or not isinstance(message, str):
        return False

    # Check for embedded JSON with structured error code (e.g. OpenRouter error payload)
    if "{" in message and "}" in message:
        try:
            start = message.find("{")
            end = message.rfind("}")
            parsed = json.loads(message[start : end + 1])
            if isinstance(parsed, dict) and _structured_error_is_overload(parsed):
                return True
        except (ValueError, TypeError):
            pass

    # Check for explicit HTTP status indicator in text (e.g. "HTTP 429", "status 503")
    if _HTTP_STATUS_OVERLOAD_RE.search(message):
        return True

    # Text fallback: matches only phrases like 'overloaded'/'rate limit', no bare 429/503
    low = message.lower()
    return any(marker in low for marker in _OVERLOAD_TEXT_MARKERS)


def stream_error_is_overload(chunk: dict[str, Any]) -> bool:
    """True when a mid-stream error is worth one retry before any UI text.

    A structured code (429, 503, rate_limit_exceeded, overloaded_error) wins.
    Otherwise explicit HTTP status or phrases match; bare numbers like 503 tokens do not.
    """
    if _structured_error_is_overload(chunk):
        return True
    message = stream_error_message(chunk) or ""
    return _text_is_overload(message)


# Process-wide spacing after a retry: keyed by host so chat, grammar, and
# Calc =PROMPT() share one cooldown. last_sent starts unset so the first
# request of a session is never delayed by a stale epoch of 0.
# OpenRouter :free / openrouter/free are 20 rpm; those jobs use ``{host}:{port}:free``
# (OpenRouter only) so paid OpenRouter traffic is not slowed.
OPENROUTER_FREE_MIN_GAP_SEC = 3.0
_host_gap_lock = threading.Lock()
_host_gap_sec: dict[str, float] = {}
_host_last_sent: dict[str, float] = {}


def pacing_key(host: str, model: str | None = None, *, port: int | None = None, openrouter: bool = False) -> str:
    """Host key for gap maps; ``{host}:{port}:free`` for OpenRouter free models."""
    if not host:
        return host
    if port is None:
        base = host
    elif ":" in host:
        base = f"[{host}]:{port}"
    else:
        base = f"{host}:{port}"
    mid = str(model or "").strip()
    if openrouter and mid and (mid.endswith(":free") or mid == "openrouter/free"):
        return f"{base}:free"
    return base


def request_model_from_body(body: object) -> str | None:
    """Read the chat ``model`` field from a JSON request body."""
    if not isinstance(body, (str, bytes)) or not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        model = payload.get("model")
        return model if isinstance(model, str) else None
    return None


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

    The :free floor is applied on the next reserve, so :free does not burst
    and the paid key is not written.
    """
    if not host:
        return
    with _host_gap_lock:
        _host_gap_sec.pop(host, None)


def mark_host_sent(host: str, *, monotonic: Callable[[], float] | None = None) -> None:
    if not host:
        return
    now = (monotonic or time.monotonic)()
    with _host_gap_lock:
        # _reserve_host_send_slot may already have reserved a later instant.
        # A slow send must not move that stamp backward and let another job in.
        prev = _host_last_sent.get(host)
        if prev is None or now > prev:
            _host_last_sent[host] = now


def _reserve_host_send_slot(host: str, *, monotonic: Callable[[], float] | None = None) -> tuple[float, float, float | None]:
    if not host:
        return 0.0, 0.0, None
    now = (monotonic or time.monotonic)()
    with _host_gap_lock:
        gap = _host_gap_sec.get(host, 0.0)
        if host.endswith(":free"):
            gap = max(gap, OPENROUTER_FREE_MIN_GAP_SEC)
        previous_last = _host_last_sent.get(host)
        wait = 0.0
        if gap > 0 and previous_last is not None:
            wait = max(0.0, gap - (now - previous_last))
        reserved_at = now + wait
        _host_last_sent[host] = reserved_at
        return wait, reserved_at, previous_last


def _rollback_host_reservation(host: str, reserved_at: float, previous_last: float | None) -> None:
    with _host_gap_lock:
        if _host_last_sent.get(host) == reserved_at:
            if previous_last is None:
                _host_last_sent.pop(host, None)
            else:
                _host_last_sent[host] = previous_last


def wait_host_gap(host: str, stop_checker: Callable[[], bool] | None = None, status_callback: Callable[[str], None] | None = None, *, monotonic: Callable[[], float] | None = None) -> bool:
    """Wait out a learned per-host gap. False if Stop fired."""
    remaining, reserved_at, previous_last = _reserve_host_send_slot(host, monotonic=monotonic)
    if remaining <= 0:
        return True
    if status_callback is not None:
        with _host_gap_lock:
            stored_gap = _host_gap_sec.get(host, 0.0)
        # Bugfix: Previously wait_host_gap unconditionally called emit_retry_status,
        # showing "Provider busy, retrying in {0}s…" even during initial pre-send pacing
        # for free-tier models (3s floor on :free keys).
        # What was wrong: False "Provider busy" warning appeared before any failure occurred.
        # How it happened: Both rate-limit retries and proactive rate pacing used the same status formatter.
        # Why this change fixes it: If the pacing key is a :free model and stored gap is <= the free floor,
        # show a dedicated pacing message; real retry delays (> 3s) still show "Provider busy".
        if host.endswith(":free") and stored_gap <= OPENROUTER_FREE_MIN_GAP_SEC:
            from plugin.framework.i18n import _

            status_callback(_("Spacing free-model requests, next send in {0}s…").format(_shown_seconds(remaining)))
        else:
            emit_retry_status(status_callback, remaining)
    ok = wait_abortable(remaining, stop_checker, monotonic=monotonic)
    if not ok:
        # Bugfix: Stop during the wait left last_sent at now + wait, so the next caller
        # waited an extra gap. _reserve_host_send_slot wrote the future stamp before the sleep;
        # rollback restores the previous stamp only if nobody else has reserved past it.
        _rollback_host_reservation(host, reserved_at, previous_last)
        return False
    return True


class RequestPacer:
    """Sleep before rapid repeat sends on the same client.

    Note: RequestPacer is per-transport (short 0.05s intervals on one client).
    Process-wide host pacing (for 429 cooldowns and OpenRouter :free 20 rpm rate limits)
    is handled across clients by wait_host_gap.
    """

    min_interval_sec: float
    last_sent_monotonic: float

    def __init__(self, min_interval_sec: float = LLM_MIN_REQUEST_INTERVAL_SEC) -> None:
        self.min_interval_sec = min_interval_sec
        self.last_sent_monotonic = 0.0

    def wait_before_send(self) -> None:
        """Sleep if needed so consecutive sends are not back-to-back."""
        wait = self.min_interval_sec - (time.monotonic() - self.last_sent_monotonic)
        if wait > 0:
            time.sleep(wait)

    def mark_sent(self) -> None:
        self.last_sent_monotonic = time.monotonic()


_local_unverified_hosts: set[str] = set()
_local_unverified_lock = threading.Lock()


def reset_local_unverified_hosts_for_tests() -> None:
    """Drop process-wide local TLS fallback memory between tests."""
    with _local_unverified_lock:
        _local_unverified_hosts.clear()


def ssl_mode_for(scheme: str, host: str) -> str:
    """Return ``verified``, ``unverified``, or ``plain`` for the next connection.

    Public HTTPS is always verified. Local HTTPS starts verified and
    switches to unverified only after ``enable_local_unverified`` records a
    certificate failure (self-signed Ollama/LM Studio). Previously this
    returned ``unverified`` for every non-local host, which silently
    disabled TLS checks for OpenAI, Anthropic, OpenRouter, and similar.
    """
    if scheme != "https":
        return "plain"
    if is_local_host(host):
        with _local_unverified_lock:
            if host in _local_unverified_hosts:
                return "unverified"
    return "verified"


def enable_local_unverified(host: str, err: BaseException) -> bool:
    """Enable unverified retry for a local host after certificate validation fails."""
    if not host or not is_local_host(host) or not is_certificate_verify_error(err):
        return False
    with _local_unverified_lock:
        if host in _local_unverified_hosts:
            return False
        _local_unverified_hosts.add(host)
    log.warning("Local HTTPS certificate verification failed for %s; retrying unverified.", host)
    return True
