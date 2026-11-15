"""Tests for DuckDuckGoSearchTool in plugin.contrib.smolagents.default_tools."""

import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from plugin.contrib.smolagents import default_tools
from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, _search_cache_key, _web_cache_get
from plugin.framework.client.request_controls import (
    RETRY_MAX_ATTEMPTS,
    reset_host_pacing_for_tests,
    reset_local_unverified_hosts_for_tests,
)
from plugin.framework.send_cancellation import SendCancellation


@pytest.fixture(autouse=True)
def _reset_host_pacing():
    reset_host_pacing_for_tests()
    reset_local_unverified_hosts_for_tests()
    yield
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


def test_web_search_recency_sets_df_parameter():
    for recency, expected_df in [("day", "d"), ("week", "w"), ("month", "m"), ("year", "y")]:
        captured = {}
        html = b"<html><body>no results</body></html>"

        def _fake_sync_request(_url, data=None, _headers=None, **_kwargs):
            captured["params"] = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
            return html

        with patch("plugin.contrib.smolagents.default_tools.sync_request", side_effect=_fake_sync_request):
            DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news", recency=recency)
        assert captured["params"].get("df") == expected_df, f"recency={recency} should map to df={expected_df}"


def test_web_search_recency_any_or_none_omits_df():
    for recency in [None, "any"]:
        captured = {}

        def _fake_sync_request(_url, data=None, _headers=None, **_kwargs):
            captured["params"] = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
            return b"<html><body>no results</body></html>"

        with patch("plugin.contrib.smolagents.default_tools.sync_request", side_effect=_fake_sync_request):
            DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news", recency=recency)
        assert "df" not in captured["params"], f"recency={recency} should omit df"


def test_web_search_cache_key_includes_recency():
    plain = _search_cache_key("AI news", None)
    assert plain == "any|AI news"
    assert _search_cache_key("AI news", "day") != plain
    assert _search_cache_key("AI news", "week") != _search_cache_key("AI news", "day")


def test_web_search_unknown_recency_behaves_as_any():
    assert _search_cache_key("AI news", "d") == "any|AI news"
    assert _search_cache_key("AI news", 7) == "any|AI news"

    captured = {}

    def _fake_sync_request(_url, data=None, _headers=None, **_kwargs):
        captured["params"] = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
        return b"<html><body>no results</body></html>"

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

    with patch("plugin.contrib.smolagents.default_tools.sync_request", return_value=html):
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

    with patch("plugin.contrib.smolagents.default_tools.sync_request", return_value=html):
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
        return_value=b"<html><body>no results</body></html>",
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
        return b"<html><body>no results</body></html>"

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


def test_web_search_http_202_is_unavailable_not_no_results():
    patcher, _created = _patch_https([_conn_returning(_response(202, b"<html><body>challenge</body></html>", "Accepted"))])
    with patcher:
        result = DuckDuckGoSearchTool(cache_max_age_days=30).forward("AI news")

    assert result == default_tools._SEARCH_UNAVAILABLE
    assert "No results found" not in result


def test_web_search_stop_returns_without_connecting():
    cancel = SendCancellation()
    cancel.cancel()
    with patch("http.client.HTTPSConnection") as https_conn:
        result = DuckDuckGoSearchTool(cache_max_age_days=30, cancel=cancel).forward("AI news")

    assert result == default_tools._SEARCH_STOPPED
    https_conn.assert_not_called()
