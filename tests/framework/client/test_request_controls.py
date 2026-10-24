import datetime
import email.utils
import ssl
import time
from typing import Any

from plugin.framework.client import request_controls
from plugin.framework.client.request_controls import (
    OPENROUTER_FREE_MIN_GAP_SEC,
    LocalHttpsCertificateFallback,
    RequestPacer,
    _reserve_host_send_slot,
    _structured_error_is_overload,
    _text_is_overload,
    backoff_delay_sec,
    clear_host_gap,
    emit_retry_status,
    ensure_free_model_pacing,
    format_retry_wait_status,
    is_openrouter_free_model,
    mark_host_sent,
    pacing_key,
    parse_retry_after,
    parse_retry_after_ms,
    remember_host_gap,
    request_model_from_body,
    reset_host_pacing_for_tests,
    reset_local_unverified_hosts_for_tests,
    retry_after_exceeds_cap,
    stream_error_is_overload,
    wait_abortable,
    wait_host_gap,
)


def _remaining(key: str, now: Any = None) -> float:
    current = (now or time.monotonic)()
    with request_controls._host_gap_lock:
        gap = request_controls._host_gap_sec.get(key, 0.0)
        last = request_controls._host_last_sent.get(key)
    if gap <= 0 or last is None:
        return 0.0
    return max(0.0, gap - (current - last))


def setup_function() -> None:
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()


def test_request_pacer_sleeps_for_back_to_back_sends():
    sleeps: list[float] = []
    times = iter([1000.0, 1000.0, 1000.0])
    pacer = RequestPacer(monotonic=lambda: next(times), sleep=sleeps.append)

    pacer.wait_before_send()
    pacer.mark_sent()
    pacer.wait_before_send()

    assert sleeps == [0.05]


def test_local_https_certificate_fallback_only_enables_for_local_cert_errors():
    fallback = LocalHttpsCertificateFallback()

    assert fallback.ssl_mode_for("https", "localhost") == "verified"
    assert fallback.ssl_mode_for("https", "api.openai.com") == "verified"
    assert fallback.enable_if_applicable("localhost", ssl.SSLCertVerificationError("self-signed certificate")) is True
    assert fallback.ssl_mode_for("https", "localhost") == "unverified"
    assert fallback.ssl_mode_for("https", "api.openai.com") == "verified"

    assert fallback.enable_if_applicable("api.openai.com", ssl.SSLCertVerificationError("self-signed certificate")) is False
    assert fallback.enable_if_applicable("localhost", OSError("connection reset")) is False
    assert fallback.ssl_mode_for("http", "localhost") == "plain"
    # The next client must not pay the failed handshake again.
    assert LocalHttpsCertificateFallback().ssl_mode_for("https", "localhost") == "unverified"
    # A dotless name is not local. Search-domain names must stay verified.
    assert fallback.enable_if_applicable("ollama-box", ssl.SSLCertVerificationError("self-signed certificate")) is False
    assert fallback.ssl_mode_for("https", "ollama-box") == "verified"


def test_parse_retry_after_seconds_and_http_date():
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after(" 1.5 ") == 1.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("not-a-date") is None

    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=8)
    header = email.utils.format_datetime(future, usegmt=True)
    delay = parse_retry_after(header)
    assert delay is not None
    assert 6.0 < delay < 10.0


def test_backoff_honors_retry_after_floor_with_zero_jitter():
    # OpenClaw: 1.4ms Retry-After with jitter=0 ceils to 2ms, never undercuts the hint.
    delay = backoff_delay_sec(
        attempt=1,
        retry_after_sec=0.0014,
        min_delay=0.0,
        max_delay=0.01,
        jitter=0,
        rng=lambda: 0.0,
    )
    assert delay == 0.002


def test_backoff_without_retry_after_grows_with_attempt():
    d1 = backoff_delay_sec(attempt=1, min_delay=0.3, max_delay=30.0, jitter=0, rng=lambda: 0.0)
    d2 = backoff_delay_sec(attempt=2, min_delay=0.3, max_delay=30.0, jitter=0, rng=lambda: 0.0)
    assert d1 == 0.3
    assert d2 == 0.6


def test_backoff_over_cap_retry_after_spreads_downward():
    delay = backoff_delay_sec(
        attempt=1,
        retry_after_sec=100.0,
        min_delay=0.3,
        max_delay=30.0,
        jitter="full",
        rng=lambda: 0.0,
    )
    assert delay == 15.0


def test_wait_abortable_stops_without_sleeping_remainder():
    sleeps: list[float] = []
    calls = {"n": 0}

    def stop_checker() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    times = iter([0.0, 0.0, 0.0, 0.0])
    ok = wait_abortable(
        10.0,
        stop_checker,
        sleep=sleeps.append,
        monotonic=lambda: next(times),
        chunk_sec=0.05,
    )
    assert ok is False
    assert sleeps == [0.05]


def test_format_retry_wait_status_rounds_seconds():
    assert "0.3s" in format_retry_wait_status(0.3)
    assert "2s" in format_retry_wait_status(1.6)


def test_openrouter_free_pacing_key_covers_suffix_and_auto_router():
    assert is_openrouter_free_model("deepseek/deepseek-r1:free")
    assert is_openrouter_free_model("openrouter/free")
    assert not is_openrouter_free_model("openai/gpt-oss-120b")
    assert pacing_key("openrouter.ai", "deepseek/deepseek-r1:free", openrouter=True) == "openrouter.ai:free"
    assert pacing_key("openrouter.ai", "openrouter/free", openrouter=True) == "openrouter.ai:free"
    assert pacing_key("openrouter.ai", "deepseek/deepseek-r1:free") == "openrouter.ai"
    assert pacing_key("openrouter.ai", "openai/gpt-oss-120b") == "openrouter.ai"
    assert pacing_key("localhost", "llama3:free", port=11434) == "localhost:11434"
    assert pacing_key("api.openai.com", "x:free", port=443) == "api.openai.com:443"
    assert pacing_key("openrouter.ai", "x:free", port=443, openrouter=True) == "openrouter.ai:443:free"
    assert pacing_key("::1", "m", port=11434) == "[::1]:11434"


def test_openrouter_free_floor_survives_clear_and_yields_to_larger_429():
    clock = {"t": 100.0}

    def now() -> float:
        return clock["t"]

    key = ensure_free_model_pacing("openrouter.ai", "openrouter/free", openrouter=True)
    assert key == "openrouter.ai:free"
    mark_host_sent(key, monotonic=now)
    clock["t"] = 101.0
    assert _remaining(key, now) == OPENROUTER_FREE_MIN_GAP_SEC - 1.0
    remember_host_gap(key, 10.0)
    assert _remaining(key, now) == 9.0
    clear_host_gap(key)
    assert _remaining(key, now) == OPENROUTER_FREE_MIN_GAP_SEC - 1.0
    clear_host_gap("openrouter.ai")
    assert _remaining("openrouter.ai", now) == 0.0


def test_free_pacing_not_seeded_off_openrouter():
    key = ensure_free_model_pacing("localhost", "llama3:free", port=11434)
    assert key == "localhost:11434"
    assert _remaining(key) == 0.0


def test_request_model_from_body_reads_model_field():
    assert request_model_from_body(b'{"model": "openrouter/free"}') == "openrouter/free"
    assert request_model_from_body('{"model": "foo:free"}') == "foo:free"
    assert request_model_from_body(b"not-json") is None


def test_host_gap_is_sticky_until_first_try_success():
    clock = {"t": 100.0}

    def now() -> float:
        return clock["t"]

    assert _remaining("localhost", now) == 0.0
    mark_host_sent("localhost", monotonic=now)
    remember_host_gap("localhost", 5.0)
    clock["t"] = 102.0
    assert _remaining("localhost", now) == 3.0
    remember_host_gap("localhost", 2.0)
    assert _remaining("localhost", now) == 3.0
    clear_host_gap("localhost")
    assert _remaining("localhost", now) == 0.0


def test_emit_retry_status_skips_none():
    emit_retry_status(None, 1.0)
    seen: list[str] = []
    emit_retry_status(seen.append, 2.0)
    assert len(seen) == 1
    assert "2s" in seen[0]


def test_parse_retry_after_ms():
    assert parse_retry_after_ms("1500") == 1.5
    assert parse_retry_after_ms("0") == 0.0
    assert parse_retry_after_ms(None) is None
    assert parse_retry_after_ms("") is None
    assert parse_retry_after_ms("nope") is None
    assert parse_retry_after_ms("-5") is None


def test_retry_after_exceeds_cap():
    assert not retry_after_exceeds_cap(None)
    assert not retry_after_exceeds_cap(30)
    assert not retry_after_exceeds_cap(30.0)
    assert retry_after_exceeds_cap(30.1)
    assert retry_after_exceeds_cap(120)


def test_wait_host_gap_stop_rolls_back_reservation():
    remember_host_gap("localhost", 5.0)
    mark_host_sent("localhost", monotonic=lambda: 100.0)
    assert wait_host_gap("localhost", stop_checker=lambda: True, monotonic=lambda: 100.0) is False
    assert _reserve_host_send_slot("localhost", monotonic=lambda: 100.0)[0] == 5.0


def test_wait_host_gap_stop_does_not_clobber_later_claim():
    remember_host_gap("localhost", 5.0)
    mark_host_sent("localhost", monotonic=lambda: 100.0)
    first_wait = _reserve_host_send_slot("localhost", monotonic=lambda: 100.0)[0]
    assert first_wait == 5.0
    assert wait_host_gap("localhost", stop_checker=lambda: True, monotonic=lambda: 100.0) is False
    assert _remaining("localhost", lambda: 100.0) == 10.0


def test_wait_host_gap_status_callback():
    key = ensure_free_model_pacing("openrouter.ai", "openrouter/free", openrouter=True)
    mark_host_sent(key, monotonic=lambda: 100.0)
    seen: list[str] = []
    # Key has 3s floor, 1s has elapsed, remaining is 2s.
    wait_host_gap(key, status_callback=seen.append, monotonic=lambda: 101.0, stop_checker=lambda: True)
    assert len(seen) == 1
    assert "Spacing free-model requests, next send in 2s…" in seen[0]

    # Learned 10s gap on a busy host (exceeds free floor) -> "Provider busy"
    remember_host_gap("busy.ai", 10.0)
    mark_host_sent("busy.ai", monotonic=lambda: 100.0)
    seen_busy: list[str] = []
    wait_host_gap("busy.ai", status_callback=seen_busy.append, monotonic=lambda: 101.0, stop_checker=lambda: True)
    assert len(seen_busy) == 1
    assert "Provider busy, retrying in 9s…" in seen_busy[0]


def test_local_https_certificate_fallback_logs_warning(caplog):
    import logging
    fallback = LocalHttpsCertificateFallback()
    with caplog.at_level(logging.WARNING):
        fallback.enable_if_applicable("localhost", ssl.SSLCertVerificationError("self-signed certificate"))
    assert any(rec.levelno == logging.WARNING and "Local HTTPS certificate verification failed for localhost" in rec.message for rec in caplog.records)


def test_overload_detection_prefers_code_and_word_boundary():
    """429/503 inside a larger number is not overload. A structured code is."""
    assert stream_error_is_overload({"error": {"message": "Requested 4290 tokens"}}) is False
    assert stream_error_is_overload({"error": {"message": "code 5031"}}) is False
    assert stream_error_is_overload({"error": {"code": "context_length_exceeded", "message": "Requested 4290 tokens"}}) is False
    assert stream_error_is_overload({"error": {"message": "HTTP 429"}}) is True
    assert stream_error_is_overload({"error": {"message": "status 503"}}) is True
    assert stream_error_is_overload({"error": {"code": 429, "message": "slow down"}}) is True
    assert stream_error_is_overload({"error": {"code": "503", "message": "down"}}) is True
    assert stream_error_is_overload({"error": {"type": "overloaded_error", "message": "Overloaded"}}) is True
    assert stream_error_is_overload({"error": {"code": "rate_limit_exceeded", "message": "slow down"}}) is True
    assert stream_error_is_overload({"error": {"message": "limit is 503 tokens"}}) is False
    assert _text_is_overload("limit is 503 tokens") is False
    assert _text_is_overload('{"error": {"code": 429, "message": "rate limit reached"}}') is True
    assert _text_is_overload('{"error": {"code": 503, "message": "down"}}') is True
    assert _text_is_overload('{"error": {"code": 400, "message": "limit is 503 tokens"}}') is False


class TestOverloadDetection:
    """Overload detection prefers HTTP status / structured error code over text fallback."""

    def test_text_is_overload_bare_503_tokens_not_overload(self):
        assert _text_is_overload("limit is 503 tokens") is False
        assert _text_is_overload("context limit is 503 tokens") is False
        assert _text_is_overload("Requested 4290 tokens") is False
        assert _text_is_overload("code 5031") is False

    def test_text_is_overload_http_status_detected(self):
        assert _text_is_overload("HTTP 429") is True
        assert _text_is_overload("status 503") is True
        assert _text_is_overload("HTTP status 429") is True
        assert _text_is_overload("error code 503") is True

    def test_text_is_overload_phrases_detected(self):
        assert _text_is_overload("Server is overloaded, try again later") is True
        assert _text_is_overload("Rate limit exceeded") is True
        assert _text_is_overload("Too Many Requests") is True
        assert _text_is_overload("Service Unavailable") is True

    def test_text_is_overload_openrouter_json_body(self):
        # OpenRouter error.code in body string
        assert _text_is_overload('{"error": {"code": 429, "message": "Provider rate limit reached"}}') is True
        assert _text_is_overload('{"error": {"code": 503, "message": "Service temporarily unavailable"}}') is True
        assert _text_is_overload('{"error": {"code": 400, "message": "limit is 503 tokens"}}') is False

    def test_structured_error_is_overload(self):
        assert _structured_error_is_overload({"error": {"code": 429}}) is True
        assert _structured_error_is_overload({"error": {"code": 503}}) is True
        assert _structured_error_is_overload({"error": {"type": "overloaded_error"}}) is True
        assert _structured_error_is_overload({"error": {"code": "rate_limit_exceeded"}}) is True
        assert _structured_error_is_overload({"error": {"code": 400, "message": "limit is 503 tokens"}}) is False
        assert stream_error_is_overload({"error": {"message": "limit is 503 tokens"}}) is False
