# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Low-level pacing and retry policy helpers for outbound LLM requests.

Concurrency: ``RequestPacer`` (minimum gap between sends) is a field on one
transport and is not locked. Each ``LlmClient`` has its own transport.
``LocalHttpsCertificateFallback`` remembers local hosts process-wide (same
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
from typing import Any, Callable, Literal

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


def format_retry_wait_status(delay_sec: float) -> str:
    """Short sidebar status line for the abortable retry wait."""
    from plugin.framework.i18n import _

    return _("Provider busy, retrying in {0}s…").format(_shown_seconds(delay_sec))


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


def backoff_delay_sec(*, attempt: int = 1, retry_after_sec: float | None = None, min_delay: float = RETRY_MIN_DELAY_SEC, max_delay: float = RETRY_MAX_DELAY_SEC, jitter: float | Literal["full"] = "full", rng: Callable[[], float] = random.random) -> float:
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
    delay_ms = _apply_jitter_ms(delay_ms, jitter, "positive" if wants_positive else "symmetric", rng)
    delay = min(max(delay_ms / 1000.0, min_delay), delay_cap)
    return delay


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
    if isinstance(value, int):
        return value in (429, 503, 529)
    if isinstance(value, float):
        return value in (429.0, 503.0, 529.0)
    text = str(value).strip().lower()
    if not text:
        return False
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


def _text_is_overload(message: Any, *, status_code: int | None = None) -> bool:
    """Check if an error indicates an overload/rate-limit condition.

    Checks HTTP status / structured error code (including OpenRouter error.code
    in the body) first; the text fallback matches only phrases like 'overloaded' /
    'rate limit', no bare 429/503.
    """
    if isinstance(message, dict):
        return _structured_error_is_overload(message)
    if status_code is not None and _error_code_is_overload(status_code):
        return True
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


def is_openrouter_free_model(model: str | None) -> bool:
    mid = str(model or "").strip()
    # Catalog auto-router is ``openrouter/free``; per-model free variants end in ``:free``.
    return bool(mid) and (mid.endswith(":free") or mid == "openrouter/free")


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
    if openrouter and is_openrouter_free_model(model):
        return f"{base}:free"
    return base


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
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        model = payload.get("model")
        return model if isinstance(model, str) else None
    return None


def ensure_free_model_pacing(host: str, model: str | None, *, port: int | None = None, openrouter: bool = False) -> str:
    """Return the pacing key and seed the 3s :free floor when applicable."""
    key = pacing_key(host, model, port=port, openrouter=openrouter)
    if key.endswith(":free"):
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
        previous_last = _host_last_sent.get(host)
        wait = 0.0
        if gap > 0 and previous_last is not None:
            wait = max(0.0, gap - (now - previous_last))
        reserved_at = now + wait
        _host_last_sent[host] = reserved_at
        return wait, reserved_at, previous_last


def _rollback_host_reservation(host: str, reserved_at: float, previous_last: float | None) -> None:
    if not host:
        return
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
        # for free-tier models (3s floor seeded by ensure_free_model_pacing).
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


_local_unverified_hosts: set[str] = set()
_local_unverified_lock = threading.Lock()


def reset_local_unverified_hosts_for_tests() -> None:
    """Drop process-wide local TLS fallback memory between tests."""
    with _local_unverified_lock:
        _local_unverified_hosts.clear()


def _local_host_unverified(host: str) -> bool:
    with _local_unverified_lock:
        return host in _local_unverified_hosts


class LocalHttpsCertificateFallback:
    """Track local HTTPS hosts that should retry with certificate verification disabled.

    The host set is process-wide. A new ``LlmClient`` per sidebar send used to
    forget the fallback and fail TLS verify on every request.
    """

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
        if is_local_host(host) and _local_host_unverified(host):
            return "unverified"
        return "verified"

    def enable_if_applicable(self, host: str, err: BaseException) -> bool:
        """Enable unverified retry for a local host after certificate validation fails."""
        if not host or not is_local_host(host) or not is_certificate_verify_error(err):
            return False
        with _local_unverified_lock:
            if host in _local_unverified_hosts:
                return False
            _local_unverified_hosts.add(host)
        log.warning("Local HTTPS certificate verification failed for %s; retrying unverified.", host)
        return True
