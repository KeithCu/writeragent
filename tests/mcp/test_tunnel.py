import dataclasses
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.mcp.tunnel import (
    TailscaleProvider,
    TunnelManager,
    binary_available,
    build_ngrok_command,
    detect_tunnel_auth_error,
    normalize_public_base,
    parse_bore_provider_config,
    parse_bore_url,
    parse_cloudflare_url,
    parse_ngrok_url,
    parse_tailscale_url,
    provider_label,
)


def _make_fake_async_process(
    *,
    procs: list | None = None,
    cmds: list | None = None,
    envs: list | None = None,
    flags: list | None = None,
    exits: list | None = None,
    exit_cb: dict | None = None,
    stdout_cb_ref: dict | None = None,
    on_start=None,
):
    def _fake(cmd, stdout_cb=None, stderr_cb=None, on_exit_cb=None, **kwargs):
        proc = MagicMock()
        proc.is_running = True
        proc.cmd = list(cmd)
        proc.terminate = MagicMock()
        if procs is not None:
            procs.append(proc)
        if cmds is not None:
            cmds.append(list(cmd))
        if envs is not None:
            envs.append(kwargs.get("env", {}))
        if flags is not None:
            flags.append(kwargs.get("die_with_parent"))
        if exits is not None:
            exits.append(on_exit_cb)
        if exit_cb is not None:
            exit_cb["fn"] = on_exit_cb
        if stdout_cb_ref is not None:
            stdout_cb_ref["fn"] = stdout_cb
        if on_start is not None:
            proc.start = lambda: on_start(cmd, stdout_cb, stderr_cb)
        else:
            proc.start = MagicMock()
        return proc

    return _fake


def _record_subprocess_run(reset_cmds: list[list[str]] | None = None, *, timeouts: list | None = None, returncode: int = 0):
    def _run(cmd, **kwargs):
        if reset_cmds is not None:
            reset_cmds.append(list(cmd))
        if timeouts is not None:
            timeouts.append(kwargs.get("timeout"))
        completed = MagicMock()
        completed.returncode = returncode
        return completed

    return _run


def test_parse_bore_provider_config():
    assert parse_bore_provider_config("") == ("bore.pub", "")
    assert parse_bore_provider_config("  ") == ("bore.pub", "")
    assert parse_bore_provider_config("host.example") == ("host.example", "")
    assert parse_bore_provider_config("host.example sec") == ("host.example", "sec")
    assert parse_bore_provider_config("localhost:sec") == ("localhost", "sec")
    # IPv6-looking values keep the whole string as server (no colon-split).
    assert parse_bore_provider_config("2001:db8::1") == ("2001:db8::1", "")
    assert parse_bore_provider_config("tok") == ("bore.pub", "tok")


def test_build_ngrok_ignores_token():
    cmd = build_ngrok_command(18765, "secret-token")
    assert cmd == build_ngrok_command(18765)
    assert "secret-token" not in cmd


def test_parse_cloudflare_url():
    line = "2026-03-25T12:00:00Z INF |  https://abc-123.trycloudflare.com"
    assert parse_cloudflare_url(line) == "https://abc-123.trycloudflare.com"
    assert parse_cloudflare_url("INF Starting tunnel") is None
    # Generic marketing/doc URLs logged by cloudflared banner must be ignored
    assert parse_cloudflare_url("INF Visit https://www.cloudflare.com to manage tunnels") is None
    assert parse_cloudflare_url("INF Docs at https://developers.cloudflare.com/pages") is None
    # Token tunnels may log a custom hostname.
    assert parse_cloudflare_url("INF | https://mcp.example.com") == "https://mcp.example.com"
    assert parse_cloudflare_url("INF connected https://1.1.1.1") is None
    assert parse_cloudflare_url("INF | https://1.1.1.1") is None
    assert parse_cloudflare_url("see https://mcp.example.com for docs") is None
    assert parse_cloudflare_url("INF | https://api.cloudflare.com") is None



def test_parse_bore_url_adds_http_scheme():
    assert parse_bore_url("listening at bore.pub:45123") == "http://bore.pub:45123"
    assert parse_bore_url("waiting…") is None


def test_parse_ngrok_url_from_json():
    line = '{"msg":"started tunnel","url":"https://abc.ngrok-free.app"}'
    assert parse_ngrok_url(line) == "https://abc.ngrok-free.app"
    assert parse_ngrok_url('{"msg":"other"}') is None
    assert parse_ngrok_url("not json") is None
    assert parse_ngrok_url("{bad json") is None


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

    def _on_start(cmd, stdout_cb, _stderr_cb):
        if stdout_cb and cmd[0] == "tailscale":
            for line in funnel_stdout:
                stdout_cb(line)

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", return_value=MagicMock(returncode=0)),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(on_start=_on_start),
        ),
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
    mgr._state = dataclasses.replace(mgr._state, public_url="bore.pub:45123")
    assert mgr.mcp_public_url() == "http://bore.pub:45123/mcp"
    mgr._state = dataclasses.replace(mgr._state, public_url="https://abc-123.trycloudflare.com/")
    assert mgr.mcp_public_url() == "https://abc-123.trycloudflare.com/mcp"
    mgr._state = dataclasses.replace(mgr._state, public_url=None)
    assert mgr.mcp_public_url() is None


def test_provider_label():
    assert provider_label("cloudflare") == "Cloudflare"
    assert provider_label("ngrok") == "Ngrok"
    assert provider_label("unknown") == "Unknown"


def test_start_skips_when_testing_env(monkeypatch):
    from plugin.framework.queue_executor import Policy, main

    main.install(Policy.test_inline)
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
    with patch("plugin.mcp.tunnel.binary_available", return_value="cloudflared binary not found on PATH"):
        assert mgr.start(18765, "cloudflare") is False
    assert mgr.public_url is None


def test_start_parses_url_and_restarts_on_provider_change(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    started_cmds = []

    def _on_start(cmd, stdout_cb, stderr_cb):
        if stderr_cb and cmd[0] == "cloudflared":
            stderr_cb("INF |  https://xyz.trycloudflare.com")
        elif stdout_cb and cmd[0] == "bore":
            stdout_cb("listening at bore.pub:9999")

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(cmds=started_cmds, on_start=_on_start),
        ),
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
    started_flags = []
    background: list = []

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(
                cmds=started_cmds, envs=started_envs, flags=started_flags
            ),
        ),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        assert mgr.start(18765, "ngrok", provider_token="tok-a") is True
        assert started_envs[0].get("NGROK_AUTHTOKEN") == "tok-a"

        assert mgr.start(18765, "cloudflare", provider_token="cf-tok") is True
        assert started_envs[1].get("TUNNEL_TOKEN") == "cf-tok"
        assert "cf-tok" not in started_cmds[1]

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
        assert started_flags == [True, True, True, True]
        mgr.stop()
        _join_recorded_background(background)


def test_detect_tunnel_auth_error():
    assert detect_tunnel_auth_error("ngrok", '{"err":"ERR_NGROK_105"}') == (
        "ngrok authtoken required or invalid"
    )
    assert detect_tunnel_auth_error("cloudflare", "ERR invalid tunnel token") == (
        "cloudflare tunnel token invalid or unauthorized"
    )
    assert detect_tunnel_auth_error("bore", "listening at bore.pub:1") is None
    assert detect_tunnel_auth_error("ngrok", "{bad json") is None


def test_start_sets_last_error_when_binary_missing(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    with patch("plugin.mcp.tunnel.binary_available", return_value="cloudflared binary not found on PATH"):
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
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch.object(mgr, "_spawn_process_unlocked", side_effect=_fail_spawn),
    ):
        assert mgr.start(18765, "cloudflare") is False
        assert mgr.status == TunnelStatus.FAILED
        assert mgr.last_error == "unrecognized custom error message"


def test_auth_line_and_exit_without_url_set_last_error(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    exit_cb = {"fn": None}

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(
                exit_cb=exit_cb,
                on_start=lambda _c, out, _err: out and out('{"err":"ERR_NGROK_105: authentication failed"}'),
            ),
        ),
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

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(
                exit_cb=exit_cb,
                on_start=lambda _c, _out, err: err and err("INF |  https://ok.trycloudflare.com"),
            ),
        ),
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
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
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

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(
                exit_cb=exit_cb,
                stdout_cb_ref=stdout_cb_ref,
                on_start=lambda _c, out, _err: out and out("listening at bore.pub:1111"),
            ),
        ),
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
        assert mgr.retry_count == 1  # reset only after a stable connection drops
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
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
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

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(procs=procs, exits=exits),
        ),
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
    """After provider/token restart, a stale exit waiter must not clear the replacement tailscale process."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    from plugin.mcp.tunnel_state import TunnelStatus

    mgr = TunnelManager()
    exits: list = []
    procs: list = []

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(procs=procs, exits=exits),
        ),
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

    Tests must join background handles before the subprocess.run mock exits.
    Otherwise post_stop can run real tailscale and clear markers the next case needs.
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

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(procs=procs, exits=exits),
        ),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        assert mgr.start(18765, "tailscale") is True
        # pre_start resets before the funnel process is spawned.
        assert reset_cmds == _tailscale_reset_cmds()
        assert mgr.provider == "tailscale"

        assert mgr.start(18765, "cloudflare") is True
        assert mgr.provider == "cloudflare"
        assert mgr._process is procs[1]
        procs[0].terminate.assert_called_once()
        # post_stop for the provider being left, not cloudflare (which has none).
        _join_recorded_background(background)
        assert reset_cmds == _tailscale_reset_cmds() * 2
        # Stale tailscale exit must not drop the cloudflared process.
        exits[0](0)
        assert mgr._process is procs[1]
        assert mgr.is_reconnecting is False
        mgr.stop()
        _join_recorded_background(background)


def test_leaving_tailscale_resets_funnel_for_old_port_on_port_change(monkeypatch):
    """The local port is not the off target; both the pre_start and the post_stop on provider switch use the 443 command, including when the new MCP port is 19000."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    procs: list = []
    background: list = []

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(procs=procs),
        ),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == _tailscale_reset_cmds()

        # Switch to cloudflare on port 19000
        assert mgr.start(19000, "cloudflare") is True
        _join_recorded_background(background)
        assert reset_cmds == [
            ["tailscale", "funnel", "--https=443", "off"],
            ["tailscale", "funnel", "--https=443", "off"],
        ]
        assert "19000" not in reset_cmds[0]
        assert "19000" not in reset_cmds[1]
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

    tailscale_reset = _tailscale_reset_cmds()

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(exits=exits),
        ),
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

        # post_stop runs in the background, so wait for it to finish
        _join_recorded_background(background)
        assert reset_cmds == tailscale_reset + tailscale_reset
        mgr.stop()
        assert reset_cmds == tailscale_reset + tailscale_reset

        # Disable while reconnecting.
        assert mgr.start(18765, "tailscale") is True
        # wait for pre_start to finish
        _join_recorded_background(background)

        exits[-1](1)
        assert mgr._process is None
        assert mgr.is_reconnecting is True
        if mgr._reconnect_timer is not None:
            mgr._reconnect_timer.cancel()
        mgr.stop()

        # wait for post_stop
        _join_recorded_background(background)
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
        _join_recorded_background(background)
        assert reset_cmds == tailscale_reset * 6
        mgr.stop()
        _join_recorded_background(background)
        assert reset_cmds == tailscale_reset * 7
        mgr.stop()
        assert reset_cmds == tailscale_reset * 7
        _join_recorded_background(background)


def _tailscale_reset_cmds() -> list[list[str]]:
    return [
        ["tailscale", "funnel", "--https=443", "off"],
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


def test_stale_tailscale_post_stop_does_not_clear_a_newer_funnel(monkeypatch, tmp_path):
    """A reset scheduled by stop() must no-op after a newer Tailscale start."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    spawned: list = []
    tailscale_reset = _tailscale_reset_cmds()

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_make_fake_async_process(procs=spawned)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == _tailscale_reset_cmds()
        mgr.stop()
        assert len(scheduled) == 1
        assert reset_cmds == tailscale_reset
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == tailscale_reset * 2
        scheduled[0]()
        assert reset_cmds == tailscale_reset * 2
        assert len(spawned) == 2
        assert marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 0


def test_tailscale_post_stop_runs_when_no_newer_session_started(monkeypatch):
    """The same captured reset still runs if start() has not bumped the generation."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    spawned: list = []
    tailscale_reset = _tailscale_reset_cmds()

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_make_fake_async_process(procs=spawned)),
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
    tailscale_reset = _tailscale_reset_cmds()

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_make_fake_async_process(procs=spawned)),
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
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))

    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []

    with (
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        mgr.stop()
        assert len(scheduled) == 1
        assert mgr._providers["tailscale"].reset._resets_in_flight == 1
        mgr.stop()
        assert len(scheduled) == 1
        assert mgr._providers["tailscale"].reset._resets_in_flight == 1
        scheduled[0]()
        assert reset_cmds == _tailscale_reset_cmds()
        assert not marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 0
        mgr.stop()
        assert len(scheduled) == 1
        assert reset_cmds == _tailscale_reset_cmds()


def test_failed_tailscale_reset_keeps_crash_marker_for_retry(monkeypatch, tmp_path):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    marker.write_text("armed\n", encoding="utf-8")
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))

    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    completed = MagicMock()
    completed.returncode = 1
    completed.stderr = "failed"
    completed.stdout = ""

    def _run(cmd, **kwargs):
        del kwargs
        reset_cmds.append(list(cmd))
        return completed

    with (
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        mgr.stop()
        assert len(scheduled) == 1
        scheduled[0]()
        assert marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 0
        assert reset_cmds == [["tailscale", "funnel", "--https=443", "off"]]

        completed.returncode = 0
        completed.stderr = ""
        mgr.stop()
        assert len(scheduled) == 2
        scheduled[1]()
        assert not marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 0

        mgr.stop()
        assert len(scheduled) == 2


def test_tailscale_reset_oserror_keeps_crash_marker_for_retry(monkeypatch, tmp_path):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    marker.write_text("armed\n", encoding="utf-8")
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))

    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    completed = MagicMock()
    completed.returncode = 0
    completed.stderr = ""
    completed.stdout = ""
    fail = True

    def _run(cmd, **kwargs):
        del kwargs
        reset_cmds.append(list(cmd))
        if fail:
            raise OSError("boom")
        return completed

    with (
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        mgr.stop()
        assert len(scheduled) == 1
        scheduled[0]()
        assert marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 0

        fail = False
        mgr.stop()
        assert len(scheduled) == 2
        scheduled[1]()
        assert not marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 0


def test_stale_tailscale_reset_does_not_drop_a_newer_inflight_count(monkeypatch, tmp_path):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))
    mgr = TunnelManager()
    reset_cmds: list[list[str]] = []
    scheduled: list = []
    spawned: list = []
    tailscale_reset = _tailscale_reset_cmds()

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run(reset_cmds)),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_make_fake_async_process(procs=spawned)),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_background(scheduled)),
    ):
        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == tailscale_reset
        mgr.stop()
        assert len(scheduled) == 1
        assert mgr._providers["tailscale"].reset._resets_in_flight == 1

        assert mgr.start(18765, "tailscale") is True
        assert reset_cmds == tailscale_reset * 2
        mgr.stop()
        assert len(scheduled) == 2
        assert mgr._providers["tailscale"].reset._resets_in_flight == 2

        scheduled[0]()
        assert reset_cmds == tailscale_reset * 2
        assert marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 1

        scheduled[1]()
        assert reset_cmds == tailscale_reset * 3
        assert not marker.exists()
        assert mgr._providers["tailscale"].reset._resets_in_flight == 0


def test_tailscale_spawn_writes_arm_marker_and_post_stop_clears_it(monkeypatch, tmp_path):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))

    mgr = TunnelManager()
    scheduled: list = []
    spawned: list = []

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_record_subprocess_run()),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_make_fake_async_process(procs=spawned)),
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
    """Tailscale arm_path returns the marker file path next to writeragent.json without creating it."""
    from plugin.mcp.tunnel import TailscaleProvider

    fake_config = tmp_path / "writeragent.json"
    fake_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("plugin.framework.config._resolved_config_path", str(fake_config))
    expected = tmp_path / "writeragent-tailscale-funnel-armed"
    reset = TailscaleProvider().reset
    assert reset is not None
    assert reset.arm_path() == str(expected)
    assert not expected.exists()


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
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=_run),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=_make_fake_async_process(procs=spawned)),
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
    mock_tunnel.provider = "cloudflare"
    mock_tunnel.public_url = "https://test-live.trycloudflare.com"
    mock_tunnel.mcp_public_url.return_value = "https://test-live.trycloudflare.com/mcp"

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("subprocess.run", return_value=mock_res),
        patch("urllib.request.urlopen", return_value=mock_probe),
        patch("plugin.mcp.current_tunnel", return_value=mock_tunnel),
    ):
        ok, msg, pub_url = test_tunnel_connectivity("cloudflare", port=18765)
        assert ok is True
        assert "tunnel is running" in msg or "tunnel is active" in msg
        assert pub_url == "https://test-live.trycloudflare.com/mcp"


def test_start_does_not_hold_lock_during_binary_probe(monkeypatch):
    """A hung provider --version must not block stop() or the tunnel lock."""
    import threading

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    probed = threading.Event()
    release = threading.Event()
    result: dict[str, bool] = {}

    def _slow(provider: str) -> str | None:
        del provider
        probed.set()
        assert release.wait(2)
        return None

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
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch("plugin.framework.worker_pool.AsyncProcess", side_effect=AssertionError("should not spawn")),
    ):
        assert mgr.start(18765, "cloudflare", start_token=token) is False
    assert mgr.is_running is False


def test_sync_tunnel_from_main_thread_probes_off_thread(monkeypatch):
    """config:changed must return while provider --version is still running."""
    import threading

    import plugin.mcp as mcp_mod
    from plugin.mcp.tunnel_state import TunnelStatus

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mod = mcp_mod.McpModule.__new__(mcp_mod.McpModule)
    mod.name = "mcp"
    services = MagicMock()
    cfg_data = {
        "mcp.mcp_enabled": True,
        "mcp.tunnel_enabled": True,
        "mcp.mcp_port": 18765,
        "mcp.tunnel_provider": "cloudflare",
        "mcp.tunnel_provider_token": "",
    }
    monkeypatch.setattr(mcp_mod, "get_config", lambda k: cfg_data.get(k))
    mod._services = services
    bound = MagicMock()
    bound.is_running.return_value = True
    bound.port = 18765
    mod.server = bound
    tunnel = TunnelManager()
    mod.tunnel = tunnel
    monkeypatch.setattr(mcp_mod, "_current_module", mod)

    caller = threading.current_thread()
    probed = threading.Event()
    release = threading.Event()
    probe_thread: dict[str, threading.Thread] = {}

    def _slow(provider: str) -> str | None:
        del provider
        probe_thread["t"] = threading.current_thread()
        probed.set()
        assert release.wait(2)
        return "cloudflared binary not found on PATH"

    background: list = []
    monkeypatch.setattr("plugin.mcp.tunnel.binary_available", _slow)
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    with patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)):
        try:
            mod._sync_tunnel()
            assert probed.wait(2)
            assert probe_thread["t"] is not caller
        finally:
            release.set()
        _join_recorded_background(background)
    assert tunnel.status == TunnelStatus.FAILED



def test_start_absent_token_in_env_and_args(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    mgr = TunnelManager()
    started_envs = []
    started_cmds = []

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(cmds=started_cmds, envs=started_envs),
        ),
    ):
        assert mgr.start(18765, "cloudflare", provider_token="") is True
        assert "TUNNEL_TOKEN" not in started_envs[-1]
        assert "--token" not in started_cmds[-1]


def test_tailscale_reset_returns_false_on_nonzero_exit():
    completed = MagicMock()
    completed.returncode = 1
    completed.stderr = "Error: the CLI for serve and funnel has changed."
    completed.stdout = ""
    with patch("subprocess.run", return_value=completed):
        assert TailscaleProvider.reset_funnel(18765) is False


def test_tailscale_reset_returns_true_on_success():
    completed = MagicMock()
    completed.returncode = 0
    completed.stderr = ""
    completed.stdout = ""
    with patch("subprocess.run", return_value=completed):
        assert TailscaleProvider.reset_funnel(18765) is True


def test_tailscale_reset_returns_false_on_exception(caplog):
    import logging

    with patch("subprocess.run", side_effect=OSError("boom")):
        with caplog.at_level(logging.ERROR, logger="writeragent.mcp.tunnel"):
            assert TailscaleProvider.reset_funnel(18765) is False
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def test_binary_available_missing_returns_error():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert binary_available("cloudflare") == "cloudflared binary not found on PATH"


def test_binary_available_timeout_returns_error():
    import subprocess

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["cloudflared", "--version"], timeout=10)):
        assert binary_available("cloudflare") == "cloudflared binary check timed out"


def test_binary_available_permission_error(caplog):
    import logging

    with patch("subprocess.run", side_effect=PermissionError("denied")):
        with caplog.at_level(logging.ERROR, logger="writeragent.mcp.tunnel"):
            assert binary_available("cloudflare") == "could not execute cloudflared"
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def test_binary_available_nonzero_version_is_available():
    completed = MagicMock()
    completed.returncode = 1
    completed.stdout = ""
    completed.stderr = "tailscale 1.2.3"
    with patch("subprocess.run", return_value=completed):
        assert binary_available("tailscale") is None


def test_binary_available_unknown_provider():
    assert binary_available("not-a-provider") == "unknown tunnel provider: not-a-provider"


def test_start_uses_binary_probe_error_verbatim(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    from plugin.mcp.tunnel_state import TunnelStatus

    mgr = TunnelManager()
    with patch("plugin.mcp.tunnel.binary_available", return_value="cloudflared binary check timed out"):
        assert mgr.start(18765, "cloudflare") is False
    assert mgr.status == TunnelStatus.FAILED
    assert mgr.last_error == "cloudflared binary check timed out"
    assert mgr.public_url is None


def test_binary_available_unexpected_error_propagates():
    with patch("subprocess.run", side_effect=RuntimeError("bug")):
        with pytest.raises(RuntimeError, match="bug"):
            binary_available("cloudflare")


def test_notify_url_failure_is_logged(caplog):
    import dataclasses
    import logging
    from plugin.mcp.tunnel_state import NotifyUrlAcquiredEffect, TunnelStatus

    mgr = TunnelManager()
    mgr._state = dataclasses.replace(mgr._state, public_url="https://abc.example.com")
    with patch("plugin.mcp.mcp_ui.notify_tunnel_url_acquired", side_effect=RuntimeError("toast")):
        with caplog.at_level(logging.ERROR, logger="writeragent.mcp.tunnel"):
            mgr._apply_effects_unlocked(
                [NotifyUrlAcquiredEffect("cloudflare", "https://abc.example.com")],
                TunnelStatus.STARTING,
            )
    assert any(r.exc_info for r in caplog.records)
    assert mgr.public_url == "https://abc.example.com"


def test_retire_snippet_provider_propagates_clear_failure():
    mgr = TunnelManager()
    with patch("plugin.mcp.mcp_ui.clear_tested_provider_tunnel_url", side_effect=RuntimeError("cache")):
        with pytest.raises(RuntimeError, match="cache"):
            mgr._retire_snippet_provider("cloudflare")


def test_post_stop_start_failure_decrements_in_flight(monkeypatch):
    mgr = TunnelManager()

    def _boom(*args, **kwargs):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", _boom)
    with mgr._lock:
        with pytest.raises(RuntimeError, match="cannot start thread"):
            mgr._schedule_post_stop_unlocked("tailscale", 18765)
    assert mgr._providers["tailscale"].reset._resets_in_flight == 0


@pytest.mark.parametrize(
    "mode",
    [
        # Idle manager with a stale arm file still schedules funnel off without blocking.
        pytest.param("idle", id="idle"),
        # Live CONNECTED session: quit terminates the CLI and triggers background reset.
        pytest.param("connected", id="connected"),
        # FAILED Tailscale (no live process, _process is None): quit still triggers background reset.
        pytest.param("failed", id="failed"),
    ],
)
def test_stop_for_quit_schedules_background_reset(mode, monkeypatch, tmp_path):
    """LibreOffice quit triggers funnel off in background without blocking."""
    from plugin.mcp.tunnel_state import TunnelStatus

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    marker.write_text("armed\n", encoding="utf-8")
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))

    mgr = TunnelManager()
    procs: list = []
    exits: list = []
    reset_cmds: list[list[str]] = []
    timeouts: list[float] = []
    background: list = []
    funnel_stdout = [
        "Available on the internet:",
        "",
        "https://node.tailnet-name.ts.net/",
    ]

    def _on_start(cmd, stdout_cb, _stderr_cb):
        if mode == "connected" and stdout_cb and cmd[0] == "tailscale":
            for line in funnel_stdout:
                stdout_cb(line)

    with (
        patch("plugin.mcp.tunnel.binary_available", return_value=None),
        patch(
            "plugin.mcp.tunnel.subprocess.run",
            side_effect=_record_subprocess_run(reset_cmds, timeouts=timeouts),
        ),
        patch(
            "plugin.framework.worker_pool.AsyncProcess",
            side_effect=_make_fake_async_process(
                procs=procs,
                exits=exits,
                on_start=_on_start if mode == "connected" else None,
            ),
        ),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        if mode == "connected":
            assert mgr.start(18765, "tailscale") is True
            assert mgr.status == TunnelStatus.CONNECTED
        elif mode == "failed":
            assert mgr.start(18765, "tailscale", max_retries=0) is True
            exits[0](1)
            assert mgr.status == TunnelStatus.FAILED
            assert mgr._process is None
            _join_recorded_background(background)
            background.clear()

        reset_cmds.clear()
        timeouts.clear()
        mgr.stop_for_quit()
        if mode == "connected":
            procs[0].terminate.assert_called_once()
        _join_recorded_background(background)
        assert reset_cmds == _tailscale_reset_cmds()
        assert timeouts == [5]
        assert not marker.exists()


def test_stop_for_quit_sleeping_reset_returns_fast(monkeypatch, tmp_path):
    """When funnel reset sleeps, stop_for_quit returns quickly without joining."""
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    marker = tmp_path / "writeragent-tailscale-funnel-armed"
    marker.write_text("armed\n", encoding="utf-8")
    monkeypatch.setattr("plugin.mcp.tunnel.FunnelReset.arm_path", lambda self: str(marker))

    mgr = TunnelManager()
    blocker = threading.Event()
    started = threading.Event()

    def slow_run(*args, **kwargs):
        started.set()
        blocker.wait(5.0)
        completed = MagicMock()
        completed.returncode = 0
        return completed

    background: list = []
    with (
        patch("plugin.mcp.tunnel.subprocess.run", side_effect=slow_run),
        patch("plugin.framework.worker_pool.run_in_background", side_effect=_track_background(background)),
    ):
        try:
            mgr.stop_for_quit()
            assert started.wait(2.0)
            assert len(background) == 1
            assert background[0].is_alive()
        finally:
            blocker.set()
            _join_recorded_background(background)


def _sync_tunnel_module(monkeypatch, port, bound_port=18765):
    import plugin.mcp as mcp_mod

    mod = mcp_mod.McpModule.__new__(mcp_mod.McpModule)
    mod.name = "mcp"
    services = MagicMock()
    cfg_data = {"mcp.mcp_enabled": True, "mcp.tunnel_enabled": True, "mcp.mcp_port": port, "mcp.tunnel_provider": "cloudflare", "mcp.tunnel_provider_token": ""}
    monkeypatch.setattr(mcp_mod, "get_config", lambda k: cfg_data.get(k))
    mod._services = services
    bound = MagicMock()
    bound.is_running.return_value = True
    bound.port = bound_port
    mod.server = bound
    tunnel = MagicMock()
    tunnel.last_error = None
    mod.tunnel = tunnel
    monkeypatch.setattr(mcp_mod, "_current_module", mod)
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    return mod, tunnel


def test_sync_tunnel_unusable_port_does_not_start(monkeypatch):
    mod, tunnel = _sync_tunnel_module(monkeypatch, port="not-a-port", bound_port=None)
    mod._sync_tunnel()
    tunnel.note_pending_start.assert_not_called()
    tunnel.start.assert_not_called()


def test_sync_tunnel_missing_port_uses_bound_server_port(monkeypatch):
    mod, tunnel = _sync_tunnel_module(monkeypatch, port=None, bound_port=18999)
    mod._sync_tunnel()
    assert tunnel.start.call_args.args[0] == 18999


def test_sync_tunnel_string_port_is_coerced_to_int(monkeypatch):
    mod, tunnel = _sync_tunnel_module(monkeypatch, port="18765")
    mod._sync_tunnel()
    assert tunnel.start.call_args.args[0] == 18765


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "linux", reason="PDEATHSIG is Linux-only")
def test_tunnel_child_dies_with_parent(tmp_path) -> None:
    import os
    import pathlib
    import signal
    import subprocess

    root = str(pathlib.Path(__file__).resolve().parents[2])
    parent_script = tmp_path / "parent_tunnel.py"
    pid_file = tmp_path / "child.pid"
    parent_script.write_text(
        "\n".join(
            [
                "import os, sys, time, threading",
                "from unittest.mock import patch",
                f"sys.path.insert(0, {root!r})",
                "from plugin.framework.queue_executor import Policy, main",
                "main.install(Policy.production)",
                "from plugin.mcp.tunnel import CloudflareProvider, TunnelManager",
                "",
                "cmd = [sys.executable, '-c', 'import time; time.sleep(120)']",
                "with patch('plugin.mcp.tunnel.binary_available', return_value=None), \\",
                "     patch.object(CloudflareProvider, 'build_command', return_value=cmd):",
                "    mgr = TunnelManager()",
                "    t = threading.Thread(target=lambda: mgr.start(18765, 'cloudflare'))",
                "    t.start()",
                "    t.join()",
                "    deadline = time.monotonic() + 3.0",
                "    while time.monotonic() < deadline:",
                "        if mgr._process is None or mgr._process.process is None or mgr._process.process.poll() is not None:",
                "            sys.exit(4)",
                "        time.sleep(0.05)",
                "    with open(sys.argv[1], 'w', encoding='utf-8') as f:",
                "        f.write(str(mgr._process.process.pid))",
                "    time.sleep(120)",
            ]
        ),
        encoding="utf-8",
    )
    parent = subprocess.Popen([sys.executable, str(parent_script), str(pid_file)])
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if pid_file.is_file():
                break
            time.sleep(0.05)
        assert pid_file.is_file(), "parent never recorded child pid"
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        gone_by = time.monotonic() + 5.0
        while time.monotonic() < gone_by:
            try:
                os.kill(child_pid, 0)
            except OSError:
                return
            time.sleep(0.05)
        pytest.fail(f"tunnel child {child_pid} outlived SIGKILL parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass

