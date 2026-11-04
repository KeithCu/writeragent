"""Tests for plugin.framework.quit_hooks."""

from __future__ import annotations


def test_run_quit_hooks_invokes_registered_callbacks(monkeypatch):
    from plugin.framework import quit_hooks

    monkeypatch.setattr(quit_hooks, "_hooks", [])
    seen: list[str] = []

    def _hook() -> None:
        seen.append("ran")

    quit_hooks.register_quit_hook(_hook)
    quit_hooks.run_quit_hooks()
    assert seen == ["ran"]


def test_run_quit_hooks_continues_after_hook_raises(monkeypatch):
    from plugin.framework import quit_hooks

    monkeypatch.setattr(quit_hooks, "_hooks", [])
    seen: list[str] = []

    def _bad() -> None:
        raise RuntimeError("boom")

    def _good() -> None:
        seen.append("ok")

    quit_hooks.register_quit_hook(_bad)
    quit_hooks.register_quit_hook(_good)
    quit_hooks.run_quit_hooks()
    assert seen == ["ok"]


def test_register_quit_hook_dedupes(monkeypatch):
    from plugin.framework import quit_hooks

    monkeypatch.setattr(quit_hooks, "_hooks", [])
    calls = 0

    def _hook() -> None:
        nonlocal calls
        calls += 1

    quit_hooks.register_quit_hook(_hook)
    quit_hooks.register_quit_hook(_hook)
    quit_hooks.run_quit_hooks()
    assert calls == 1


def test_register_quit_hook_last_moves_to_end_and_runs_once(monkeypatch):
    from plugin.framework import quit_hooks

    monkeypatch.setattr(quit_hooks, "_hooks", [])
    order: list[str] = []

    def supervisor_marker() -> None:
        order.append("supervisor")

    def tunnel() -> None:
        order.append("tunnel")

    quit_hooks._hooks.extend([supervisor_marker, tunnel])
    quit_hooks.register_quit_hook_last(supervisor_marker)
    quit_hooks.run_quit_hooks()
    assert order == ["tunnel", "supervisor"]
    assert quit_hooks._hooks.count(supervisor_marker) == 1


def test_supervisor_quit_hook_orders_after_cooperative_hook(monkeypatch):
    from plugin.framework import quit_hooks
    from plugin.framework.warm_child import Supervisor, register_supervisor_quit_hook

    monkeypatch.setattr(quit_hooks, "_hooks", [])
    local_supervisor = Supervisor()
    monkeypatch.setattr(
        "plugin.framework.warm_child.get_supervisor",
        lambda: local_supervisor,
    )
    ms_calls: list[str] = []

    def _ms_shutdown() -> None:
        ms_calls.append("sweep")

    monkeypatch.setattr(
        "plugin.framework.managed_subprocess.shutdown_all",
        _ms_shutdown,
    )

    def tunnel_marker() -> None:
        pass

    register_supervisor_quit_hook()
    quit_hooks.register_quit_hook(tunnel_marker)
    register_supervisor_quit_hook()
    quit_hooks.run_quit_hooks()
    assert quit_hooks._hooks == [tunnel_marker, local_supervisor.shutdown_all]
    assert ms_calls == ["sweep"]
