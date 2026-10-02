import datetime
import email.utils
import ssl

from plugin.framework.client.request_controls import (
    OPENROUTER_FREE_MIN_GAP_SEC,
    LocalHttpsCertificateFallback,
    RequestPacer,
    backoff_delay_sec,
    claim_host_send_slot,
    clear_host_gap,
    emit_retry_status,
    ensure_free_model_pacing,
    format_retry_wait_status,
    is_openrouter_free_model,
    mark_host_sent,
    pacing_key,
    parse_retry_after,
    remaining_host_gap,
    remember_host_gap,
    request_model_from_body,
    reset_host_pacing_for_tests,
    reset_local_unverified_hosts_for_tests,
    wait_abortable,
)


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
        random=lambda: 0.0,
    )
    assert delay == 0.002


def test_backoff_without_retry_after_grows_with_attempt():
    d1 = backoff_delay_sec(attempt=1, min_delay=0.3, max_delay=30.0, jitter=0, random=lambda: 0.0)
    d2 = backoff_delay_sec(attempt=2, min_delay=0.3, max_delay=30.0, jitter=0, random=lambda: 0.0)
    assert d1 == 0.3
    assert d2 == 0.6


def test_backoff_over_cap_retry_after_spreads_downward():
    delay = backoff_delay_sec(
        attempt=1,
        retry_after_sec=100.0,
        min_delay=0.3,
        max_delay=30.0,
        jitter="full",
        random=lambda: 0.0,
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
    assert pacing_key("openrouter.ai", "deepseek/deepseek-r1:free") == "openrouter.ai:free"
    assert pacing_key("openrouter.ai", "openrouter/free") == "openrouter.ai:free"
    assert pacing_key("openrouter.ai", "openai/gpt-oss-120b") == "openrouter.ai"


def test_openrouter_free_floor_survives_clear_and_yields_to_larger_429():
    clock = {"t": 100.0}

    def now() -> float:
        return clock["t"]

    key = ensure_free_model_pacing("openrouter.ai", "openrouter/free")
    assert key == "openrouter.ai:free"
    mark_host_sent(key, monotonic=now)
    clock["t"] = 101.0
    assert remaining_host_gap(key, monotonic=now) == OPENROUTER_FREE_MIN_GAP_SEC - 1.0
    remember_host_gap(key, 10.0)
    assert remaining_host_gap(key, monotonic=now) == 9.0
    clear_host_gap(key)
    assert remaining_host_gap(key, monotonic=now) == OPENROUTER_FREE_MIN_GAP_SEC - 1.0
    clear_host_gap("openrouter.ai")
    assert remaining_host_gap("openrouter.ai", monotonic=now) == 0.0


def test_request_model_from_body_reads_model_field():
    assert request_model_from_body(b'{"model": "openrouter/free"}') == "openrouter/free"
    assert request_model_from_body('{"model": "foo:free"}') == "foo:free"
    assert request_model_from_body(b"not-json") is None


def test_host_gap_claims_do_not_both_see_zero_wait():
    """Two jobs must not both observe a zero gap and send together."""
    remember_host_gap("example.com", 5.0)
    mark_host_sent("example.com", monotonic=lambda: 100.0)
    first = claim_host_send_slot("example.com", monotonic=lambda: 100.0)
    second = claim_host_send_slot("example.com", monotonic=lambda: 100.0)
    assert first == 5.0
    assert second == 10.0
    # The later reservation must survive the first job recording its real send.
    mark_host_sent("example.com", monotonic=lambda: 105.0)
    # 105 is earlier than the second reservation (110). A third caller still waits.
    assert remaining_host_gap("example.com", monotonic=lambda: 105.0) == 10.0


def test_host_gap_is_sticky_until_first_try_success():
    clock = {"t": 100.0}

    def now() -> float:
        return clock["t"]

    assert remaining_host_gap("localhost", monotonic=now) == 0.0
    mark_host_sent("localhost", monotonic=now)
    remember_host_gap("localhost", 5.0)
    clock["t"] = 102.0
    assert remaining_host_gap("localhost", monotonic=now) == 3.0
    remember_host_gap("localhost", 2.0)
    assert remaining_host_gap("localhost", monotonic=now) == 3.0
    clear_host_gap("localhost")
    assert remaining_host_gap("localhost", monotonic=now) == 0.0


def test_emit_retry_status_skips_none():
    emit_retry_status(None, 1.0)
    seen: list[str] = []
    emit_retry_status(seen.append, 2.0)
    assert len(seen) == 1
    assert "2s" in seen[0]
