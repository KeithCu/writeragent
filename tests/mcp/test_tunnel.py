"""Unit tests for the lightweight multi-provider MCP tunnel helper."""

from unittest.mock import MagicMock, patch

from plugin.mcp.tunnel import (
    TunnelManager,
    _redact_cmd_for_log,
    _tailscale_off_commands,
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
    ]
    assert build_bore_command(18765, "my.relay.example:s3cret") == [
        "bore",
        "local",
        "18765",
        "--to",
        "my.relay.example",
    ]
    assert build_bore_command(18765, "onlysecret") == [
        "bore",
        "local",
        "18765",
        "--to",
        "bore.pub",
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
    ]
    assert build_tailscale_command(18765) == ["tailscale", "funnel", "18765"]


def test_tailscale_off_commands_only_stops_funnel_port():
    cmds = _tailscale_off_commands(18765)
    assert cmds == (["tailscale", "funnel", "18765", "off"],)
    # Must not run serve --https=443 off to avoid wiping user serve config
    assert not any("serve" in cmd for cmd in cmds)


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
    # Real `tailscale funnel` stdout (serve_v2.go messageForPort). The URL is
    # alone on a line; AsyncProcess delivers one line at a time.
    output = "\n".join(
        [
            "Available on the internet:",
            "",
            "https://node.tailnet-name.ts.net/",
            "|-- proxy http://127.0.0.1:18765",
        ]
    )
    urls = [parse_tailscale_url(line) for line in output.splitlines()]
    assert urls == [None, None, "https://node.tailnet-name.ts.net", None]
    # Docs sample omits the trailing slash; non-443 Funnel keeps the port.
    assert parse_tailscale_url("https://amelie-workstation.pango-lin.ts.net") == (
        "https://amelie-workstation.pango-lin.ts.net"
    )
    assert parse_tailscale_url("https://node.tailnet-name.ts.net:8443/") == (
        "https://node.tailnet-name.ts.net:8443"
    )
    assert parse_tailscale_url("starting") is None
    assert parse_tailscale_url("Available on the internet:") is None


def test_tailscale_funnel_stdout_publishes_url(monkeypatch):
    """One line at a time, as AsyncProcess._read_stream delivers funnel stdout."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    from plugin.mcp.tunnel_state import TunnelStatus

    mgr = TunnelManager()
    background: list = []
    funnel_stdout = [
        "Available on the internet:",
        "",
        "https://node.tailnet-name.ts.net/",
        "|-- proxy http://127.0.0.1:18765",
    ]

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True

        def start():
            if stdout_cb and cmd[0] == "tailscale":
                for line in funnel_stdout:
                    stdout_cb(line)

        proc.start = start
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", return_value=MagicMock(returncode=0)),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert mgr.status == TunnelStatus.CONNECTED
        assert mgr.public_url == "https://node.tailnet-name.ts.net"
        assert mgr.mcp_public_url() == "https://node.tailnet-name.ts.net/mcp"
        mgr.stop()
        _join_recorded_background(background)


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
    started_envs = []
    background: list = []

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        started_cmds.append(list(cmd))
        started_envs.append(kwargs.get("env", {}))
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        assert mgr.start(18765, "ngrok", provider_token="tok-a") is True
        assert started_envs[0].get("NGROK_AUTHTOKEN") == "tok-a"

        assert mgr.start(18765, "cloudflare", provider_token="cf-tok") is True
        assert started_envs[1].get("TUNNEL_TOKEN") == "cf-tok"

        assert mgr.start(18765, "bore", provider_token="relay.example sec") is True
        assert started_envs[2].get("BORE_SECRET") == "sec"
        assert started_cmds[2] == [
            "bore",
            "local",
            "18765",
            "--to",
            "relay.example",
        ]

        # Tailscale ignores Provider config.
        assert mgr.start(18765, "tailscale", provider_token="ignored") is True
        assert started_cmds[3] == ["tailscale", "funnel", "18765"]
        mgr.stop()
        _join_recorded_background(background)


def test_redact_cmd_for_log_masks_secrets():
    assert "super-secret" not in _redact_cmd_for_log(["ngrok", "http", "80", "--authtoken", "super-secret"])
    assert "--authtoken ***" in _redact_cmd_for_log(["ngrok", "http", "80", "--authtoken", "super-secret"])
    assert "cf-jwt" not in _redact_cmd_for_log(["cloudflared", "tunnel", "--token", "cf-jwt"])
    assert "--token ***" in _redact_cmd_for_log(["cloudflared", "tunnel", "--token", "cf-jwt"])
    assert "s3cret" not in _redact_cmd_for_log(["bore", "local", "1", "--secret", "s3cret"])
    assert "--secret ***" in _redact_cmd_for_log(["bore", "local", "1", "--secret", "s3cret"])
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


def test_start_returns_false_when_status_is_failed_with_custom_message(monkeypatch):
    """start() returns False on TunnelStatus.FAILED regardless of last_error text."""
    import dataclasses
    from plugin.mcp.tunnel_state import TunnelStatus

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()

    def _fail_spawn(effect):
        with mgr._lock:
            mgr._state = dataclasses.replace(
                mgr._state,
                status=TunnelStatus.FAILED,
                last_error="unrecognized custom error message",
                desired_running=False,
            )

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch.object(mgr, "_spawn_process_unlocked", side_effect=_fail_spawn),
    ):
        assert mgr.start(18765, "cloudflare") is False
        assert mgr.status == TunnelStatus.FAILED
        assert mgr.last_error == "unrecognized custom error message"


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
        assert mgr.start(18765, "ngrok") is False
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

    Timer.cancel() does not stop a callback that is already in flight.
    After start() has spawned the replacement, the callback must not
    start another process.
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


def _track_background(handles: list):
    """Record real post_stop threads so the test can join them.

    What was wrong: tests that let run_in_background finish after the
    subprocess.run patch came off exec'd `tailscale` and
    _clear_tailscale_arm() deleted the next test's monkeypatched marker
    (macOS CI 37719597720).
    Why: join the handles before that patch exits, while subprocess.run
    is still the mock.
    """
    from plugin.framework.worker_pool import run_in_background as real

    def _wrap(func, *args, **kwargs):
        handle = real(func, *args, **kwargs)
        handles.append(handle)
        return handle

    return _wrap


def _join_recorded_background(handles: list) -> None:
    for handle in handles:
        handle.join(timeout=5)
        assert not handle.is_alive()


def test_leaving_tailscale_resets_funnel_for_old_provider(monkeypatch):
    """Tailscale → other must run funnel/serve reset even though state.provider already changed."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    procs: list = []
    exits: list = []
    background: list = []

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
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        assert mgr.start(18765, "tailscale") is True
        # pre_start resets before the funnel process is spawned.
        assert reset_cmds == _tailscale_reset_cmds(18765)
        assert mgr.provider == "tailscale"

        assert mgr.start(18765, "cloudflare") is True
        assert mgr.provider == "cloudflare"
        assert mgr._process is procs[1]
        procs[0].terminate.assert_called_once()
        # post_stop for the provider being left, not cloudflare (which has none).
        import time
        t0 = time.monotonic()
        while len(reset_cmds) < len(_tailscale_reset_cmds(18765)) * 2 and time.monotonic() - t0 < 2:
            time.sleep(0.01)
        assert reset_cmds == _tailscale_reset_cmds(18765) * 2
        # Stale tailscale exit must not drop the cloudflared process.
        exits[0](0)
        assert mgr._process is procs[1]
        assert mgr.is_reconnecting is False
        mgr.stop()
        _join_recorded_background(background)


def test_leaving_tailscale_resets_funnel_for_old_port_on_port_change(monkeypatch):
    """When port changes on provider switch, post_stop must reset old port, not new port."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    procs: list = []
    background: list = []

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
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == _tailscale_reset_cmds(18765)

        # Switch to cloudflare on port 19000
        assert mgr.start(19000, "cloudflare") is True
        import time
        t0 = time.monotonic()
        while len(reset_cmds) < 2 and time.monotonic() - t0 < 2:
            time.sleep(0.01)
        # Old port (18765) must be turned off, NOT the new port (19000)
        assert reset_cmds == [
            ["tailscale", "funnel", "18765", "off"],
            ["tailscale", "funnel", "18765", "off"],
        ]
        mgr.stop()
        _join_recorded_background(background)


def test_reconnecting_tailscale_reset_without_live_process(monkeypatch):
    """Funnel config outlives the process. Reset it when leaving while reconnecting."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    from plugin.mcp.tunnel_state import TunnelStatus

    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    exits: list = []
    background: list = []

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

    tailscale_reset = _tailscale_reset_cmds(18765)

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
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

        # post_stop runs in the background, so wait a bit
        import time
        t0 = time.monotonic()
        while len(reset_cmds) < len(tailscale_reset) * 2 and time.monotonic() - t0 < 2:
            time.sleep(0.01)
        assert reset_cmds == tailscale_reset + tailscale_reset
        mgr.stop()
        assert reset_cmds == tailscale_reset + tailscale_reset

        # Disable while reconnecting.
        assert mgr.start(18765, "tailscale") is True
        # wait for pre_start to finish
        t0 = time.monotonic()
        while len(reset_cmds) < len(tailscale_reset) * 3 and time.monotonic() - t0 < 2:
            time.sleep(0.01)

        exits[-1](1)
        assert mgr._process is None
        assert mgr.is_reconnecting is True
        if mgr._reconnect_timer is not None:
            mgr._reconnect_timer.cancel()
        mgr.stop()

        # wait for post_stop
        t0 = time.monotonic()
        while len(reset_cmds) < len(tailscale_reset) * 4 and time.monotonic() - t0 < 2:
            time.sleep(0.01)
        assert reset_cmds == tailscale_reset + tailscale_reset + tailscale_reset + tailscale_reset

        # Idle stop was already STOPPED. Another stop must not reset again.
        mgr.stop()
        assert reset_cmds == tailscale_reset + tailscale_reset + tailscale_reset + tailscale_reset

        # FAILED has no process. Giving up resets on that transition.
        # Restart from STOPPED does not post_stop (already reset above);
        # pre_start resets once, the FAILED transition resets again, and
        # stop-from-FAILED resets once more. A further idle stop does not.
        assert mgr.start(18765, "tailscale", max_retries=0) is True
        exits[-1](1)
        assert mgr.status == TunnelStatus.FAILED
        assert mgr._process is None
        t0 = time.monotonic()
        while len(reset_cmds) < len(tailscale_reset) * 6 and time.monotonic() - t0 < 2:
            time.sleep(0.01)
        assert reset_cmds == tailscale_reset * 6
        mgr.stop()
        t0 = time.monotonic()
        while len(reset_cmds) < len(tailscale_reset) * 7 and time.monotonic() - t0 < 2:
            time.sleep(0.01)
        assert reset_cmds == tailscale_reset * 7
        mgr.stop()
        assert reset_cmds == tailscale_reset * 7
        _join_recorded_background(background)


def _tailscale_reset_cmds(port: int = 18765) -> list[list[str]]:
    return [
        ["tailscale", "funnel", str(int(port)), "off"],
    ]


def _capture_background(bucket: list):
    def _capture(func, *args, **kwargs):
        del args, kwargs
        bucket.append(func)
        handle = MagicMock()
        handle.join = MagicMock()
        handle.is_alive = MagicMock(return_value=False)
        return handle

    return _capture


def _fake_running_process(spawned: list):
    def _fake(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        del stdout_cb, stderr_cb, on_exit_cb, kwargs
        proc = MagicMock()
        proc.is_running = True
        proc.cmd = cmd
        proc.start = MagicMock()
        proc.terminate = MagicMock()
        spawned.append(proc)
        return proc

    return _fake


def test_stale_tailscale_post_stop_does_not_clear_a_newer_funnel(monkeypatch):
    """A reset scheduled by stop() must no-op after a newer Tailscale start."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    spawned: list = []
    tailscale_reset = _tailscale_reset_cmds(18765)

    def _run(cmd, **kwargs):
        del kwargs
        reset_cmds.append(list(cmd))
        completed = MagicMock()
        completed.returncode = 0
        return completed

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_running_process(spawned)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == _tailscale_reset_cmds(18765)
        mgr.stop()
        assert len(scheduled) == 1
        assert reset_cmds == tailscale_reset
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == tailscale_reset * 2
        scheduled[0]()
        assert reset_cmds == tailscale_reset * 2
        assert len(spawned) == 2


def test_tailscale_post_stop_runs_when_no_newer_session_started(monkeypatch):
    """The same captured reset still runs if start() has not bumped the generation."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    spawned: list = []
    tailscale_reset = _tailscale_reset_cmds(18765)

    def _run(cmd, **kwargs):
        del kwargs
        reset_cmds.append(list(cmd))
        completed = MagicMock()
        completed.returncode = 0
        return completed

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_running_process(spawned)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        assert mgr.start(18765, "tailscale") is True
        mgr.stop()
        assert len(scheduled) == 1
        scheduled[0]()
        assert reset_cmds == tailscale_reset * 2
        assert len(spawned) == 1


def test_tailscale_post_stop_still_runs_after_cloudflare_start(monkeypatch):
    """Cloudflare has no post_stop, so it must not invalidate a pending Tailscale reset."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    spawned: list = []
    tailscale_reset = _tailscale_reset_cmds(18765)

    def _run(cmd, **kwargs):
        del kwargs
        reset_cmds.append(list(cmd))
        completed = MagicMock()
        completed.returncode = 0
        return completed

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_running_process(spawned)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        assert mgr.start(18765, "tailscale") is True
        mgr.stop()
        assert mgr.start(18765, "cloudflare") is True
        assert len(scheduled) == 1
        scheduled[0]()
        assert reset_cmds == tailscale_reset * 2
        assert spawned[-1].cmd[0] == "cloudflared"


def test_stopped_stop_resets_tailscale_when_crash_marker_exists(monkeypatch, tmp_path):
    """A new process is STOPPED on cloudflare. The arm file is what still names Tailscale."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    marker.write_text("armed\n", encoding="utf-8")
    monkeypatch.setattr("plugin.mcp.tunnel._tailscale_arm_path", lambda: str(marker))

    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []

    def _run(cmd, **kwargs):
        del kwargs
        reset_cmds.append(list(cmd))
        completed = MagicMock()
        completed.returncode = 0
        return completed

    with (
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        mgr.stop()
        assert len(scheduled) == 1
        scheduled[0]()
        assert reset_cmds == _tailscale_reset_cmds(18765)
        assert not marker.exists()
        mgr.stop()
        assert len(scheduled) == 1
        assert reset_cmds == _tailscale_reset_cmds()


def test_tailscale_spawn_writes_arm_marker_and_post_stop_clears_it(monkeypatch, tmp_path):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    monkeypatch.setattr("plugin.mcp.tunnel._tailscale_arm_path", lambda: str(marker))

    mgr = TunnelManager()
    scheduled: list = []
    spawned: list = []

    def _run(cmd, **kwargs):
        del cmd, kwargs
        completed = MagicMock()
        completed.returncode = 0
        return completed

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_running_process(spawned)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert marker.is_file()
        assert marker.read_text(encoding="utf-8") == "armed\n"
        mgr.stop()
        assert marker.is_file()
        assert len(scheduled) == 1
        scheduled[0]()
        assert not marker.exists()
        assert len(spawned) == 1


def test_pytest_does_not_use_config_dir_for_tailscale_arm(monkeypatch, tmp_path):
    """PYTEST_CURRENT_TEST forces the marker off even when config has a resolved path."""
    import os

    from plugin.mcp.tunnel import _tailscale_arm_path

    fake_config = tmp_path / "writeragent.json"
    fake_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("plugin.framework.config._resolved_config_path", str(fake_config))
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert _tailscale_arm_path() is None
    assert not (tmp_path / "writeragent-tailscale-funnel-armed").exists()


def test_tailscale_pre_start_does_not_hold_tunnel_lock(monkeypatch):
    """stop() must return while funnel reset is still inside pre_start."""
    import threading

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    spawned: list = []
    background: list = []
    result: dict[str, bool] = {}

    def _run(cmd, **kwargs):
        del cmd, kwargs
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            assert release.wait(2)
        completed = MagicMock()
        completed.returncode = 0
        return completed

    def _start() -> None:
        result["ok"] = mgr.start(18765, "tailscale")

    stopped = threading.Event()

    def _do_stop() -> None:
        mgr.stop()
        stopped.set()

    stopper: threading.Thread | None = None
    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_running_process(spawned)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        worker = threading.Thread(target=_start)
        worker.start()
        try:
            assert entered.wait(2)
            acquired = mgr._lock.acquire(timeout=0.3)
            assert acquired, "pre_start held _lock"
            mgr._lock.release()
            stopper = threading.Thread(target=_do_stop)
            stopper.start()
            assert stopped.wait(1), "stop() blocked behind Tailscale pre_start"
        finally:
            release.set()
        worker.join(2)
        if stopper is not None:
            stopper.join(2)
        _join_recorded_background(background)
    assert not worker.is_alive()
    assert result["ok"] is False
    assert spawned == []
    assert mgr.is_running is False


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

    # 4b. Tailscale placeholder is a Funnel hostname (<machine>.<tailnet>.ts.net).
    mock_provider.getText.return_value = "tailscale"
    provider_listener.itemStateChanged(MagicMock())
    args, _ = mock_snippet.setText.call_args
    data = json.loads(args[0])
    assert data["mcpServers"]["libreoffice"]["url"] == "https://<machine>.<tailnet>.ts.net/mcp"

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


def test_start_does_not_hold_lock_during_binary_probe(monkeypatch):
    """A hung provider --version must not block stop() or the tunnel lock."""
    import threading

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    probed = threading.Event()
    release = threading.Event()
    result: dict[str, bool] = {}

    def _slow(provider: str) -> bool:
        del provider
        probed.set()
        assert release.wait(2)
        return True

    def _run() -> None:
        result["ok"] = mgr.start(18765, "cloudflare")

    stopped = threading.Event()

    def _do_stop() -> None:
        mgr.stop()
        stopped.set()

    stopper: threading.Thread | None = None
    with (
        patch("plugin.mcp.tunnel.binary_available", side_effect=_slow),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=AssertionError("should not spawn")),
    ):
        worker = threading.Thread(target=_run)
        worker.start()
        try:
            assert probed.wait(2)
            acquired = mgr._lock.acquire(timeout=0.3)
            assert acquired, "start() held _lock across binary_available"
            mgr._lock.release()
            stopper = threading.Thread(target=_do_stop)
            stopper.start()
            assert stopped.wait(1), "stop() blocked behind the binary probe"
        finally:
            release.set()
        worker.join(2)
        if stopper is not None:
            stopper.join(2)
    assert not worker.is_alive()
    assert result["ok"] is False
    assert mgr.is_running is False


def test_stop_cancels_a_start_that_was_only_armed(monkeypatch):
    """stop() after note_pending_start must win even if start() has not entered the probe."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    token = object()
    mgr.note_pending_start(token)
    mgr.stop()
    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=AssertionError("should not spawn")),
    ):
        assert mgr.start(18765, "cloudflare", start_token=token) is False
    assert mgr.is_running is False


def test_sync_tunnel_from_main_thread_probes_off_thread(monkeypatch):
    """config:changed must return while provider --version is still running."""
    import threading
    import time

    import plugin.mcp as mcp_mod
    from plugin.mcp.tunnel_state import TunnelStatus

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    monkeypatch.setattr(mcp_mod, "_shared_tunnel", None)
    monkeypatch.setattr(mcp_mod, "_shared_http_server", None)
    mod = mcp_mod.McpModule.__new__(mcp_mod.McpModule)
    mod.name = "mcp"
    services = MagicMock()
    services.config.proxy_for.return_value = {
        "mcp_enabled": True,
        "tunnel_enabled": True,
        "mcp_port": 18765,
        "tunnel_provider": "cloudflare",
        "tunnel_provider_token": "",
    }
    mod._services = services
    bound = MagicMock()
    bound.is_running.return_value = True
    bound.port = 18765
    mod._server = bound
    tunnel = TunnelManager()
    mod._tunnel = tunnel

    caller = threading.current_thread()
    probed = threading.Event()
    release = threading.Event()
    probe_thread: dict[str, threading.Thread] = {}

    def _slow(provider: str) -> bool:
        del provider
        probe_thread["t"] = threading.current_thread()
        probed.set()
        assert release.wait(2)
        return False

    monkeypatch.setattr("plugin.mcp.tunnel.binary_available", _slow)
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    try:
        mod._sync_tunnel()
        assert probed.wait(2)
        assert probe_thread["t"] is not caller
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and tunnel.status != TunnelStatus.FAILED:
        time.sleep(0.02)
    assert tunnel.status == TunnelStatus.FAILED



def test_start_absent_token_in_env_and_args(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    started_envs = []
    started_cmds = []

    def _fake_async_process(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        started_cmds.append(list(cmd))
        started_envs.append(kwargs.get("env", {}))
        proc.start = MagicMock()
        return proc

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=True),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_fake_async_process),
    ):
        assert mgr.start(18765, "cloudflare", provider_token="") is True
        assert "TUNNEL_TOKEN" not in started_envs[-1]
        assert "--token" not in started_cmds[-1]
