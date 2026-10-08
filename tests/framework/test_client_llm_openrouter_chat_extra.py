# Tests for OpenRouter chat body extras and external editor resolution.

from __future__ import annotations

import pytest

from plugin.framework.client.llm_client import (
    OPENROUTER_CHAT_EXTRA_BLOCKLIST,
    merge_openrouter_chat_extra,
)


def test_blocklist_keys_documented() -> None:
    assert "messages" in OPENROUTER_CHAT_EXTRA_BLOCKLIST
    assert "tools" in OPENROUTER_CHAT_EXTRA_BLOCKLIST


def test_merge_skips_blocklisted_keys() -> None:
    base: dict = {
        "messages": [{"role": "user", "content": "keep"}],
        "model": "m",
        "stream": True,
    }
    merge_openrouter_chat_extra(
        base,
        {
            "messages": [],
            "tools": [],
            "tool_choice": "none",
            "stream": False,
            "provider": {"order": ["together"]},
        },
    )
    assert base["messages"] == [{"role": "user", "content": "keep"}]
    assert "tools" not in base
    assert base["stream"] is True
    assert base["provider"]["order"] == ["together"]


def test_merge_nested_provider_dicts() -> None:
    base: dict = {"model": "x", "provider": {"allow_fallbacks": True}}
    merge_openrouter_chat_extra(base, {"provider": {"order": ["a", "b"]}})
    assert base["provider"]["allow_fallbacks"] is True
    assert base["provider"]["order"] == ["a", "b"]


def test_resolve_editor_argv_darwin(monkeypatch: "pytest.MonkeyPatch") -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "darwin")
    assert ee.resolve_editor_argv("/tmp/w.json") == ["open", "-t", "/tmp/w.json"]


def test_resolve_editor_argv_windows_returns_none(monkeypatch: "pytest.MonkeyPatch") -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "win32")
    assert ee.resolve_editor_argv("C:\\a.json") is None


@pytest.mark.parametrize(
    "expected, value, value_2",
    [
        pytest.param("kate", "/usr/bin/kate", "/usr/bin/kate", id="test_resolve_editor_argv_linux_kate_first"),
        pytest.param("gedit", "/usr/bin/gedit", "/usr/bin/gedit", id="test_resolve_editor_argv_linux_gedit_if_no_kate"),
    ],
)
def test_resolve_editor_argv_linux_kate_first(monkeypatch: "pytest.MonkeyPatch", expected, value, value_2) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")

    def _which(cmd: str) -> str | None:
        if cmd == expected:
            return value
        return None

    monkeypatch.setattr(ee.shutil, "which", _which)
    assert ee.resolve_editor_argv("/p.json") == [value_2, "/p.json"]

def test_resolve_editor_argv_linux_editor_env(monkeypatch: "pytest.MonkeyPatch") -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(ee.shutil, "which", lambda _cmd: None)
    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.delenv("VISUAL", raising=False)
    assert ee.resolve_editor_argv("/q.json") == ["nano", "/q.json"]


def test_resolve_editor_argv_linux_visual_when_editor_unset(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(ee.shutil, "which", lambda _cmd: None)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("VISUAL", "vi")
    assert ee.resolve_editor_argv("/r.json") == ["vi", "/r.json"]
