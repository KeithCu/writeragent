"""Tests for DuckDuckGoSearchTool in plugin.contrib.smolagents.default_tools."""

import http.client
import socket
import threading
import time
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from plugin.contrib.smolagents import default_tools
from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, _search_cache_key, _web_cache_get
from plugin.framework.client.http_transport import HttpResult
from plugin.framework.client.request_controls import (
    RETRY_MAX_ATTEMPTS,
    reset_host_pacing_for_tests,
    reset_local_unverified_hosts_for_tests,
)
from plugin.framework.client.requests import close_sync_transports
from plugin.framework.send_cancellation import SendCancellation


@pytest.fixture(autouse=True)
def _reset_host_pacing(monkeypatch):
    close_sync_transports()
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()
    monkeypatch.setattr(default_tools, "_SEARCH_MIN_GAP_SEC", 0.0)
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_DELAYS_SEC", (0.0, 0.0, 0.0))
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(default_tools, "_search_next_allowed", 0.0)
    monkeypatch.setattr(default_tools, "_search_connection_challenged", False)
    yield
    close_sync_transports()
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()


def _response(status: int, body: bytes, reason: str = "OK"):
    response = MagicMock()
    response.status = status
    response.reason = reason
    response.read.return_value = body
    response.getheader.return_value = None
    return response


def _conn_returning(response):
    conn = MagicMock()
    conn.getresponse.return_value = response
    return conn


def _patch_https(connections):
    conns = list(connections)
    created = []

    def _factory(*args, **kwargs):
        conn = conns.pop(0)
        created.append((args, kwargs, conn))
        return conn

    return patch("http.client.HTTPSConnection", side_effect=_factory), created


def _http_ok(body: bytes) -> HttpResult:
    return HttpResult(status=200, body=body, content_type="text/html")


def test_web_search_recency_sets_df_parameter():
    for recency, expected_df in [("day", "d"), ("week", "w"), ("month", "m"), ("year", "y")]:
        captured = {}
        html = b"<html><body>no results</body></html>"

        def _fake_sync_request(_url, data=None, _headers=None, **_kwargs):
            captured["params"] = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
            return _http_ok(html)

        with patch("plugin.contrib.smolagents.default_tools.sync_request", side_effect=_fake_sync_request):
            DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news", recency=recency)
        assert captured["params"].get("df") == expected_df, f"recency={recency} should map to df={expected_df}"


def test_web_search_recency_any_or_none_omits_df():
    for recency in [None, "any"]:
        captured = {}

        def _fake_sync_request(_url, data=None, _headers=None, **_kwargs):
            captured["params"] = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
            return _http_ok(b"<html><body>no results</body></html>")

        with patch("plugin.contrib.smolagents.default_tools.sync_request", side_effect=_fake_sync_request):
            DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news", recency=recency)
        assert "df" not in captured["params"], f"recency={recency} should omit df"


def test_web_search_cache_key_includes_recency():
    plain = _search_cache_key("AI news", None)
    assert plain == "any|ai news"
    assert _search_cache_key("AI news", "day") != plain
    assert _search_cache_key("AI news", "week") != _search_cache_key("AI news", "day")


def test_web_search_unknown_recency_behaves_as_any():
    assert _search_cache_key("AI news", "d") == "any|ai news"
    assert _search_cache_key("AI news", 7) == "any|ai news"

    captured = {}

    def _fake_sync_request(_url, data=None, _headers=None, **_kwargs):
        captured["params"] = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
        return _http_ok(b"<html><body>no results</body></html>")

    with patch("plugin.contrib.smolagents.default_tools.sync_request", side_effect=_fake_sync_request):
        DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news", recency="recent")
    assert "df" not in captured["params"]


def test_web_search_skips_sponsored_rows():
    html = (
        "<table>"
        # Sponsored ad row (single row, has its own result-link anchors incl. 'more info')
        "<tr class='result-sponsored'>"
        "<td>&nbsp;</td>"
        "<td><a href='http://ad.example/' class='result-link'>Sponsored Ad Title</a></td>"
        "<td>(Sponsored link - <a href='http://help/' rel='nofollow' class='result-link'>more info</a>)</td>"
        "</tr>"
        # Real result split across three rows (current DDG Lite layout)
        "<tr><td valign='top'>1.&nbsp;</td>"
        "<td><a rel='nofollow' href='http://real.example/story' class='result-link'>Real Result Title</a></td></tr>"
        "<tr><td>&nbsp;</td><td class='result-snippet'>real result snippet</td></tr>"
        "<tr><td>&nbsp;</td><td><span class='link-text'>real.example/story</span>"
        "<span class='timestamp'>2026-08-24</span></td></tr>"
        "</table>"
    ).encode("utf-8")

    with patch("plugin.contrib.smolagents.default_tools.sync_request", return_value=_http_ok(html)):
        tool = DuckDuckGoSearchTool(cache_max_age_days=30)
        result = tool.forward("AI news")

    assert "Sponsored Ad Title" not in result
    assert "more info" not in result
    assert "Real Result Title" in result
    assert "https://real.example/story" in result
    assert "real result snippet" in result


def test_web_search_parses_split_row_layout():
    """Current DDG Lite splits each result across title/snippet/link rows."""
    html = (
        "<table>"
        "<tr><td valign='top'>1.&nbsp;</td>"
        "<td><a rel='nofollow' href='http://one.example/a' class='result-link'>First Headline</a></td></tr>"
        "<tr><td>&nbsp;</td><td class='result-snippet'>first snippet</td></tr>"
        "<tr><td>&nbsp;</td><td><span class='link-text'>one.example/a</span></td></tr>"
        "<tr><td valign='top'>2.&nbsp;</td>"
        "<td><a rel='nofollow' href='http://two.example/b' class='result-link'>Second Headline</a></td></tr>"
        "<tr><td>&nbsp;</td><td class='result-snippet'>second snippet</td></tr>"
        "<tr><td>&nbsp;</td><td><span class='link-text'>two.example/b</span></td></tr>"
        "</table>"
    ).encode("utf-8")

    with patch("plugin.contrib.smolagents.default_tools.sync_request", return_value=_http_ok(html)):
        tool = DuckDuckGoSearchTool(cache_max_age_days=30)
        result = tool.forward("AI news")

    assert "First Headline" in result
    assert "Second Headline" in result
    assert "one.example/a" in result
    assert "two.example/b" in result


def test_web_search_does_not_cache_empty_parse(tmp_path):
    db_file = str(tmp_path / "writeragent_web_cache.db")
    with patch(
        "plugin.contrib.smolagents.default_tools.sync_request",
        return_value=_http_ok(b"<html><body>no results</body></html>"),
    ):
        result = DuckDuckGoSearchTool(cache_max_age_days=30, cache_path=db_file, cache_max_mb=10).forward("nothing here")
    assert "No results found" in result
    assert _web_cache_get(db_file, "search", _search_cache_key("nothing here", None), max_age_days=30) is None


def test_web_search_posts_form_encoded_body():
    """DDG Lite returns an empty 200 without an explicit form Content-Type header."""
    captured = {}

    def _fake_sync_request(_url, data=None, headers=None, **_kwargs):
        captured["data"] = data
        captured["headers"] = headers or {}
        return _http_ok(b"<html><body>no results</body></html>")

    with patch("plugin.contrib.smolagents.default_tools.sync_request", side_effect=_fake_sync_request):
        DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")

    assert captured["headers"].get("Content-Type") == "application/x-www-form-urlencoded"
    assert captured["data"] == b"q=AI+news"


def test_web_search_retries_reset_then_returns_results():
    html = (
        "<table>"
        "<tr><td valign='top'>1.&nbsp;</td>"
        "<td><a rel='nofollow' href='http://one.example/a' class='result-link'>First Headline</a></td></tr>"
        "<tr><td>&nbsp;</td><td class='result-snippet'>first snippet</td></tr>"
        "<tr><td>&nbsp;</td><td><span class='link-text'>one.example/a</span></td></tr>"
        "</table>"
    ).encode("utf-8")
    first = MagicMock()
    first.getresponse.side_effect = ConnectionResetError(104, "Connection reset by peer")
    ok = _conn_returning(_response(200, html, "OK"))
    patcher, created = _patch_https([first, ok])

    with patcher, patch("plugin.framework.client.http_transport.wait_abortable", return_value=True):
        result = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")

    assert "First Headline" in result
    assert len(created) == 2


def test_web_search_exhausted_retries_returns_unavailable_uncached(tmp_path):
    db_file = str(tmp_path / "writeragent_web_cache.db")
    conns = []
    for _idx in range(RETRY_MAX_ATTEMPTS):
        conn = MagicMock()
        conn.sock = None
        conn.connect.side_effect = TimeoutError("_ssl.c:1067: The handshake operation timed out")
        conns.append(conn)
    patcher, created = _patch_https(conns)

    with patcher, patch("plugin.framework.client.http_transport.wait_abortable", return_value=True):
        result = DuckDuckGoSearchTool(cache_max_age_days=30, cache_path=db_file, cache_max_mb=10).forward("AI news")

    assert result == default_tools._SEARCH_UNAVAILABLE
    assert "_ssl.c" not in result
    assert len(created) == RETRY_MAX_ATTEMPTS
    assert [kwargs["timeout"] for _args, kwargs, _conn in created] == [
        default_tools._SEARCH_CONNECT_TIMEOUT_SEC
    ] * RETRY_MAX_ATTEMPTS
    assert _web_cache_get(db_file, "search", _search_cache_key("AI news", None), max_age_days=30) is None


def test_web_search_http_202_then_200_returns_results():
    conn1 = _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
    conn2 = _keepalive_conn([_response(200, _ddg_html("Recovered"), "OK")])
    patcher, created = _patch_https([conn1, conn2])
    with patcher:
        result = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")

    assert "Recovered" in result
    assert "No results found" not in result
    assert len(created) == 2
    conn1.close.assert_called()
    assert conn1.request.call_args.args == ("POST", "/lite/")
    assert conn1.request.call_args.kwargs["body"] == b"q=AI+news"
    assert conn2.request.call_args.args == ("GET", "/lite/?q=AI+news")
    assert conn2.request.call_args.kwargs["body"] is None
    assert "Content-Type" not in conn2.request.call_args.kwargs["headers"]


def test_web_search_stop_returns_without_connecting():
    cancel = SendCancellation()
    cancel.cancel()
    with patch("http.client.HTTPSConnection") as https_conn:
        result = DuckDuckGoSearchTool(cache_max_age_days=30, cancel=cancel).forward("AI news")

    assert result == default_tools._SEARCH_STOPPED
    https_conn.assert_not_called()


def _ddg_html(title: str, url: str = "http://one.example/a", snippet: str = "snippet") -> bytes:
    return (
        "<table>"
        "<tr><td valign='top'>1.&nbsp;</td>"
        f"<td><a rel='nofollow' href='{url}' class='result-link'>{title}</a></td></tr>"
        f"<tr><td>&nbsp;</td><td class='result-snippet'>{snippet}</td></tr>"
        "<tr><td>&nbsp;</td><td><span class='link-text'>one.example/a</span></td></tr>"
        "</table>"
    ).encode("utf-8")


def _keepalive_conn(responses):
    conn = MagicMock()
    conn.sock = None

    def _connect():
        conn.sock = MagicMock(spec=socket.socket)

    conn.connect.side_effect = _connect
    conn.getresponse.side_effect = list(responses)
    return conn


def test_web_search_reuses_connection_across_searches():
    conn = _keepalive_conn([_response(200, _ddg_html("First Result")), _response(200, _ddg_html("Second Result"))])
    patcher, created = _patch_https([conn])

    tool = DuckDuckGoSearchTool(cache_max_age_days=30)
    with patcher:
        first = tool.forward("first query")
        second = tool.forward("second query")

    assert "First Result" in first
    assert "Second Result" in second
    assert len(created) == 1
    assert conn.connect.call_count == 1
    assert conn.request.call_count == 2


def test_web_search_stale_connection_reconnects_and_returns_results():
    stale = _keepalive_conn([_response(200, _ddg_html("First Result")), http.client.RemoteDisconnected("closed")])
    fresh = _keepalive_conn([_response(200, _ddg_html("Recovered Result"))])
    patcher, created = _patch_https([stale, fresh])

    tool = DuckDuckGoSearchTool(cache_max_age_days=30)
    with (
        patcher,
        patch("plugin.framework.client.http_transport.remember_host_gap") as remember_gap,
        patch("plugin.framework.client.http_transport.wait_abortable") as wait_abort,
    ):
        first = tool.forward("first query")
        second = tool.forward("second query")

    assert "First Result" in first
    assert "Recovered Result" in second
    assert len(created) == 2
    assert stale.connect.call_count == 1
    assert fresh.connect.call_count == 1
    remember_gap.assert_not_called()
    wait_abort.assert_not_called()


def test_concurrent_searches_from_separate_runs_never_overlap():
    lock = threading.Lock()
    active = 0
    peak = 0
    conn = _keepalive_conn([])

    def _getresponse():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return _response(200, _ddg_html("Concurrent Result"))

    conn.getresponse.side_effect = _getresponse
    patcher, _created = _patch_https([conn])
    results: list[str] = []
    results_lock = threading.Lock()

    def _run(query: str) -> None:
        tool = DuckDuckGoSearchTool(cache_max_age_days=30, cancel=SendCancellation())
        out = tool.forward(query)
        with results_lock:
            results.append(out)

    with patcher:
        threads = [threading.Thread(target=_run, args=(f"query {i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert peak == 1
    assert len(results) == 3
    assert all("<h2>Search Results</h2>" in r for r in results)


def test_web_search_challenge_on_all_attempts_returns_unavailable_uncached(tmp_path):
    db_file = str(tmp_path / "writeragent_web_cache.db")
    conns = [
        _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
        for _idx in range(4)
    ]
    patcher, created = _patch_https(conns)
    with (
        patcher,
        patch("plugin.framework.client.http_transport.remember_host_gap") as remember_gap,
    ):
        result = DuckDuckGoSearchTool(cache_max_age_days=30, cache_path=db_file, cache_max_mb=10).forward("AI news")

    assert result == default_tools._SEARCH_UNAVAILABLE
    assert len(created) == 4
    assert all(c.request.call_count == 1 for c in conns)
    assert _web_cache_get(db_file, "search", _search_cache_key("AI news", None), max_age_days=30) is None
    remember_gap.assert_not_called()


def test_web_search_403_is_retried_like_a_challenge():
    conn1 = _keepalive_conn([_response(403, b"<html><body>forbidden</body></html>", "Forbidden")])
    conn2 = _keepalive_conn([_response(200, _ddg_html("Recovered 403"), "OK")])
    patcher, created = _patch_https([conn1, conn2])
    with patcher:
        result = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")

    assert "Recovered 403" in result
    assert len(created) == 2
    assert conn2.request.call_args.args == ("GET", "/lite/?q=AI+news")
    assert conn2.request.call_args.kwargs["body"] is None


def test_web_search_challenge_wait_uses_backoff_and_stop_aborts_it(monkeypatch):
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_DELAYS_SEC", (30.0,))
    conn = _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
    patcher, _created = _patch_https([conn])
    recorded_delays: list[float] = []

    def _fake_wait(delay: float, _cancel) -> bool:
        recorded_delays.append(delay)
        return len(recorded_delays) == 1

    with patcher, patch("plugin.contrib.smolagents.default_tools.wait_abortable", side_effect=_fake_wait):
        result = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")

    assert result == default_tools._SEARCH_STOPPED
    assert len(recorded_delays) == 2
    assert 24.0 <= recorded_delays[1] <= 36.0


def test_web_search_spaces_query_starts(monkeypatch):
    monkeypatch.setattr(default_tools, "_SEARCH_MIN_GAP_SEC", 30.0)
    delays: list[float] = []

    def _fake_wait(delay: float, _cancel) -> bool:
        delays.append(delay)
        return True

    conn = _keepalive_conn([
        _response(200, _ddg_html("First"), "OK"),
        _response(200, _ddg_html("Second"), "OK"),
    ])
    patcher, _created = _patch_https([conn])

    with patcher, patch("plugin.contrib.smolagents.default_tools.wait_abortable", side_effect=_fake_wait):
        DuckDuckGoSearchTool(cache_max_age_days=30).forward("first query")
        DuckDuckGoSearchTool(cache_max_age_days=30).forward("second query")

    assert len(delays) == 2
    assert delays[0] <= 0.0
    assert 0.0 < delays[1] <= 30.0


def test_web_search_gate_blocks_other_threads_during_challenge_backoff(monkeypatch):
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_DELAYS_SEC", (1.0,))
    in_backoff = threading.Event()
    backoff_release = threading.Event()
    requests_seen: list[str] = []
    requests_lock = threading.Lock()
    wait_calls = 0

    def _fake_wait(delay: float, _cancel) -> bool:
        nonlocal wait_calls
        wait_calls += 1
        if delay > 0 and wait_calls == 2:
            in_backoff.set()
            assert backoff_release.wait(timeout=5)
        return True

    conn1 = _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
    conn2 = _keepalive_conn([
        _response(200, _ddg_html("A Result"), "OK"),
        _response(200, _ddg_html("B Result"), "OK"),
    ])

    def _record_request(_method, path, body=None, _headers=None, **_kwargs):
        raw_qs = body.decode("utf-8") if body is not None else urllib.parse.urlsplit(path).query
        q = dict(urllib.parse.parse_qsl(raw_qs)).get("q", "")
        with requests_lock:
            requests_seen.append(q)

    conn1.request.side_effect = _record_request
    conn2.request.side_effect = _record_request
    patcher, created = _patch_https([conn1, conn2])
    results: dict[str, str] = {}

    def _run(name: str, query: str) -> None:
        results[name] = DuckDuckGoSearchTool(cache_max_age_days=30).forward(query)

    with patcher, patch("plugin.contrib.smolagents.default_tools.wait_abortable", side_effect=_fake_wait):
        t_a = threading.Thread(target=_run, args=("a", "query A"))
        t_a.start()
        assert in_backoff.wait(timeout=5)
        assert requests_seen == ["query A"]

        t_b = threading.Thread(target=_run, args=("b", "query B"))
        t_b.start()
        time.sleep(0.05)
        assert requests_seen == ["query A"]

        backoff_release.set()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

    assert len(created) == 2
    assert requests_seen == ["query A", "query A", "query B"]
    assert "A Result" in results["a"]
    assert "B Result" in results["b"]


def test_web_search_challenge_cools_down_next_query(monkeypatch):
    monkeypatch.setattr(default_tools, "_SEARCH_MIN_GAP_SEC", 1.5)
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_DELAYS_SEC", (3.0,))
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_COOLDOWN_SEC", 3.0)
    conn1 = _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
    conn2 = _keepalive_conn([
        _response(200, _ddg_html("Recovered"), "OK"),
        _response(200, _ddg_html("Next"), "OK"),
    ])
    patcher, _created = _patch_https([conn1, conn2])
    delays: list[float] = []

    def _fake_wait(delay: float, _cancel) -> bool:
        delays.append(delay)
        return True

    with patcher, patch("plugin.contrib.smolagents.default_tools.wait_abortable", side_effect=_fake_wait):
        first = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")
        second = DuckDuckGoSearchTool(cache_max_age_days=30).forward("more news")

    assert "Recovered" in first
    assert "Next" in second
    assert len(delays) == 3
    assert delays[0] <= 0.0
    assert 2.4 <= delays[1] <= 3.6
    assert 1.5 < delays[2] <= 3.0


def test_web_search_gave_up_cools_down_and_resets_connection(monkeypatch):
    monkeypatch.setattr(default_tools, "_SEARCH_MIN_GAP_SEC", 1.5)
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_DELAYS_SEC", (0.0,))
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_COOLDOWN_SEC", 3.0)
    conn1 = _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
    conn2 = _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
    conn3 = _keepalive_conn([_response(200, _ddg_html("Fresh"), "OK")])
    patcher, created = _patch_https([conn1, conn2, conn3])
    delays: list[float] = []

    def _fake_wait(delay: float, _cancel) -> bool:
        delays.append(delay)
        return True

    with patcher, patch("plugin.contrib.smolagents.default_tools.wait_abortable", side_effect=_fake_wait):
        first = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")
        second = DuckDuckGoSearchTool(cache_max_age_days=30).forward("more news")

    assert first == default_tools._SEARCH_UNAVAILABLE
    assert "Fresh" in second
    assert len(created) == 3
    conn2.close.assert_called()
    assert conn3.request.call_args.args == ("POST", "/lite/")
    assert 1.5 < delays[-1] <= 3.0


def test_web_search_stop_in_backoff_resets_connection_for_next_query(monkeypatch):
    monkeypatch.setattr(default_tools, "_SEARCH_CHALLENGE_DELAYS_SEC", (30.0,))
    conn1 = _keepalive_conn([_response(202, b"<html><body>challenge</body></html>", "Accepted")])
    conn2 = _keepalive_conn([_response(200, _ddg_html("Fresh"), "OK")])
    patcher, created = _patch_https([conn1, conn2])

    def _abort_on_backoff(delay: float, _cancel) -> bool:
        return delay <= 0.0

    with patcher:
        with patch("plugin.contrib.smolagents.default_tools.wait_abortable", side_effect=_abort_on_backoff):
            stopped = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")
        recovered = DuckDuckGoSearchTool(cache_max_age_days=30).forward("more news")

    assert stopped == default_tools._SEARCH_STOPPED
    assert "Fresh" in recovered
    assert len(created) == 2
    conn1.close.assert_called()
    assert conn2.request.call_args.args == ("POST", "/lite/")


def test_web_search_stop_while_waiting_for_gate_returns_stopped():
    cancel = SendCancellation()
    out: list[str] = []

    def _run() -> None:
        out.append(DuckDuckGoSearchTool(cache_max_age_days=30, cancel=cancel).forward("AI news"))

    default_tools._SEARCH_GATE.acquire()
    try:
        with patch("http.client.HTTPSConnection") as https_conn:
            t = threading.Thread(target=_run)
            t.start()
            time.sleep(0.05)
            assert t.is_alive()
            cancel.cancel()
            t.join(timeout=5)
    finally:
        default_tools._SEARCH_GATE.release()

    assert out == [default_tools._SEARCH_STOPPED]
    https_conn.assert_not_called()


def test_web_search_cache_key_normalizes_case_and_trailing_punctuation():
    assert _search_cache_key("  AI   News? ", None) == "any|ai news"
    assert _search_cache_key("AI news ?", None) == "any|ai news"
    assert _search_cache_key('"exact phrase"', None) == 'any|"exact phrase"'


def test_web_search_recency_day_and_week_shorten_cache_age():
    seen_ages: list[int] = []

    def _fake_cache_get(_db_path, _kind, _key, max_age_days, **_kwargs):
        seen_ages.append(max_age_days)
        return None

    with (
        patch("plugin.contrib.smolagents.default_tools._web_cache_get", side_effect=_fake_cache_get),
        patch("plugin.contrib.smolagents.default_tools.sync_request", return_value=_http_ok(b"<html></html>")),
    ):
        tool = DuckDuckGoSearchTool(cache_max_age_days=30, cache_path="x", cache_max_mb=1)
        for recency in ("day", "week", "month", None):
            tool.forward("AI news", recency=recency)
        DuckDuckGoSearchTool(cache_max_age_days=3, cache_path="x", cache_max_mb=1).forward("AI news", recency="week")

    assert seen_ages == [1, 7, 30, 30, 3]


def test_web_search_recency_day_does_not_refresh_created_at(tmp_path):
    import sqlite3

    db_file = str(tmp_path / "web_cache.db")
    html = _ddg_html("Fresh Headline")
    tool = DuckDuckGoSearchTool(cache_max_age_days=30, cache_path=db_file, cache_max_mb=10)
    with patch("plugin.contrib.smolagents.default_tools.sync_request", return_value=_http_ok(html)):
        tool.forward("AI news", recency="day")
        tool.forward("AI news", recency=None)

    backdated = time.time() - 20 * 3600
    with sqlite3.connect(db_file) as conn:
        conn.execute("UPDATE web_cache SET created_at = ? WHERE kind = 'search'", (backdated,))
        conn.commit()

    with patch("plugin.contrib.smolagents.default_tools.sync_request") as req:
        assert "Fresh Headline" in tool.forward("AI news", recency="day")
        assert "Fresh Headline" in tool.forward("AI news", recency=None)
    req.assert_not_called()

    with sqlite3.connect(db_file) as conn:
        day_ts = conn.execute("SELECT created_at FROM web_cache WHERE kind = 'search' AND key = 'day|ai news'").fetchone()[0]
        any_ts = conn.execute("SELECT created_at FROM web_cache WHERE kind = 'search' AND key = 'any|ai news'").fetchone()[0]

    assert day_ts == pytest.approx(backdated)
    assert any_ts > backdated
