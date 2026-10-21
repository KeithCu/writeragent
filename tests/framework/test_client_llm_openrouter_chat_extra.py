# Tests for OpenRouter chat body extras and external editor resolution.

from __future__ import annotations


from plugin.framework.client.base_provider_shim import (
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

