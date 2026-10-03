"""Unit tests for the lightweight multi-provider MCP tunnel helper."""

from unittest.mock import MagicMock, patch

from plugin.mcp.tunnel import (
    TunnelManager,
    _redact_cmd_for_log,
    build_bore_command,
    build_cloudflare_command,
    build_ngrok_command,
    build_tailscale_command,
    detect_tunnel_auth_error,
    normalize_public_base,
    parse_bore_provider_config,
    parse_bore_url,
    parse_cloudflare_url,
    parse_ngrok_url,
    parse_tailscale_url,
    provider_label,
)


def test_build_cloudflare_quick_and_token():
    assert build_cloudflare_command(18765) == [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://localhost:18765",
    ]
    assert build_cloudflare_command(18765, "") == build_cloudflare_command(18765)
    assert build_cloudflare_command(18765, "cf-jwt-token") == [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "run",
        "--token",
        "cf-jwt-token",
    ]


def test_build_bore_from_provider_config():
    assert build_bore_command(18765) == ["bore", "local", "18765", "--to", "bore.pub"]
    assert build_bore_command(18765, "my.relay.example") == [
        "bore",
        "local",
        "18765",
        "--to",
        "my.relay.example",
    ]
    assert build_bore_command(18765, "my.relay.example s3cret") == [
        "bore",
        "local",
        "18765",
        "--to",
        "my.relay.example",
        "--secret",
        "s3cret",
    ]
    assert build_bore_command(18765, "my.relay.example:s3cret") == [
        "bore",
        "local",
        "18765",
        "--to",
        "my.relay.example",
        "--secret",
        "s3cret",
    ]
    assert build_bore_command(18765, "onlysecret") == [
        "bore",
        "local",
        "18765",
        "--to",
        "bore.pub",
        "--secret",
        "onlysecret",
    ]


def test_parse_bore_provider_config():
    assert parse_bore_provider_config("") == ("bore.pub", "")
    assert parse_bore_provider_config("  ") == ("bore.pub", "")
    assert parse_bore_provider_config("host.example") == ("host.example", "")
    assert parse_bore_provider_config("host.example sec") == ("host.example", "sec")
    assert parse_bore_provider_config("localhost:sec") == ("localhost", "sec")
    # IPv6-looking values keep the whole string as server (no colon-split).
    assert parse_bore_provider_config("2001:db8::1") == ("2001:db8::1", "")
    assert parse_bore_provider_config("tok") == ("bore.pub", "tok")


def test_build_ngrok_and_tailscale():
    assert build_ngrok_command(18765) == [
        "ngrok",
        "http",
        "http://localhost:18765",
        "--log",
        "stdout",
        "--log-format",
        "json",
    ]
    assert build_ngrok_command(18765, "secret-token") == [
        "ngrok",
        "http",
        "http://localhost:18765",
        "--log",
        "stdout",
        "--log-format",
        "json",
        "--authtoken",
        "secret-token",
    ]
    assert build_tailscale_command(18765) == ["tailscale", "funnel", "18765"]


def test_parse_cloudflare_url():
    line = "2026-03-25T12:00:00Z INF |  https://abc-123.trycloudflare.com"
    assert parse_cloudflare_url(line) == "https://abc-123.trycloudflare.com"
    assert parse_cloudflare_url("INF Starting tunnel") is None
    # Generic marketing/doc URLs logged by cloudflared banner must be ignored
    assert parse_cloudflare_url("INF Visit https://www.cloudflare.com to manage tunnels") is None
    assert parse_cloudflare_url("INF Docs at https://developers.cloudflare.com/pages") is None
    # Token tunnels may log a custom hostname.
    assert parse_cloudflare_url("INF | https://mcp.example.com") == "https://mcp.example.com"



def test_parse_bore_url_adds_http_scheme():
    assert parse_bore_url("listening at bore.pub:45123") == "http://bore.pub:45123"
    assert parse_bore_url("waiting…") is None


def test_parse_ngrok_url_from_json():
    line = '{"msg":"started tunnel","url":"https://abc.ngrok-free.app"}'
    assert parse_ngrok_url(line) == "https://abc.ngrok-free.app"
    assert parse_ngrok_url('{"msg":"other"}') is None
    assert parse_ngrok_url("not json") is None


def test_parse_tailscale_url():
    line = "Available at https://node.tailnet-name.ts.net/"
    assert parse_tailscale_url(line) == "https://node.tailnet-name.ts.net"
    assert parse_tailscale_url("starting") is None


def test_parse_tailscale_url_multiline_funnel_output():
    """Current `tailscale funnel` puts the public URL on the line after the header."""
    output = (
        "Available on the internet:\n"
        "\n"
        "https://node.tailnet-name.ts.net/\n"
        "|-- / proxy http://127.0.0.1:18765\n"
        "\n"
        "Press Ctrl+C to exit.\n"
    )
    assert parse_tailscale_url(output) == "https://node.tailnet-name.ts.net"
    # The process reader delivers one line at a time.
    found = [parse_tailscale_url(line) for line in output.splitlines()]
    assert found == [None, None, "https://node.tailnet-name.ts.net", None, None, None]
    # `funnel status` sometimes omits the trailing slash.
    status = "Available on the internet:\nhttps://desktop.tailnet.ts.net\n|-- / proxy http://127.0.0.1:8096\n"
    assert parse_tailscale_url(status) == "https://desktop.tailnet.ts.net"
    assert parse_tailscale_url("See https://tailscale.com/kb/1223/tailscale-funnel/") is None


def test_normalize_public_base_and_mcp_url():
    assert normalize_public_base("bore.pub:1") == "http://bore.pub:1"
    assert normalize_public_base("https://x.trycloudflare.com/") == "https://x.trycloudflare.com"
    mgr = TunnelManager()
    mgr._public_url = "bore.pub:45123"
    assert mgr.mcp_public_url() == "http://bore.pub:45123/mcp"
    mgr._public_url = "https://abc-123.trycloudflare.com/"
    assert mgr.mcp_public_url() == "https://abc-123.trycloudflare.com/mcp"
    mgr._public_url = None
    assert mgr.mcp_public_url() is None


def test_provider_label():
    assert provider_label("cloudflare") == "Cloudflare"
    assert provider_label("ngrok") == "Ngrok"
    assert provider_label("unknown") == "Unknown"


def test_start_skips_when_testing_env(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    mgr = TunnelManager()
    assert mgr.start(18765, "bore") is True
    assert mgr.is_running is False


def test_start_fails_unknown_provider(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    assert mgr.start(18765, "not-a-provider") is False


def test_start_fails_when_binary_missing(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    with patch("plugin.mcp.tunnel.binary_available", return_value=False):
        assert mgr.start(18765, "cloudflare") is False
    assert mgr.public_url is None


def test_start_parses_url_and_restarts_on_provider_change(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    started_cmds = []

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        started_cmds.append(list(cmd))

        def start():
            if stderr_cb and cmd[0] == "cloudflared":
                stderr_cb("INF |  https://xyz.trycloudflare.com")
            elif stdout_cb and cmd[0] == "bore":
                stdout_cb("listening at bore.pub:9999")

        proc.start = start
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "cloudflare") is True
        assert mgr.public_url == "https://xyz.trycloudflare.com"
        assert mgr.provider == "cloudflare"

        # Same port+provider → keep running (no second spawn).
        assert mgr.start(18765, "cloudflare") is True
        assert len(started_cmds) == 1

        # Provider change restarts.
        assert mgr.start(18765, "bore") is True
        assert len(started_cmds) == 2
        assert started_cmds[1][0] == "bore"
        assert mgr.public_url == "http://bore.pub:9999"
        assert mgr.provider == "bore"
        assert mgr.mcp_public_url() == "http://bore.pub:9999/mcp"
        mgr.stop()


def test_start_passes_provider_config_per_provider(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    started_cmds = []

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        started_cmds.append(list(cmd))
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "ngrok", provider_token="tok-a") is True
        assert started_cmds[0][-2:] == ["--authtoken", "tok-a"]

        assert mgr.start(18765, "cloudflare", provider_token="cf-tok") is True
        assert started_cmds[1][-2:] == ["--token", "cf-tok"]

        assert mgr.start(18765, "bore", provider_token="relay.example sec") is True
        assert started_cmds[2] == [
            "bore",
            "local",
            "18765",
            "--to",
            "relay.example",
            "--secret",
            "sec",
        ]

        # Tailscale ignores Provider config.
        assert mgr.start(18765, "tailscale", provider_token="ignored") is True
        assert started_cmds[3] == ["tailscale", "funnel", "18765"]
        mgr.stop()


def test_redact_cmd_for_log_masks_secrets():
    assert "super-secret" not in _redact_cmd_for_log(build_ngrok_command(18765, "super-secret"))
    assert "--authtoken ***" in _redact_cmd_for_log(build_ngrok_command(18765, "super-secret"))
    assert "cf-jwt" not in _redact_cmd_for_log(build_cloudflare_command(1, "cf-jwt"))
    assert "--token ***" in _redact_cmd_for_log(build_cloudflare_command(1, "cf-jwt"))
    assert "s3cret" not in _redact_cmd_for_log(build_bore_command(1, "host.example s3cret"))
    assert "--secret ***" in _redact_cmd_for_log(build_bore_command(1, "host.example s3cret"))
    assert _redact_cmd_for_log(build_bore_command(1)) == "bore local 1 --to bore.pub"


def test_detect_tunnel_auth_error():
    assert detect_tunnel_auth_error("ngrok", '{"err":"ERR_NGROK_105"}') == (
        "ngrok authtoken required or invalid"
    )
    assert detect_tunnel_auth_error("cloudflare", "ERR invalid tunnel token") == (
        "cloudflare tunnel token invalid or unauthorized"
    )
    assert detect_tunnel_auth_error("bore", "listening at bore.pub:1") is None


def test_start_sets_last_error_when_binary_missing(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    with patch("plugin.mcp.tunnel.binary_available", return_value=False):
        assert mgr.start(18765, "cloudflare") is False
    assert mgr.last_error == "cloudflared binary not found on PATH"


def test_start_sets_last_error_unknown_provider(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    assert mgr.start(18765, "not-a-provider") is False
    assert mgr.last_error and "unknown" in mgr.last_error


def test_auth_line_and_exit_without_url_set_last_error(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    exit_cb = {"fn": None}

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        exit_cb["fn"] = on_exit_cb

        def start():
            if stdout_cb:
                stdout_cb('{"err":"ERR_NGROK_105: authentication failed"}')

        proc.start = start
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "ngrok") is True
        assert mgr.last_error == "ngrok authtoken required or invalid"
        assert mgr.public_url is None

        # Exit without URL keeps the auth error (does not overwrite).
        exit_cb["fn"](1)
        assert mgr.last_error == "ngrok authtoken required or invalid"
        assert mgr.is_running is False


def test_successful_url_clears_last_error_exit_without_prior_sets_code(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    exit_cb = {"fn": None}

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        exit_cb["fn"] = on_exit_cb

        def start():
            if stderr_cb:
                stderr_cb("INF |  https://ok.trycloudflare.com")

        proc.start = start
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "cloudflare") is True
        assert mgr.public_url == "https://ok.trycloudflare.com"
        assert mgr.last_error is None
        mgr.stop()
        assert mgr.last_error is None

    # Fresh start that exits before URL → generic exit message.
    # Call on_exit after start() returns — production fires it from a worker thread
    # (calling it inside start() while TunnelManager holds _lock would deadlock).
    def _fake_die(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        exit_cb["fn"] = on_exit_cb
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_die),
    ):
        assert mgr.start(18765, "bore") is True
        exit_cb["fn"](2)
        assert "tunnel process exited (code 2)" in (mgr.last_error or "")
        mgr.stop()


def test_tunnel_manager_reconnect_and_url_recovery(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    exit_cb = {"fn": None}
    stdout_cb_ref = {"fn": None}

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        exit_cb["fn"] = on_exit_cb
        stdout_cb_ref["fn"] = stdout_cb

        def start():
            if stdout_cb:
                stdout_cb("listening at bore.pub:1111")

        proc.start = start
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        # 1. Initial successful start
        assert mgr.start(18765, "bore") is True
        assert mgr.public_url == "http://bore.pub:1111"
        assert mgr.retry_count == 0
        assert mgr.is_reconnecting is False

        # 2. Process drops unexpectedly -> enters reconnecting state
        exit_cb["fn"](1)
        assert mgr.is_reconnecting is True
        assert mgr.retry_count == 1
        assert mgr.public_url is None
        assert "reconnecting (attempt 1/5" in (mgr.last_error or "")

        # 3. Simulate timer firing / reconnect attempt -> recovers URL.
        # cancel() only stops the daemon thread. The manager still tracks
        # this timer, so the callback is a live expiry.
        _fire_current_retry_timer(mgr)
        assert mgr.public_url == "http://bore.pub:1111"
        assert mgr.is_reconnecting is False
        assert mgr.retry_count == 0
        assert mgr.last_error is None

        mgr.stop()
        assert mgr.is_reconnecting is False


def test_tunnel_manager_max_retries_failure(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    exits: list = []

    def _fake_die_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        exits.append(on_exit_cb)
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_die_process),
    ):
        def _expire_retry() -> None:
            # The exit callback arms a real Timer. Fire its callback on this
            # thread so the identity guard sees the timer still being tracked.
            _fire_current_retry_timer(mgr)

        assert mgr.start(18765, "bore", max_retries=2) is True
        # Attempt 1 drop. A second call on this same callback is stale: the
        # process ref is already cleared, and only the replacement's exit counts.
        exits[0](1)
        assert mgr.is_reconnecting is True
        assert mgr.retry_count == 1
        exits[0](1)
        assert mgr.retry_count == 1

        # Retry starts a new process; that process drops -> attempt 2.
        _expire_retry()
        exits[1](1)
        assert mgr.is_reconnecting is True
        assert mgr.retry_count == 2

        # Attempt 3 drop -> max retries (2) exceeded -> FAILED
        _expire_retry()
        exits[2](1)
        assert mgr.is_reconnecting is False
        assert "failed to reconnect after 2 attempts" in (mgr.last_error or "")

        mgr.stop()


def _fire_current_retry_timer(mgr: TunnelManager) -> None:
    """Run the armed reconnect callback on this thread.

    ``Timer.cancel()`` stops the daemon thread. It does not clear the timer
    ``TunnelManager`` is tracking, so this is a live expiry, not a stale one.
    """
    timer = mgr._reconnect_timer
    assert timer is not None
    timer.cancel()
    timer.function(*timer.args, **timer.kwargs)


def test_stale_retry_timer_does_not_spawn_second_process(monkeypatch):
    """A retry callback that already started must not outlive start()'s cancel.

    Timer.cancel() does not stop a callback blocked on the lock start() holds
    across binary_available(). After that start() has spawned the replacement,
    the callback must not start another process.
    """
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    procs: list = []
    exits: list = []

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        procs.append(proc)
        exits.append(on_exit_cb)
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "cloudflare") is True
        exits[0](1)
        assert mgr.is_reconnecting is True
        stale = mgr._reconnect_timer
        assert stale is not None
        stale.cancel()

        # Config change while the callback is already in flight: cancel is a
        # no-op for a running callback. The exited process is already gone
        # (_process is None); start() spawns the replacement (P2).
        assert mgr.start(18765, "bore", provider_token="relay.example") is True
        assert len(procs) == 2
        assert mgr._process is procs[1]

        stale.function(*stale.args, **stale.kwargs)
        assert len(procs) == 2
        assert mgr._process is procs[1]
        procs[1].terminate.assert_not_called()
        assert mgr.is_reconnecting is False

        # The replacement's own exit still arms a timer the stale callback
        # must not clear.
        exits[1](1)
        assert mgr.is_reconnecting is True
        live = mgr._reconnect_timer
        assert live is not None
        assert live is not stale
        stale.function(*stale.args, **stale.kwargs)
        assert mgr._reconnect_timer is live
        assert mgr._process is None
        assert len(procs) == 2
        mgr.stop()


def test_stale_exit_does_not_drop_replacement_process(monkeypatch):
    """Provider/token restart: the old wait thread must not orphan the new process."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    from plugin.mcp.tunnel_state import TunnelStatus

    mgr = TunnelManager()
    exits: list = []
    procs: list = []

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        proc.cmd = list(cmd)
        exits.append(on_exit_cb)
        procs.append(proc)
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "cloudflare") is True
        assert mgr.start(18765, "bore", provider_token="relay.example sec") is True
        assert len(procs) == 2
        procs[0].terminate.assert_called_once()
        assert mgr._process is procs[1]
        assert mgr.provider == "bore"
        assert mgr.status == TunnelStatus.STARTING
        assert mgr._reconnect_timer is None

        # Old cloudflared wait thread exits after bore is already current.
        exits[0](1)
        assert mgr._process is procs[1]
        assert mgr.is_reconnecting is False
        assert mgr.retry_count == 0
        assert mgr._reconnect_timer is None
        procs[1].terminate.assert_not_called()

        # Token change on the same provider is the same terminate-then-start race.
        assert mgr.start(18765, "bore", provider_token="other-secret") is True
        assert mgr._process is procs[2]
        exits[1](1)
        assert mgr._process is procs[2]
        assert mgr.is_reconnecting is False
        assert mgr._reconnect_timer is None

        # The live process exiting still reconnects.
        exits[2](1)
        assert mgr._process is None
        assert mgr.is_reconnecting is True
        assert mgr.retry_count == 1
        mgr.stop()


def test_leaving_tailscale_resets_funnel_for_old_provider(monkeypatch):
    """Tailscale → other must run funnel/serve reset even though state.provider already changed."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    procs: list = []
    exits: list = []

    def _run(cmd, **kwargs):
        reset_cmds.append(list(cmd))
        completed = MagicMock()
        completed.returncode = 0
        return completed

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        procs.append(proc)
        exits.append(on_exit_cb)
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "tailscale") is True
        # pre_start resets before the funnel process is spawned.
        assert reset_cmds == [
            ["tailscale", "funnel", "reset"],
            ["tailscale", "serve", "reset"],
        ]
        assert mgr.provider == "tailscale"

        assert mgr.start(18765, "cloudflare") is True
        assert mgr.provider == "cloudflare"
        assert mgr._process is procs[1]
        procs[0].terminate.assert_called_once()
        # post_stop for the provider being left, not cloudflare (which has none).
        assert reset_cmds == [
            ["tailscale", "funnel", "reset"],
            ["tailscale", "serve", "reset"],
            ["tailscale", "funnel", "reset"],
            ["tailscale", "serve", "reset"],
        ]
        # Stale tailscale exit must not drop the cloudflared process.
        exits[0](0)
        assert mgr._process is procs[1]
        assert mgr.is_reconnecting is False
        mgr.stop()


def test_reconnecting_tailscale_reset_without_live_process(monkeypatch):
    """Funnel config outlives the process. Reset it when leaving while reconnecting."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    from plugin.mcp.tunnel_state import TunnelStatus

    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    exits: list = []

    def _run(cmd, **kwargs):
        reset_cmds.append(list(cmd))
        completed = MagicMock()
        completed.returncode = 0
        return completed

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        exits.append(on_exit_cb)
        return proc

    tailscale_reset = [
        ["tailscale", "funnel", "reset"],
        ["tailscale", "serve", "reset"],
    ]

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == tailscale_reset
        exits[0](1)
        assert mgr.is_reconnecting is True
        assert mgr._process is None
        if mgr._reconnect_timer is not None:
            mgr._reconnect_timer.cancel()

        # Provider switch: no process object, but the effect's provider is tailscale.
        assert mgr.start(18765, "cloudflare") is True
        assert reset_cmds == tailscale_reset + tailscale_reset
        mgr.stop()
        assert reset_cmds == tailscale_reset + tailscale_reset

        # Disable while reconnecting.
        assert mgr.start(18765, "tailscale") is True
        exits[-1](1)
        assert mgr._process is None
        assert mgr.is_reconnecting is True
        if mgr._reconnect_timer is not None:
            mgr._reconnect_timer.cancel()
        mgr.stop()
        assert reset_cmds == tailscale_reset + tailscale_reset + tailscale_reset + tailscale_reset

        # Idle stop was already STOPPED. Another stop must not reset again.
        mgr.stop()
        assert reset_cmds == tailscale_reset + tailscale_reset + tailscale_reset + tailscale_reset

        # FAILED also has no process. Giving up must still reset.
        # Restart from STOPPED does not post_stop (already reset above);
        # pre_start resets once, then stop-from-FAILED resets again.
        assert mgr.start(18765, "tailscale", max_retries=0) is True
        exits[-1](1)
        assert mgr.status == TunnelStatus.FAILED
        assert mgr._process is None
        mgr.stop()
        assert reset_cmds == tailscale_reset * 6
        mgr.stop()
        assert reset_cmds == tailscale_reset * 6


def test_test_tunnel_connectivity_binary_missing():
    from plugin.mcp.tunnel import test_tunnel_connectivity

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("subprocess.run", side_effect=FileNotFoundError("not found")),
    ):
        ok, msg, pub_url = test_tunnel_connectivity("cloudflare")
        assert ok is False
        assert "not found on PATH" in msg
        assert pub_url is None


def test_test_tunnel_connectivity_server_not_running():
    from plugin.mcp.tunnel import test_tunnel_connectivity
    from unittest.mock import MagicMock

    mock_res = MagicMock()
    mock_res.stdout = "cloudflared version 2026.1.0\n"
    mock_res.stderr = ""

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("subprocess.run", return_value=mock_res),
        patch("urllib.request.urlopen", side_effect=Exception("Connection refused")),
    ):
        ok, msg, pub_url = test_tunnel_connectivity("cloudflare", port=18765)
        assert ok is True
        assert "is installed and verified" in msg
        assert "MCP server is not currently running" in msg
        assert pub_url is None


def test_test_tunnel_connectivity_live_probe_success():
    from plugin.mcp.tunnel import test_tunnel_connectivity
    from unittest.mock import MagicMock

    mock_res = MagicMock()
    mock_res.stdout = "cloudflared version 2026.1.0\n"

    mock_probe = MagicMock()
    mock_probe.getcode.return_value = 200
    mock_probe.__enter__ = MagicMock(return_value=mock_probe)
    mock_probe.__exit__ = MagicMock(return_value=None)

    mock_tunnel = MagicMock()
    mock_tunnel.is_running = True
    mock_tunnel._provider = "cloudflare"
    mock_tunnel._public_url = "https://test-live.trycloudflare.com"
    mock_tunnel.mcp_public_url.return_value = "https://test-live.trycloudflare.com/mcp"

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("subprocess.run", return_value=mock_res),
        patch("urllib.request.urlopen", return_value=mock_probe),
        patch("plugin.mcp._shared_tunnel", mock_tunnel),
    ):
        ok, msg, pub_url = test_tunnel_connectivity("cloudflare", port=18765)
        assert ok is True
        assert "tunnel is running" in msg or "tunnel is active" in msg
        assert pub_url == "https://test-live.trycloudflare.com/mcp"



def test_build_mcp_config_snippet_with_custom_and_local_url():
    import json
    from plugin.mcp.mcp_ui import build_mcp_config_snippet

    local_snippet = build_mcp_config_snippet(port=18765)
    local_data = json.loads(local_snippet)
    assert local_data["mcpServers"]["libreoffice"]["url"] == "http://localhost:18765/mcp"

    tunnel_url = "https://abc.trycloudflare.com/mcp"
    tunnel_snippet = build_mcp_config_snippet(url=tunnel_url)
    tunnel_data = json.loads(tunnel_snippet)
    assert tunnel_data["mcpServers"]["libreoffice"]["url"] == "https://abc.trycloudflare.com/mcp"


def test_sync_mcp_config_snippet_reacts_to_checkbox_and_custom_url():
    import json
    from unittest.mock import MagicMock
    from plugin.mcp.mcp_ui import (
        sync_mcp_config_snippet,
        McpTunnelEnabledListener,
        McpPortTextListener,
        _retired_provider_tunnel_urls,
        _tested_provider_tunnel_urls,
    )

    _tested_provider_tunnel_urls.clear()
    _retired_provider_tunnel_urls.clear()

    mock_dlg = MagicMock()
    mock_snippet = MagicMock()
    mock_port = MagicMock()
    mock_port.getValue.return_value = 19000
    mock_port.getText.return_value = "19000"

    mock_checkbox = MagicMock()
    mock_checkbox.getState.return_value = 0  # Unchecked

    mock_provider = MagicMock()
    mock_provider.getText.return_value = "cloudflare"

    controls = {
        "mcp__client_config_snippet": mock_snippet,
        "mcp__mcp_port": mock_port,
        "mcp__tunnel_enabled": mock_checkbox,
        "mcp__tunnel_provider": mock_provider,
    }
    mock_dlg.getControl.side_effect = lambda k: controls.get(k)

    # 1. Unchecked checkbox -> local URL
    sync_mcp_config_snippet(mock_dlg)
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "http://localhost:19000/mcp"

    # 2. Checkbox toggled to checked with custom URL for cloudflare -> tunnel URL
    mock_checkbox.getState.return_value = 1
    mock_checkbox.State = 1
    sync_mcp_config_snippet(mock_dlg, custom_tunnel_url="https://abc.trycloudflare.com/mcp", custom_provider="cloudflare")
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "https://abc.trycloudflare.com/mcp"

    # 3. Switching provider to bore (untested) -> shows bore default template, NOT cloudflare URL!
    mock_provider.getText.return_value = "bore"
    from plugin.chatbot.dialog_views import McpTunnelProviderListener
    provider_listener = McpTunnelProviderListener(mock_dlg)
    provider_listener.itemStateChanged(MagicMock())
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "http://bore.pub:<remote-port>/mcp"

    # 4. Switching provider to ngrok (untested) -> shows ngrok default template
    mock_provider.getText.return_value = "ngrok"
    provider_listener.itemStateChanged(MagicMock())
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "https://<domain>.ngrok-free.app/mcp"

    # 5. Switching back to cloudflare -> shows tested cloudflare URL again
    mock_provider.getText.return_value = "cloudflare"
    provider_listener.itemStateChanged(MagicMock())
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "https://abc.trycloudflare.com/mcp"

    # 6. Checkbox toggled back to unchecked -> immediately reverts to local URL
    mock_checkbox.getState.return_value = 0
    mock_checkbox.State = 0
    listener = McpTunnelEnabledListener(mock_dlg)
    listener.itemStateChanged(MagicMock())
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "http://localhost:19000/mcp"

    # 7. Port changed while unchecked -> updates local URL
    mock_port.getValue.return_value = 20000
    port_listener = McpPortTextListener(mock_dlg)
    port_listener.textChanged(MagicMock())
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "http://localhost:20000/mcp"




