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
